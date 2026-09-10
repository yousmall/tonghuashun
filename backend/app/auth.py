"""无第三方依赖的密码哈希与短期登录令牌。"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any


_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1


class TokenError(ValueError):
    """令牌缺失、被篡改或已过期。"""


def hash_password(password: str) -> str:
    """使用带随机盐的 scrypt 保存密码，数据库中永不出现明文密码。"""
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32
    )
    return "$".join(
        ("scrypt", str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P), _b64encode(salt), _b64encode(digest))
    )


def verify_password(password: str, encoded: str) -> bool:
    """以常量时间比较密码；旧数据格式异常时安全地返回失败。"""
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=_b64decode(salt), n=int(n), r=int(r), p=int(p), dklen=32
        )
        return hmac.compare_digest(digest, _b64decode(expected))
    except (ValueError, TypeError):
        return False


def create_access_token(
    user_id: int,
    username: str,
    secret: str,
    ttl_hours: int | None = None,
    *,
    session_id: str | None = None,
) -> tuple[str, datetime]:
    """创建带服务端会话标识的 HMAC 签名令牌。"""
    ttl = ttl_hours or int(os.getenv("WENCE_AUTH_TOKEN_TTL_HOURS", "24"))
    expires_at = datetime.now(timezone.utc) + timedelta(hours=max(1, ttl))
    payload = {
        "sub": user_id,
        "username": username,
        "exp": int(expires_at.timestamp()),
        "sid": session_id or secrets.token_urlsafe(24),
    }
    encoded_payload = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{encoded_payload}.{_sign(encoded_payload, secret)}", expires_at


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    """校验签名和过期时间并返回令牌载荷。"""
    try:
        encoded_payload, supplied_signature = token.split(".", 1)
        if not hmac.compare_digest(supplied_signature, _sign(encoded_payload, secret)):
            raise TokenError("登录状态无效")
        payload = json.loads(_b64decode(encoded_payload))
        if int(payload["exp"]) <= int(datetime.now(timezone.utc).timestamp()):
            raise TokenError("登录已过期，请重新登录")
        if not isinstance(payload.get("sub"), int) or not isinstance(payload.get("sid"), str):
            raise TokenError("登录状态无效")
        return payload
    except TokenError:
        raise
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise TokenError("登录状态无效") from exc


def _sign(payload: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
