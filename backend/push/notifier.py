"""Turns the runner's `attention` events into Web Push notifications.

Started with the ASGI app (see riesz/asgi.py), it keeps one WebSocket open to
the runner's global event stream, independent of any browser, and sends each
event to every subscribed browser unless someone is looking at that session.
Subscriptions the push service reports as gone (404/410) are deleted.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import aiohttp
from asgiref.sync import sync_to_async
from django.utils import timezone
from pywebpush import WebPushException, webpush_async

from bridge import runner

from . import presence
from .models import PushSubscription
from .vapid import claims, vapid

log = logging.getLogger(__name__)

# The same kind of event for the same session is not repeated within this window
# (several tool permissions often arrive back to back).
REPEAT_WINDOW = {"permission": 20, "question": 20, "done": 5, "exited": 30}
TTL = {"permission": 6 * 3600, "question": 6 * 3600, "done": 3600, "exited": 3600}
URGENT = {"permission", "question", "exited"}


def _duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec} 秒" if sec else f"{minutes} 分钟"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def build_payload(ev: dict[str, Any]) -> dict[str, Any]:
    kind = ev.get("kind")
    sid = ev.get("sessionId")
    detail = ev.get("detail") or ""
    if kind == "permission":
        head, body = "等你确认", detail or "Claude 需要你的许可"
    elif kind == "question":
        head, body = "Claude 有问题问你", detail
    elif kind == "done":
        head = "任务出错结束" if ev.get("isError") else "任务完成"
        body = f"用时 {_duration(int(ev.get('seconds') or 0))}" + (f" · {detail}" if detail else "")
    else:
        head, body = "会话中断", f"Claude 进程意外退出（代码 {ev.get('code')}）"
    return {
        "title": f"{head} · {ev.get('title') or '会话'}",
        "body": body,
        "url": f"/s/{sid}" if sid else "/",
        "tag": f"{sid}:{kind}",
        "kind": kind,
        "sessionId": sid,
    }


@sync_to_async
def _subscriptions(endpoint: str | None = None) -> list[PushSubscription]:
    qs = PushSubscription.objects.all()
    if endpoint:
        qs = qs.filter(endpoint=endpoint)
    return list(qs)


@sync_to_async
def _record(sub: PushSubscription, ok: bool, gone: bool) -> None:
    if gone:
        PushSubscription.objects.filter(pk=sub.pk).delete()
    elif ok:
        PushSubscription.objects.filter(pk=sub.pk).update(last_sent_at=timezone.now(), failures=0)
    else:
        PushSubscription.objects.filter(pk=sub.pk).update(failures=sub.failures + 1)


class Notifier:
    def __init__(self) -> None:
        self._task: asyncio.Task[None] | None = None
        self._http: aiohttp.ClientSession | None = None
        self._last: dict[tuple[str, str], float] = {}

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._http is not None:
            await self._http.close()
            self._http = None

    def _session(self) -> aiohttp.ClientSession:
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        return self._http

    async def _run(self) -> None:
        delay = 1.0
        while True:
            try:
                ws = await runner.ws_connect()
                async with ws:
                    await ws.send_str(json.dumps({"op": "sub_global"}))
                    delay = 1.0
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            if data.get("ev") == "attention":
                                asyncio.create_task(self.handle(data))
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
            except asyncio.CancelledError:
                raise
            except runner.RunnerUnavailable:
                pass
            except Exception:  # noqa: BLE001
                log.exception("notifier connection failed")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def handle(self, ev: dict[str, Any]) -> None:
        sid = str(ev.get("sessionId") or "")
        kind = str(ev.get("kind") or "")
        if not sid or kind not in REPEAT_WINDOW or presence.is_watched(sid):
            return
        now = time.time()
        if now - self._last.get((sid, kind), 0) < REPEAT_WINDOW[kind]:
            return
        self._last[(sid, kind)] = now
        await self.send(build_payload(ev), urgent=kind in URGENT, ttl=TTL[kind])

    async def send(self, payload: dict[str, Any], *, urgent: bool = False, ttl: int = 3600, endpoint: str | None = None) -> int:
        """Send to every subscription (or only `endpoint`); returns how many accepted it."""
        subs = await _subscriptions(endpoint)
        if not subs:
            return 0
        data = json.dumps(payload, ensure_ascii=False)
        results = await asyncio.gather(*(self._send_one(sub, data, urgent, ttl) for sub in subs))
        return sum(results)

    async def _send_one(self, sub: PushSubscription, data: str, urgent: bool, ttl: int) -> bool:
        try:
            await webpush_async(
                subscription_info=sub.info(), data=data, vapid_private_key=vapid(), vapid_claims=claims(),
                ttl=ttl, headers={"Urgency": "high" if urgent else "normal"}, aiohttp_session=self._session(),
            )
        except WebPushException as exc:
            status = getattr(exc.response, "status", None) or getattr(exc.response, "status_code", None)
            gone = status in (404, 410)
            if not gone:
                log.warning("push to %s failed: %s", sub.endpoint[:60], exc)
            await _record(sub, ok=False, gone=gone)
            return False
        except Exception as exc:  # noqa: BLE001 (network errors)
            log.warning("push to %s failed: %s", sub.endpoint[:60], exc)
            await _record(sub, ok=False, gone=False)
            return False
        await _record(sub, ok=True, gone=False)
        return True


notifier = Notifier()
