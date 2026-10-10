"""任务完成回调：跑完了主动告诉调用方，而不是让他一直轮询。

异步任务（``POST /v1/run`` 带 ``"async": true``）现在只能靠 ``GET /v1/runs/<id>``
轮询。给一个 ``callback_url``，任务一结束我们就把结果 POST 过去，调用方"发完就走"。

但把"往用户给的地址发请求"做成产品，有三个坑必须自己先堵上：

1. **别打内网**（SSRF）。回调地址是调用方给的，照着发就等于送给别人一个内网探针：
   ``http://169.254.169.254/`` 这种地址能把云主机的凭据读出来。所以提交时和投递前
   各查一次地址，只放公网 IP、只放白名单端口、不跟随重定向、不走系统代理。
2. **别让人伪造**。收方得能确认"这条请求真是 AgentCode 发的"：整条报文用
   HMAC-SHA256 签名，签名覆盖时间戳（防重放）和正文（防篡改）。
3. **别发丢**。网络抖一下、收方重启一下都是常事：可重试的失败按退避重试，
   状态落库，进程重启之后接着发；4xx 这类"重发也没用"的错就直接放弃，
   如实记下来，不假装成功。
"""

from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import socket
import ssl
import time
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import quote, urlsplit

from agentcode.accounts import AccountStore
from agentcode.config import Settings
from agentcode.core.errors import AgentCodeError
from agentcode.metrics import CALLBACKS

#: 事件名：以后可能还有别的（比如 run.started），收方按它分流
EVENT_RUN_FINISHED = "run.finished"

#: 回调的交付状态（也存在 ``api_runs.callback_status`` 里）
STATUS_NONE = "none"
STATUS_PENDING = "pending"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

#: 默认只放标准 Web 端口；要放别的端口得显式配 ``AGENT_CALLBACK_PORTS``
DEFAULT_PORTS: tuple[int, ...] = (80, 443)

#: 重试退避（秒）：第 1 次失败等 5 秒、第 2 次 30 秒……最后一个给以后的重试兜底
RETRY_DELAYS: tuple[float, ...] = (5.0, 30.0, 120.0, 600.0)

#: 回调地址最长多少字符（再长就是来捣乱的）
MAX_URL_LENGTH = 2000

USER_AGENT = "AgentCode-Callback/1"

#: 投递函数：``(url, body, headers) -> HTTP 状态码``。测试注入一个假的
Poster = Callable[[str, bytes, dict[str, str]], int]
#: 域名解析函数：``host -> [ip, ...]``。测试注入，免得依赖真实 DNS
Resolver = Callable[[str], list[str]]


# ------------------------------------------------------------------ 地址校验


def parse_ports(value: Any) -> tuple[int, ...]:
    """把 ``"80,443,8443"`` 这种配置解析成端口元组；空/全非法就回默认值。"""
    ports: list[int] = []
    for part in str(value or "").replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            port = int(part)
        except ValueError:
            continue
        if 0 < port <= 65535:
            ports.append(port)
    return tuple(ports) or DEFAULT_PORTS


def callback_ports(settings: Settings) -> tuple[int, ...]:
    """当前配置允许的回调端口。"""
    return parse_ports(getattr(settings, "callback_ports", "") or "")


def _resolve_host(host: str) -> list[str]:
    """把域名解析成 IP 列表（去重，顺序稳定）。"""
    infos = socket.getaddrinfo(host, None)
    return sorted({str(info[4][0]) for info in infos})


def _looks_private(address: str) -> bool:
    """这个 IP 是不是"不该被回调打过去"的那一类。"""
    try:
        ip = ipaddress.ip_address(str(address))
    except ValueError:
        return True  # 解析不出来的东西一律当危险处理
    # IPv4-mapped IPv6（::ffff:127.0.0.1）要拆回 IPv4 再判断
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not ip.is_global


def validate_callback_url(
    url: str,
    *,
    allow_private: bool = False,
    ports: tuple[int, ...] = DEFAULT_PORTS,
    resolver: Resolver | None = None,
) -> str:
    """校验一个回调地址，通过就返回它，不通过抛 :class:`AgentCodeError`。

    ``allow_private=True`` 是给本地开发和测试的口子（回调自己机器上的接收端），
    默认关着——公网部署打开它等于把服务变成内网扫描器。
    """
    text = str(url or "").strip()
    if not text:
        raise AgentCodeError("回调地址不能为空。")
    if len(text) > MAX_URL_LENGTH:
        raise AgentCodeError(f"回调地址太长了（超过 {MAX_URL_LENGTH} 个字符）。")

    parts = urlsplit(text)
    if parts.scheme not in ("http", "https"):
        raise AgentCodeError(
            f"回调地址只支持 http/https，当前是：{parts.scheme or '（空）'}。"
        )
    if parts.username or parts.password:
        raise AgentCodeError("回调地址里不要带用户名和密码（https://user:pass@host/ 这种）。")
    host = parts.hostname or ""
    if not host:
        raise AgentCodeError("回调地址缺少主机名。")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise AgentCodeError(f"回调地址的端口不合法：{text}。") from exc

    allowed = tuple(ports) or DEFAULT_PORTS
    if port not in allowed:
        raise AgentCodeError(
            f"回调端口 {port} 不在允许范围内（{'、'.join(str(item) for item in allowed)}）。"
            "要放别的端口请配置 AGENT_CALLBACK_PORTS。"
        )

    if not allow_private:
        for address in _resolve(host, resolver):
            if _looks_private(address):
                raise AgentCodeError(
                    f"回调地址指向内网或本机地址（{host} → {address}），出于安全考虑不接受。"
                    "本地自用可以设 AGENT_CALLBACK_ALLOW_PRIVATE=true。"
                )
    return text


def _resolve(host: str, resolver: Resolver | None = None) -> list[str]:
    """拿到主机名对应的 IP：字面量 IP 直接用，域名才去解析。"""
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        pass
    resolve = resolver or _resolve_host
    try:
        addresses = [str(item) for item in resolve(host)]
    except OSError as exc:
        raise AgentCodeError(f"回调地址的域名解析不了：{host}（{exc}）。") from exc
    if not addresses:
        raise AgentCodeError(f"回调地址的域名解析不了：{host}。")
    return addresses


def safe_target(url: str) -> str:
    """审计里用的安全写法：只留 ``scheme://host:port``。

    **路径和 query 都要丢**：webhook 地址里到处是密钥，Slack 那种是塞在路径里
    （``/services/T00/B00/xxxx``），另一些是塞在 query 里（``?token=...``）。
    审计日志不能成为泄露源。
    """
    parts = urlsplit(str(url or ""))
    return f"{parts.scheme}://{parts.netloc}"


# ------------------------------------------------------------------ 签名


def callback_signature(secret: str, timestamp: int | str, body: bytes) -> str:
    """HMAC-SHA256，签名对象是 ``时间戳 + "." + 正文``。

    时间戳一起签，收方才能拒绝"很久以前的合法请求"被原样重放。
    """
    message = f"{timestamp}.".encode("utf-8") + bytes(body)
    return hmac.new(str(secret).encode("utf-8"), message, hashlib.sha256).hexdigest()


# ------------------------------------------------------------------ 投递


def _make_default_poster(settings: Settings) -> Poster:
    """真实的投递函数。

    直接用 :mod:`http.client` 而不是 ``urllib``，图三件事：

    - **不跟随重定向**：跳过去的目标没经过地址校验，等于绕开 SSRF 防护；
    - **不碰系统代理**：代理会让请求绕过我们的地址检查（``urllib`` 默认会读
      ``http_proxy`` 环境变量）；
    - **头名原样发出**：文档里写的是 ``X-AgentCode-Signature``，发出去就该是它。

    每次投递前再查一遍地址：域名解析结果是会变的（DNS 漂移/投毒）。
    """
    timeout = float(getattr(settings, "callback_timeout", 10.0) or 10.0)
    allow_private = bool(getattr(settings, "callback_allow_private", False))
    ports = callback_ports(settings)
    ssl_context = ssl.create_default_context()

    def post(url: str, body: bytes, headers: dict[str, str]) -> int:
        target = validate_callback_url(url, allow_private=allow_private, ports=ports)
        parts = urlsplit(target)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        path = quote(parts.path or "/", safe="/%?=&+:@!$'()*,;~-._")
        if parts.query:
            path = f"{path}?{parts.query}"
        if parts.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, port, timeout=timeout, context=ssl_context
            )
        else:
            connection = http.client.HTTPConnection(host, port, timeout=timeout)
        try:
            connection.request("POST", path, body=body, headers=headers)
            response = connection.getresponse()
            return int(response.status)  # 正文不读：我们只要状态码
        finally:
            connection.close()

    return post


def callback_payload(record: dict[str, Any]) -> dict[str, Any]:
    """回调正文：和 ``GET /v1/runs/<id>`` 一份数据，另加一个事件名。

    调用方不该为了"收到回调"再写一套解析逻辑。
    """
    result = dict(record.get("result") or {})
    status = str(record.get("status") or "running")
    return {
        "event": EVENT_RUN_FINISHED,
        "run_id": record.get("run_id") or "",
        "status": status,
        "success": status == "succeeded",
        "agent": record.get("agent") or "",
        "task": record.get("task") or "",
        "session_id": record.get("session_id") or "",
        "answer": result.get("answer", ""),
        "artifacts": result.get("artifacts", []),
        "usage": result.get("usage", {}),
        "elapsed_ms": result.get("elapsed_ms", 0),
        "error": record.get("error") or result.get("error") or "",
        "created_at": record.get("created_at") or "",
        "finished_at": record.get("finished_at") or "",
    }


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(float(moment), tz=timezone.utc).isoformat(timespec="seconds")


def _retry_delay(attempt: int) -> float:
    """第 ``attempt`` 次失败之后等多久再试。"""
    index = min(max(0, int(attempt) - 1), len(RETRY_DELAYS) - 1)
    return RETRY_DELAYS[index]


def _retryable_http_status(code: int) -> bool:
    """收方的错要重试，收方说"请求不对"就别重试。"""
    return code >= 500 or code in (408, 429)


class CallbackSender:
    """发一条回调。一次调用只投递一次，重试交给 :func:`deliver_due` 排期。"""

    def __init__(
        self,
        store: AccountStore,
        settings: Settings,
        *,
        poster: Poster | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self.store = store
        self.settings = settings
        # 没配 AGENT_CALLBACK_SECRET 就回落到登录 cookie 用的那个密钥；
        # 两个都没有时，带回调的请求在提交阶段就会被拒（见 web/server.py）
        self.secret = str(
            getattr(settings, "callback_secret", "") or getattr(settings, "secret_key", "") or ""
        )
        self.max_attempts = max(1, int(getattr(settings, "callback_max_attempts", 4) or 4))
        self._resolver = resolver
        self._poster = poster or _make_default_poster(settings)

    # -------------------------------------------------------------- 主入口

    def send(self, run_id: str, *, now: float | None = None) -> dict[str, Any]:
        """投递一次，返回结果字典（``delivered`` / ``status`` / ``error``）。"""
        record = self.store.api_run(run_id)
        if record is None:
            return {"delivered": False, "status": STATUS_NONE, "error": f"没有这个任务：{run_id}"}
        url = str(record.get("callback_url") or "")
        status = str(record.get("callback_status") or STATUS_NONE)
        attempts = int(record.get("callback_attempts") or 0)
        if not url:
            return {"delivered": False, "status": STATUS_NONE, "reason": "这个任务没有配回调"}
        if status == STATUS_SUCCEEDED:
            return {"delivered": False, "status": STATUS_SUCCEEDED, "reason": "已经发过了"}
        if status == STATUS_FAILED:
            return {"delivered": False, "status": STATUS_FAILED, "reason": "之前已经放弃"}
        if str(record.get("status") or "") == "running":
            return {"delivered": False, "status": STATUS_PENDING, "reason": "任务还没跑完"}
        if not self.secret:
            return self._give_up(
                run_id,
                attempt=attempts,
                error="服务端没配回调签名密钥（AGENT_CALLBACK_SECRET 或 AGENT_SECRET_KEY）。",
            )

        moment = time.time() if now is None else float(now)
        attempt = attempts + 1
        body = json.dumps(
            callback_payload(record), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        timestamp = int(moment)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": USER_AGENT,
            "X-AgentCode-Event": EVENT_RUN_FINISHED,
            "X-AgentCode-Delivery": str(record.get("run_id") or run_id),
            "X-AgentCode-Timestamp": str(timestamp),
            "X-AgentCode-Signature": "sha256=" + callback_signature(self.secret, timestamp, body),
        }

        try:
            code = int(self._poster(url, body, headers))
        except Exception as exc:  # noqa: BLE001 - 网络问题一律可重试，结论由下面统一记
            return self._retry_or_give_up(
                record, attempt=attempt, moment=moment, error=f"连接失败：{exc}"
            )

        if 200 <= code < 300:
            self.store.record_callback_attempt(
                run_id, status=STATUS_SUCCEEDED, attempts=attempt, delivered_at=_iso(moment)
            )
            CALLBACKS.inc(result="ok")
            self._audit(record, result="ok", detail={"http_status": code, "attempts": attempt})
            return {
                "delivered": True,
                "status": STATUS_SUCCEEDED,
                "http_status": code,
                "attempts": attempt,
            }

        error = f"收方返回 {code}"
        if _retryable_http_status(code):
            return self._retry_or_give_up(record, attempt=attempt, moment=moment, error=error)
        return self._give_up(run_id, attempt=attempt, error=error, record=record)

    # -------------------------------------------------------------- 内部

    def _retry_or_give_up(
        self,
        record: dict[str, Any],
        *,
        attempt: int,
        moment: float,
        error: str,
    ) -> dict[str, Any]:
        if attempt >= self.max_attempts:
            return self._give_up(
                str(record.get("run_id") or ""),
                attempt=attempt,
                error=f"{error}，已放弃（共 {attempt} 次）",
                record=record,
            )
        next_at = moment + _retry_delay(attempt)
        self.store.record_callback_attempt(
            str(record.get("run_id") or ""),
            status=STATUS_PENDING,
            attempts=attempt,
            error=error,
            next_at=_iso(next_at),
        )
        CALLBACKS.inc(result="retry")
        return {
            "delivered": False,
            "status": STATUS_PENDING,
            "error": error,
            "attempts": attempt,
            "next_at": _iso(next_at),
        }

    def _give_up(
        self,
        run_id: str,
        *,
        attempt: int,
        error: str,
        record: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.store.record_callback_attempt(
            run_id, status=STATUS_FAILED, attempts=attempt, error=error
        )
        CALLBACKS.inc(result="failed")
        self._audit(record, result="failed", detail={"error": error, "attempts": attempt})
        return {
            "delivered": False,
            "status": STATUS_FAILED,
            "error": error,
            "attempts": attempt,
        }

    def _audit(self, record: dict[str, Any] | None, *, result: str, detail: dict[str, Any]) -> None:
        """审计是旁路：写不进去只打一行警告，不影响投递结果。"""
        if record is None:
            return
        from agentcode import audit as AUDIT
        from agentcode.audit import AuditLog

        account = self.store.get_by_id(str(record.get("account_id") or ""))
        AuditLog(self.store).record(
            AUDIT.API_CALLBACK,
            account=account,
            target=safe_target(str(record.get("callback_url") or "")),
            result=result,
            detail={"run_id": record.get("run_id") or "", **detail},
        )


def deliver_due(
    store: AccountStore,
    settings: Settings,
    *,
    sender: CallbackSender | None = None,
    limit: int = 20,
    now: float | None = None,
) -> dict[str, int]:
    """把到期的回调各投递一次，返回统计。"""
    worker = sender or CallbackSender(store, settings)
    moment = time.time() if now is None else float(now)
    due = store.due_callbacks(now=moment, limit=limit)
    delivered = failed = 0
    for record in due:
        outcome = worker.send(str(record.get("run_id") or ""), now=moment)
        if outcome.get("delivered"):
            delivered += 1
        elif outcome.get("status") == STATUS_FAILED:
            failed += 1
    return {"due": len(due), "delivered": delivered, "failed": failed}


def run_callback_loop(
    store: AccountStore, settings: Settings, *, interval_seconds: float = 5.0
) -> None:
    """后台循环：每几秒把到期的回调发一次。

    为什么用循环而不是"跑完就在线程里发一次"：循环只认库里的状态，
    进程重启后待发的回调照样会被捡起来——线程里的那点状态一重启就没了。
    """
    while True:
        try:
            report = deliver_due(store, settings)
            if report["delivered"] or report["failed"]:
                print(
                    f"任务完成回调：投递成功 {report['delivered']} 个，"
                    f"放弃 {report['failed']} 个"
                )
        except Exception as exc:  # noqa: BLE001 - 后台任务不能把服务带下去
            print(f"任务完成回调投递失败（服务继续跑）：{exc}")
        time.sleep(max(0.05, float(interval_seconds)))
