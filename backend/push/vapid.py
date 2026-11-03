"""VAPID keys that identify this server to the browsers' push services.

Read from RIESZ_VAPID_PRIVATE_KEY when set (a PEM, or the base64url-encoded raw
private key). Otherwise a key is generated on first use and
kept in the data directory (vapid_private.pem, readable only by its owner), so
subscriptions stay valid across restarts.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from django.conf import settings
from py_vapid import Vapid02, b64urlencode

_vapid: Vapid02 | None = None


def vapid() -> Vapid02:
    global _vapid
    if _vapid is not None:
        return _vapid
    raw = settings.VAPID_PRIVATE_KEY.strip()
    if raw:
        key = Vapid02.from_pem(raw.encode()) if raw.startswith("-----") else Vapid02.from_string(raw)
    else:
        path = settings.DATA_DIR / "vapid_private.pem"
        if path.exists():
            key = Vapid02.from_file(str(path))
        else:
            key = Vapid02()
            key.generate_keys()
            key.save_key(str(path))
            os.chmod(path, 0o600)
    _vapid = key
    return key


def public_key() -> str:
    """The application server key browsers subscribe with (base64url, uncompressed point)."""
    raw = vapid().public_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return b64urlencode(raw)


def claims() -> dict[str, str]:
    """A fresh claims dict per request (pywebpush adds `aud` and `exp` to it)."""
    origin = settings.PUBLIC_ORIGIN
    return {"sub": origin if origin.startswith("https://") else "mailto:riesz@localhost"}
