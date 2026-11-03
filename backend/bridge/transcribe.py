"""Speech-to-text for the composer's voice input (OpenAI-compatible API)."""

from __future__ import annotations

import aiohttp
from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.views.decorators.http import require_POST

from riesz.http import ApiError, api_view, ok

MAX_AUDIO_BYTES = 25 * 1024 * 1024
PROMPT = "以下是关于编程和软件开发的口述，可能中英文混杂。"


@require_POST
@api_view()
async def transcribe(request: HttpRequest) -> HttpResponse:
    if not settings.OPENAI_API_KEY:
        raise ApiError(501, "未配置语音转写服务", "transcription_disabled")
    audio = request.FILES.get("audio")
    if audio is None:
        raise ApiError(400, "缺少音频")
    if audio.size > MAX_AUDIO_BYTES:
        raise ApiError(413, "音频过长")
    form = aiohttp.FormData()
    form.add_field("file", audio.read(), filename=audio.name or "speech.webm",
                   content_type=audio.content_type or "audio/webm")
    form.add_field("model", settings.TRANSCRIBE_MODEL)
    form.add_field("response_format", "json")
    form.add_field("prompt", PROMPT)
    if settings.TRANSCRIBE_LANGUAGE:
        form.add_field("language", settings.TRANSCRIBE_LANGUAGE)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as client:
        async with client.post(f"{settings.OPENAI_BASE_URL}/audio/transcriptions", data=form,
                               headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}"}) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                message = (data.get("error") or {}).get("message") if isinstance(data, dict) else None
                raise ApiError(502, f"转写失败：{message or resp.status}", "transcription_failed")
    return ok({"text": (data.get("text") or "").strip()})
