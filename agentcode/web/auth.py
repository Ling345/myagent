"""登录态：HMAC 签名的 cookie，不需要服务端会话表。

token 形如 ``<账号id>.<过期时间戳>.<签名>``，签名用服务端密钥算出来，
客户端改一个字符就会失效。密钥来自 ``AGENT_SECRET_KEY``（缺省时每次启动随机生成，
那样重启后需要重新登录；线上应当固定配置它）。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from http.cookies import SimpleCookie

COOKIE_NAME = "agentcode_session"
DEFAULT_TTL_SECONDS = 7 * 24 * 3600


def _sign(secret: str, payload: str) -> str:
    """对 payload 做 HMAC-SHA256，取前 32 位十六进制。"""
    return hmac.new(
        secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()[:32]


def create_token(
    account_id: str,
    secret: str,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> str:
    """签发登录 token。"""
    issued_at = int(now if now is not None else time.time())
    payload = f"{account_id}.{issued_at + int(ttl_seconds)}"
    return f"{payload}.{_sign(secret, payload)}"


def read_token(token: str, secret: str, now: float | None = None) -> str | None:
    """校验 token，返回账号 id；签名不对或已过期返回 None。"""
    parts = str(token or "").split(".")
    if len(parts) != 3:
        return None
    account_id, expires_at, signature = parts
    if not account_id or not expires_at.isdigit():
        return None
    payload = f"{account_id}.{expires_at}"
    if not hmac.compare_digest(_sign(secret, payload), signature):
        return None
    if int(expires_at) < (now if now is not None else time.time()):
        return None
    return account_id


def build_cookie(
    token: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    secure: bool = False,
    name: str = COOKIE_NAME,
) -> str:
    """生成 Set-Cookie：HttpOnly 防止脚本读取，SameSite=Lax 防跨站提交。"""
    cookie = SimpleCookie()
    cookie[name] = token
    morsel = cookie[name]
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    morsel["path"] = "/"
    morsel["max-age"] = str(int(ttl_seconds))
    if secure:
        morsel["secure"] = True
    return morsel.OutputString()


def clear_cookie(name: str = COOKIE_NAME) -> str:
    """生成过期的 Set-Cookie，用来登出。"""
    cookie = SimpleCookie()
    cookie[name] = ""
    morsel = cookie[name]
    morsel["httponly"] = True
    morsel["samesite"] = "Lax"
    morsel["path"] = "/"
    morsel["max-age"] = "0"
    return morsel.OutputString()


def read_cookie(cookie_header: str | None, name: str = COOKIE_NAME) -> str | None:
    """从 Cookie 请求头里取出 token。"""
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:  # noqa: BLE001 - 坏 Cookie 当作未登录
        return None
    morsel = cookie.get(name)
    return morsel.value if morsel else None
