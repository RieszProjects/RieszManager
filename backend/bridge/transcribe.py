"""Speech-to-text for the composer's voice input (OpenAI-compatible API).

The recording goes to `/audio/transcriptions`. Models of the `gpt-transcribe`
family also take a list of keywords (names and terms it should recognise) and a
list of languages; older models and other providers reject those fields, so
they are sent only to that family, and a request the provider refuses is tried
once more without them.
"""

from __future__ import annotations

import logging
import re

import aiohttp
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_POST

from riesz.http import ApiError, api_view, ok

log = logging.getLogger(__name__)

MAX_AUDIO_BYTES = 25 * 1024 * 1024
PROMPT = "以下是关于编程和软件开发的口述，可能中英文混杂。"
# Names speech recognition tends to get wrong ("Cloud Code") in sessions about Claude Code.
BASE_KEYWORDS = ("Claude", "Claude Code", "Anthropic", "Riesz Manager", "GitHub")
MAX_KEYWORDS = 100
MAX_KEYWORD_CHARS = 60
KEYWORD_FORBIDDEN = re.compile(r"[<>\r\n]")


def _takes_hints(model: str) -> bool:
    """Whether the model accepts `keywords` and `languages` (the gpt-transcribe family)."""
    return model.startswith("gpt-transcribe")


def clean_keywords(sent: list[object]) -> list[str]:
    """Built-in names, the deployment's own, and the ones the web app sent (project names), without repeats."""
    out: list[str] = []
    seen: set[str] = set()
    for word in (*BASE_KEYWORDS, *settings.TRANSCRIBE_KEYWORDS, *sent):
        word = " ".join(str(word).split())
        if not word or len(word) > MAX_KEYWORD_CHARS or KEYWORD_FORBIDDEN.search(word) or word.lower() in seen:
            continue
        seen.add(word.lower())
        out.append(word)
        if len(out) >= MAX_KEYWORDS:
            break
    return out


async def _request(client: aiohttp.ClientSession, audio: bytes, filename: str, content_type: str,
                   fields: list[tuple[str, str]]) -> tuple[int, object]:
    form = aiohttp.FormData()
    form.add_field("file", audio, filename=filename, content_type=content_type)
    for name, value in fields:
        form.add_field(name, value)
    async with client.post(f"{settings.OPENAI_BASE_URL}/audio/transcriptions", data=form,
                           headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}) as resp:
        return resp.status, await resp.json(content_type=None)


@require_POST
@api_view()
async def transcribe(request: HttpRequest) -> HttpResponse:
    if not settings.OPENAI_API_KEY:
        raise ApiError(501, "未配置语音转写服务", "transcription_disabled")
    upload = request.FILES.get("audio")
    if upload is None:
        raise ApiError(400, "缺少音频")
    if upload.size > MAX_AUDIO_BYTES:
        raise ApiError(413, "音频过长")
    audio = upload.read()
    filename = upload.name or "speech.webm"
    content_type = upload.content_type or "audio/webm"
    model = settings.TRANSCRIBE_MODEL
    languages = settings.TRANSCRIBE_LANGUAGES

    base = [("model", model), ("response_format", "json"), ("prompt", PROMPT)]
    hints: list[tuple[str, str]] = []
    if _takes_hints(model):
        hints += [("keywords[]", word) for word in clean_keywords(request.POST.getlist("keywords"))]
        hints += [("languages[]", code) for code in languages]
    elif languages:
        base.append(("language", languages[0]))

    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        status, data = await _request(client, audio, filename, content_type, base + hints)
        if status == 400 and hints:
            log.warning("transcription with keywords was refused (%s); trying without them",
                        (data.get("error") or {}).get("message") if isinstance(data, dict) else status)
            status, data = await _request(client, audio, filename, content_type, base)
    if status != 200:
        message = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
        raise ApiError(502, f"转写失败：{message or status}", "transcription_failed")
    return ok({"text": (data.get("text") or "").strip() if isinstance(data, dict) else ""})
