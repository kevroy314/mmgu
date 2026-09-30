"""Image uploads: validate, strip metadata, optionally crop, dedupe by hash, store on disk."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mmgu.core.hall import hall
from mmgu.core.models import Upload

MAX_BYTES = 12 * 1024 * 1024
MAX_SIDE = 3000


class UploadError(ValueError):
    pass


def _clean_image(data: bytes, crop: tuple[int, int, int, int] | None) -> tuple[bytes, int, int]:
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001
        raise UploadError("That file isn't an image we can read. Use PNG, JPG or WebP.") from e
    img = ImageOps.exif_transpose(img)
    if crop:
        x, y, w, h = crop
        if w > 8 and h > 8:
            img = img.crop((max(0, x), max(0, y), min(img.width, x + w), min(img.height, y + h)))
    if max(img.size) > MAX_SIDE:
        img.thumbnail((MAX_SIDE, MAX_SIDE))
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)  # re-encoding drops EXIF and any other metadata
    return out.getvalue(), img.width, img.height


async def save_image(
    session: AsyncSession,
    data: bytes,
    *,
    uploaded_by: int | None,
    purpose: str = "screenshot",
    crop: tuple[int, int, int, int] | None = None,
) -> Upload:
    if len(data) > MAX_BYTES:
        raise UploadError("That image is over 12 MB. Crop it or save it as JPG.")
    clean, w, h = _clean_image(data, crop)
    digest = hashlib.sha256(clean).hexdigest()
    existing = (await session.execute(select(Upload).where(Upload.sha256 == digest))).scalars().first()
    if existing is not None:
        return existing
    rel = Path(digest[:2]) / f"{digest}.png"
    dest = hall.settings.uploads_dir / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(clean)
    up = Upload(
        sha256=digest,
        path=str(rel),
        mime="image/png",
        width=w,
        height=h,
        size=len(clean),
        purpose=purpose,
        uploaded_by=uploaded_by,
    )
    session.add(up)
    await session.flush()
    return up


def upload_path(up: Upload) -> Path:
    return hall.settings.uploads_dir / up.path


def upload_bytes(up: Upload) -> bytes:
    return upload_path(up).read_bytes()


def parse_crop(value: str | None) -> tuple[int, int, int, int] | None:
    """Parse "x,y,w,h" from the crop tool."""
    if not value:
        return None
    try:
        parts = [int(float(p)) for p in value.split(",")]
    except ValueError:
        return None
    return tuple(parts) if len(parts) == 4 else None  # type: ignore[return-value]
