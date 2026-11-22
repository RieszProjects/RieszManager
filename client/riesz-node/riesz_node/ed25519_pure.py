"""Pure-Python RFC 8032 Ed25519 (zero native dependencies, cross-platform)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

_P = (1 << 255) - 19
_L = (1 << 252) + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, -1, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * pow(_D * y * y + 1, -1, _P) % _P
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = (4 * pow(5, -1, _P)) % _P
_BX = _xrecover(_BY)
_B = (_BX % _P, _BY % _P, 1, (_BX * _BY) % _P)
_ID = (0, 1, 1, 0)


def _point_add(p: tuple[int, int, int, int], q: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (2 * t1 * t2 * _D) % _P
    d = (2 * z1 * z2) % _P
    e = (b - a) % _P
    f = (d - c) % _P
    g = (d + c) % _P
    h = (b + a) % _P
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _scalar_mul(pt: tuple[int, int, int, int], e: int) -> tuple[int, int, int, int]:
    res = _ID
    cur = pt
    while e > 0:
        if e & 1:
            res = _point_add(res, cur)
        cur = _point_add(cur, cur)
        e >>= 1
    return res


def _encode_point(pt: tuple[int, int, int, int]) -> bytes:
    x, y, z, _ = pt
    zi = pow(z, -1, _P)
    x = (x * zi) % _P
    y = (y * zi) % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _clamp(seed: bytes) -> tuple[int, bytes]:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def b64u_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def generate_keypair() -> tuple[bytes, bytes]:
    seed = secrets.token_bytes(32)
    return seed, public_key(seed)


def public_key(seed: bytes) -> bytes:
    a, _ = _clamp(seed)
    return _encode_point(_scalar_mul(_B, a))


def sign(seed: bytes, message: bytes) -> bytes:
    a, prefix = _clamp(seed)
    pub = _encode_point(_scalar_mul(_B, a))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    R = _encode_point(_scalar_mul(_B, r))
    k = int.from_bytes(hashlib.sha512(R + pub + message).digest(), "little") % _L
    s = (r + k * a) % _L
    return R + s.to_bytes(32, "little")
