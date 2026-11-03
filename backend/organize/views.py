"""Archive flags and manual ordering for sessions and projects."""

from __future__ import annotations

import re

from django.db import transaction
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from riesz.http import ApiError, api_view, json_body, ok

from .models import ItemState

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MAX_ORDER_KEYS = 2000


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
