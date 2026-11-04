"""Push notification subscriptions: VAPID key, (un)subscribe, test message."""

from __future__ import annotations

from urllib.parse import urlparse

from asgiref.sync import sync_to_async
from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET, require_POST

from riesz.http import ApiError, api_view, json_body, ok

from .models import PushSubscription
from .notifier import notifier
from .vapid import public_key


@require_GET
@api_view()
def config(request: HttpRequest) -> JsonResponse:
    return ok({"publicKey": public_key()})


def _endpoint(value: object) -> str:
    if not isinstance(value, str) or len(value) > 1000 or urlparse(value).scheme != "https":
        raise ApiError(400, "无效的推送地址")
    return value


@require_POST
@api_view()
def subscribe(request: HttpRequest) -> JsonResponse:
    sub = json_body(request).get("subscription")
    if not isinstance(sub, dict):
        raise ApiError(400, "缺少订阅信息")
    endpoint = _endpoint(sub.get("endpoint"))
    keys = sub.get("keys") if isinstance(sub.get("keys"), dict) else {}
    p256dh, auth = keys.get("p256dh"), keys.get("auth")
    if not (isinstance(p256dh, str) and isinstance(auth, str) and 0 < len(p256dh) <= 200 and 0 < len(auth) <= 100):
        raise ApiError(400, "无效的订阅密钥")
    PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"p256dh": p256dh, "auth": auth, "failures": 0,
                  "user_agent": request.META.get("HTTP_USER_AGENT", "")[:400]},
    )
    return ok({"subscribed": True})


@require_POST
@api_view()
def unsubscribe(request: HttpRequest) -> JsonResponse:
    endpoint = _endpoint(json_body(request).get("endpoint"))
    PushSubscription.objects.filter(endpoint=endpoint).delete()
    return ok({"subscribed": False})


@require_POST
@api_view()
async def test(request: HttpRequest) -> JsonResponse:
    body = await sync_to_async(json_body)(request)
    endpoint = _endpoint(body.get("endpoint"))
    sent = await notifier.send(
        {"title": "Riesz Manager", "body": "推送通知已开启。Claude 等你确认、提问或完成长任务时会在这里提醒你。",
         "url": "/", "tag": "riesz-test", "kind": "test"},
        urgent=True, ttl=600, endpoint=endpoint,
    )
    if not sent:
        raise ApiError(502, "推送服务没有接受这条消息，请关闭后重新打开推送")
    return ok({"sent": sent})
