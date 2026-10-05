from __future__ import annotations

import io
import uuid
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageOps

STATIC_DIR = Path(__file__).resolve().parent / "static"
AVATAR_DIR = STATIC_DIR / "uploads" / "avatars"
AVATAR_SIZE = 320
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


class AvatarError(Exception):
    pass


def save_avatar(profile_id: str, file: UploadFile, old_avatar_path: str | None) -> str:
    """Yüklenen resmi kareye kırpıp sabit boyuta indirger, JPEG olarak kaydeder ve
    static/ altına göreli yolunu döner. Her yüklemede benzersiz dosya adı üretilir
    (tarayıcı cache'i eski resmi göstermeye devam etmesin diye) ve eski dosya silinir."""
    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise AvatarError("Desteklenmeyen dosya türü. JPEG, PNG, WEBP veya GIF yükle.")

    raw = file.file.read(_MAX_UPLOAD_BYTES + 1)
    if len(raw) > _MAX_UPLOAD_BYTES:
        raise AvatarError("Dosya çok büyük (maks. 8 MB).")
    if not raw:
        raise AvatarError("Boş dosya.")

    try:
        image = Image.open(io.BytesIO(raw))
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
    except Exception as exc:  # noqa: BLE001
        raise AvatarError(f"Resim okunamadı: {exc}") from exc

    side = min(image.width, image.height)
    left = (image.width - side) // 2
    top = (image.height - side) // 2
    image = image.crop((left, top, left + side, top + side)).resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)

    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{profile_id}-{uuid.uuid4().hex[:8]}.jpg"
    image.save(AVATAR_DIR / filename, format="JPEG", quality=88)

    if old_avatar_path:
        old_file = STATIC_DIR / old_avatar_path
        if old_file.is_relative_to(AVATAR_DIR) and old_file.exists():
            old_file.unlink()

    return f"uploads/avatars/{filename}"


def delete_avatar(avatar_path: str | None) -> None:
    if not avatar_path:
        return
    old_file = STATIC_DIR / avatar_path
    if old_file.is_relative_to(AVATAR_DIR) and old_file.exists():
        old_file.unlink()
