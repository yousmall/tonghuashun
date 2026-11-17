"""账号展示名称与头像的默认值和图片校验。"""
from __future__ import annotations

import base64
import binascii
import hashlib
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_AVATAR_BYTES = 2 * 1024 * 1024


def default_display_name(user_id: int, username: str) -> str:
    """为未设置昵称的新旧账号返回稳定的八位数字字母混合名。"""
    digest = hashlib.sha256(f"wence-user:{user_id}:{username}".encode()).hexdigest()
    suffix = digest[:6] + chr(97 + int(digest[6:8], 16) % 26) + str(int(digest[8:10], 16) % 10)
    return "用户" + suffix


def normalize_avatar(value: str) -> str:
    """只接受真实位图，旋转、裁为正方形并重编码，去掉上传元数据。"""
    try:
        header, encoded = value.split(",", 1)
        if header not in {"data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"}:
            raise ValueError
        payload = base64.b64decode(encoded, validate=True)
        if not payload or len(payload) > MAX_AVATAR_BYTES:
            raise ValueError
        with Image.open(BytesIO(payload)) as image:
            if image.format not in {"PNG", "JPEG", "WEBP"} or max(image.size) > 4096:
                raise ValueError
            image.load()
            square = ImageOps.fit(ImageOps.exif_transpose(image).convert("RGBA"), (256, 256))
        output = BytesIO()
        square.save(output, format="PNG")
    except (ValueError, OSError, binascii.Error, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise ValueError("请上传不超过2MB、边长不超过4096像素的PNG、JPG或WebP头像") from exc
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")
