"""Live speech-to-text for the composer's voice input: text appears while the user speaks.

The browser opens `/ws/transcribe` and sends
    {"type": "start", "keywords": [...]}     once, then
    binary frames of 24 kHz mono PCM16       while recording, and
    {"type": "stop"}                         when the user stops.
It gets back
    {"type": "ready"}                        the transcription service is connected
    {"type": "text", "text": "..."}          everything recognised so far (replaces the previous one)
    {"type": "done", "text": "..."}          the final transcript, then the socket closes
    {"type": "error", "message": "..."}      then the socket closes; the browser falls back to uploading its recording.

Behind it is a Realtime transcription session of the OpenAI API (`gpt-live-transcribe`).
The API key stays here. One recording is normally one turn; a very long one is cut
into turns at a quiet moment, and the turns' texts are joined in order.
"""

from __future__ import annotations

import array
import asyncio
import base64
import json
import logging
import re
import sys
from typing import Any

import aiohttp
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from .consumers import _totp_ok
from .transcribe import PROMPT, clean_keywords

log = logging.getLogger(__name__)

SAMPLE_RATE = 24000
BYTES_PER_SECOND = SAMPLE_RATE * 2
MAX_FRAME_BYTES = 64 * 1024
MAX_SECONDS = 15 * 60                 # one recording
TURN_SECONDS = 150                    # after this much audio, a turn ends at the next quiet moment…
TURN_HARD_SECONDS = 210               # …or here at the latest
QUIET_SECONDS = 0.5
QUIET_LEVEL = 500                     # peak sample value (of 32768) below which a frame counts as quiet
PENDING_SECONDS = 20                  # audio kept while the service is still connecting
FINISH_TIMEOUT = 20.0
MIN_COMMIT_BYTES = BYTES_PER_SECOND // 5

# The live model writes " Claude Code里": drop the space it leaves between Chinese and Latin text.
CJK = r"㐀-鿿　-〿＀-￯"
SPACE_AFTER_CJK = re.compile(rf"(?<=[{CJK}]) +(?=[A-Za-z0-9])")
SPACE_BEFORE_CJK = re.compile(rf"(?<=[A-Za-z0-9]) +(?=[{CJK}])")
SPACE_IN_CJK = re.compile(rf"(?<=[{CJK}]) +(?=[{CJK}])")      # where two turns were joined


def tidy(text: str) -> str:
    for pattern in (SPACE_AFTER_CJK, SPACE_BEFORE_CJK, SPACE_IN_CJK):
        text = pattern.sub("", text)
    return text.strip()


def realtime_url() -> str:
    base = settings.OPENAI_BASE_URL
    return ("wss" + base[5:] if base.startswith("https") else "ws" + base[4:]) + "/realtime?intent=transcription"


def is_quiet(frame: bytes) -> bool:
    samples = array.array("h")
    samples.frombytes(frame[: len(frame) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return not samples or max(max(samples), -min(samples)) < QUIET_LEVEL


class TranscribeConsumer(AsyncWebsocketConsumer):
    http: aiohttp.ClientSession | None = None
    upstream: aiohttp.ClientWebSocketResponse | None = None
    reader: asyncio.Task[Any] | None = None

    async def connect(self) -> None:
        user = self.scope.get("user")
        if user is None or not user.is_authenticated or not await _totp_ok(user.username):
            await self.close(code=4401)
            return
        if not settings.OPENAI_API_KEY or not settings.TRANSCRIBE_LIVE_MODEL:
            await self.close(code=4501)
            return
        self.started = False
        self.finished = False
        self.stopping = False
        self.pending: list[bytes] = []          # frames received before the service was ready
        self.pending_bytes = 0
        self.total_bytes = 0
        self.turn_bytes = 0                     # audio sent since the last commit
        self.quiet_bytes = 0                    # length of the current quiet stretch
        self.commits = 0
        self.order: list[str] = []              # turns (items) in the order they were first heard of
        self.texts: dict[str, str] = {}
        self.completed: set[str] = set()
        self.all_done = asyncio.Event()
        await self.accept()

    # ---- from the browser ---------------------------------------------------------

    async def receive(self, text_data: str | None = None, bytes_data: bytes | None = None) -> None:
        if self.finished:
            return
        if bytes_data is not None:
            await self._audio(bytes_data)
            return
        try:
            data = json.loads(text_data or "")
        except ValueError:
            return
        kind = data.get("type") if isinstance(data, dict) else None
        if kind == "start" and not self.started:
            self.started = True
            keywords = data.get("keywords")
            self.reader = asyncio.create_task(self._run(clean_keywords(keywords if isinstance(keywords, list) else [])))
        elif kind == "stop" and self.started and not self.stopping:
            self.stopping = True
            asyncio.create_task(self._finish())

    async def _audio(self, frame: bytes) -> None:
        if not self.started or self.stopping or not frame or len(frame) > MAX_FRAME_BYTES:
            return
        self.total_bytes += len(frame)
        if self.total_bytes > MAX_SECONDS * BYTES_PER_SECOND:
            self.stopping = True
            asyncio.create_task(self._finish())
            return
        if self.upstream is None:
            if self.pending_bytes < PENDING_SECONDS * BYTES_PER_SECOND:
                self.pending.append(frame)
                self.pending_bytes += len(frame)
            return
        await self._append(frame)

    # ---- to and from the transcription service -----------------------------------------

    async def _append(self, frame: bytes, upstream: aiohttp.ClientWebSocketResponse | None = None) -> None:
        upstream = upstream or self.upstream
        assert upstream is not None
        await upstream.send_json({"type": "input_audio_buffer.append", "audio": base64.b64encode(frame).decode()})
        self.turn_bytes += len(frame)
        self.quiet_bytes = self.quiet_bytes + len(frame) if is_quiet(frame) else 0
        seconds = self.turn_bytes / BYTES_PER_SECOND
        if seconds >= TURN_HARD_SECONDS or (seconds >= TURN_SECONDS and self.quiet_bytes >= QUIET_SECONDS * BYTES_PER_SECOND):
            await self._commit()

    async def _commit(self) -> None:
        if self.upstream is None or self.turn_bytes < MIN_COMMIT_BYTES:
            return
        self.turn_bytes = 0
        self.quiet_bytes = 0
        self.commits += 1
        self.all_done.clear()
        await self.upstream.send_json({"type": "input_audio_buffer.commit"})

    async def _run(self, keywords: list[str]) -> None:
        """Connect to the service, pass on what it recognises, and end the socket when it fails."""
        transcription: dict[str, Any] = {"model": settings.TRANSCRIBE_LIVE_MODEL, "prompt": PROMPT,
                                         "delay": settings.TRANSCRIBE_LIVE_DELAY}
        if keywords:
            transcription["keywords"] = keywords
        if settings.TRANSCRIBE_LANGUAGES:
            transcription["languages"] = settings.TRANSCRIBE_LANGUAGES
        try:
            self.http = aiohttp.ClientSession()
            upstream = await self.http.ws_connect(
                realtime_url(), headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"},
                heartbeat=20)
            await upstream.send_json({"type": "session.update", "session": {"type": "transcription", "audio": {"input": {
                "format": {"type": "audio/pcm", "rate": SAMPLE_RATE}, "transcription": transcription,
                "turn_detection": None}}}})
            while self.pending:            # audio that arrived meanwhile, in order, before anything newer
                await self._append(self.pending.pop(0), upstream)
            self.upstream = upstream
            await self.send(text_data=json.dumps({"type": "ready"}))
            async for msg in upstream:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                await self._event(json.loads(msg.data))
            if not self.finished:
                await self._fail("转写服务断开了")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the browser, which falls back to uploading
            log.warning("live transcription failed: %s", exc)
            if not self.finished:
                await self._fail("连不上转写服务")

    async def _event(self, ev: dict[str, Any]) -> None:
        kind = str(ev.get("type") or "")
        item = ev.get("item_id")
        if kind.endswith("input_audio_transcription.delta") and isinstance(item, str):
            self._note(item)
            self.texts[item] += str(ev.get("delta") or "")
            await self._send_text()
        elif kind.endswith("input_audio_transcription.completed") and isinstance(item, str):
            self._note(item)
            self.texts[item] = str(ev.get("transcript") or "")
            self._completed(item)
            await self._send_text()
        elif kind.endswith("input_audio_transcription.failed") and isinstance(item, str):
            self._note(item)
            self._completed(item)
            log.warning("live transcription turn failed: %s", json.dumps(ev.get("error"))[:300])
        elif kind == "error":
            message = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else None
            log.warning("live transcription error: %s", message)
            if self.stopping:      # e.g. a commit with too little audio: finish with what there is
                self.all_done.set()
            else:
                await self._fail(f"转写出错：{message or '未知错误'}")

    def _note(self, item: str) -> None:
        if item not in self.texts:
            self.order.append(item)
            self.texts[item] = ""

    def _completed(self, item: str) -> None:
        self.completed.add(item)
        if len(self.completed) >= self.commits:
            self.all_done.set()

    def _text(self) -> str:
        return tidy(" ".join(t for t in (self.texts[i].strip() for i in self.order) if t))

    async def _send_text(self) -> None:
        if not self.finished:
            await self.send(text_data=json.dumps({"type": "text", "text": self._text()}))

    # ---- ending -------------------------------------------------------------------------

    async def _finish(self) -> None:
        """The user stopped: close the last turn, wait for its text, send the result."""
        try:
            for _ in range(100):               # the service may still be connecting
                if self.upstream is not None or self.finished:
                    break
                await asyncio.sleep(0.1)
            if self.finished:
                return
            if self.upstream is None:
                await self._fail("连不上转写服务")
                return
            await self._commit()
            if self.commits > len(self.completed):
                try:
                    await asyncio.wait_for(self.all_done.wait(), FINISH_TIMEOUT)
                except asyncio.TimeoutError:
                    await self._fail("转写超时")
                    return
            self.finished = True
            await self.send(text_data=json.dumps({"type": "done", "text": self._text()}))
            await self.close()
            await self._release()
        except Exception:  # noqa: BLE001
            log.exception("finishing a live transcription failed")
            await self._fail("转写出错")

    async def _fail(self, message: str) -> None:
        if self.finished:
            return
        self.finished = True
        try:
            await self.send(text_data=json.dumps({"type": "error", "message": message}))
            await self.close()
        except Exception:  # noqa: BLE001 - the browser is already gone
            pass
        await self._release()

    async def _release(self) -> None:
        """Close the connection to the transcription service (its reader then ends by itself)."""
        upstream, http = self.upstream, self.http
        self.upstream = self.http = None
        if upstream is not None:
            await upstream.close()
        if http is not None:
            await http.close()

    async def disconnect(self, code: int) -> None:
        self.finished = True
        if self.reader is not None and self.reader is not asyncio.current_task():
            self.reader.cancel()
        await self._release()
