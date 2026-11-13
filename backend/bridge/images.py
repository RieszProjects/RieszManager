"""Photos in formats Claude cannot read (HEIC/HEIF from phone cameras, AVIF, TIFF, BMP) become JPEG on upload.

Claude takes PNG, JPEG, GIF and WebP. A photo library hands over the camera's original file, which on many
phones is HEIC: without this it would reach Claude as a file path it cannot open as an image.
"""

from __future__ import annotations

import io
import logging
from pathlib import PurePath

log = logging.getLogger(__name__)

READABLE = {"image/png", "image/jpeg", "image/gif", "image/webp"}
CONVERT_TYPES = {"image/heic", "image/heif", "image/heic-sequence", "image/heif-sequence", "image/avif",
                 "image/tiff", "image/bmp", "image/x-ms-bmp"}
CONVERT_SUFFIXES = {".heic", ".heif", ".hif", ".avif", ".tif", ".tiff", ".bmp"}
MAX_EDGE = 2000          # like the web app does for large photos
JPEG_QUALITY = 88


def wants_conversion(name: str, mime: str | None) -> bool:
    mime = (mime or "").lower()
    if mime in READABLE:
        return False
    return mime in CONVERT_TYPES or PurePath(name or "").suffix.lower() in CONVERT_SUFFIXES


def to_jpeg(data: bytes, name: str) -> tuple[bytes, str] | None:
    """The image as JPEG (upright, at most MAX_EDGE on a side) and its new file name; None if it cannot be read."""
    try:
        from PIL import Image, ImageOps
        import pillow_heif

        pillow_heif.register_heif_opener()
        with Image.open(io.BytesIO(data)) as img:
            img = ImageOps.exif_transpose(img)
            img.thumbnail((MAX_EDGE, MAX_EDGE))
            if img.mode not in ("RGB", "L"):
                # Flatten transparency onto white rather than black.
                rgba = img.convert("RGBA")
                flat = Image.new("RGB", rgba.size, (255, 255, 255))
                flat.paste(rgba, mask=rgba.getchannel("A"))
                img = flat
            out = io.BytesIO()
            img.save(out, "JPEG", quality=JPEG_QUALITY, optimize=True)
    except Exception as exc:  # noqa: BLE001 - an unreadable file is uploaded as it is
        log.warning("could not convert %s to JPEG: %s", name, exc)
        return None
    return out.getvalue(), (PurePath(name or "photo").stem or "photo") + ".jpg"
