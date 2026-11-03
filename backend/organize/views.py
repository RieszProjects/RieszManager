"""Archive flags and manual ordering for sessions and projects; quick phrases."""

from __future__ import annotations

import re

from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from riesz.http import ApiError, api_view, json_body, ok

from .models import ItemState, QuickPhrase

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_ORDER_KEYS = 2000
MAX_PHRASES = 100
MAX_PHRASE_CHARS = 4000


def _validate(kind: object, key: object) -> tuple[str, str]:
    if kind not in (ItemState.KIND_PROJECT, ItemState.KIND_SESSION):
        raise ApiError(400, "未知的类型")
    if not isinstance(key, str) or not key or len(key) > 700:
        raise ApiError(400, "无效的标识")
    if kind == ItemState.KIND_SESSION and not _UUID.match(key):
        raise ApiError(400, "无效的会话 ID")
    return kind, key


@require_GET
@api_view()
def state(request: HttpRequest) -> JsonResponse:
    archived: dict[str, list[str]] = {"project": [], "session": []}
    positions: dict[str, dict[str, int]] = {"project": {}, "session": {}}
    for row in ItemState.objects.all().only("kind", "key", "archived_at", "position"):
        if row.archived_at is not None:
            archived[row.kind].append(row.key)
        if row.position is not None:
            positions[row.kind][row.key] = row.position
    return ok({"archived": archived, "positions": positions})


@require_POST
@api_view()
def archive(request: HttpRequest) -> JsonResponse:
    body = json_body(request)
    kind, key = _validate(body.get("kind"), body.get("key"))
    flag = bool(body.get("archived", True))
    row, _ = ItemState.objects.get_or_create(kind=kind, key=key)
    row.archived_at = timezone.now() if flag else None
    row.save(update_fields=["archived_at", "updated_at"])
    return ok({"kind": kind, "key": key, "archived": flag})


@require_POST
@api_view()
def order(request: HttpRequest) -> JsonResponse:
    """Store a manual order: `keys` lists the items of one list from top to bottom."""
    body = json_body(request)
    kind = body.get("kind")
    keys = body.get("keys")
    if not isinstance(keys, list) or len(keys) > MAX_ORDER_KEYS:
        raise ApiError(400, "无效的顺序")
    validated = [_validate(kind, k)[1] for k in keys]
    with transaction.atomic():
        existing = {r.key: r for r in ItemState.objects.filter(kind=kind, key__in=validated)}
        for position, key in enumerate(validated):
            row = existing.get(key)
            if row is None:
                ItemState.objects.create(kind=kind, key=key, position=position)
            elif row.position != position:
                row.position = position
                row.save(update_fields=["position", "updated_at"])
    return ok({"kind": kind, "count": len(validated)})


@require_http_methods(["GET", "POST"])
@api_view()
def phrases(request: HttpRequest) -> JsonResponse:
    """GET lists the quick phrases; POST replaces them with `phrases` (a list of strings, in order)."""
    if request.method == "POST":
        items = json_body(request).get("phrases")
        if not isinstance(items, list) or len(items) > MAX_PHRASES:
            raise ApiError(400, f"最多 {MAX_PHRASES} 条快捷短语")
        texts = []
        for t in items:
            if not isinstance(t, str):
                raise ApiError(400, "快捷短语必须是文字")
            t = t.strip()
            if len(t) > MAX_PHRASE_CHARS:
                raise ApiError(400, f"每条快捷短语最多 {MAX_PHRASE_CHARS} 个字")
            if t:
                texts.append(t)
        with transaction.atomic():
            QuickPhrase.objects.all().delete()
            QuickPhrase.objects.bulk_create([QuickPhrase(text=t, position=i) for i, t in enumerate(texts)])
    return ok({"phrases": [{"id": p.id, "text": p.text} for p in QuickPhrase.objects.all()]})
