"""零依赖网页服务：标准库 http.server + Server-Sent Events。

只监听本机地址，不做鉴权；请勿把它暴露到公网。
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agentcode import agents  # noqa: F401  导入即注册内置智能体
from agentcode.accounts import Account, AccountStore
from agentcode.config import (
    DEFAULT_MAX_SESSIONS,
    DEFAULT_DAILY_TOKEN_LIMIT,
    DEFAULT_DB_PATH,
    Settings,
)
from agentcode.core.registry import default_registry
from agentcode.web.auth import build_cookie, clear_cookie, create_token, read_cookie, read_token
from agentcode.web.limits import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_PER_MINUTE,
    UsageGuard,
)
from agentcode.web.runs import RunRegistry
from agentcode.tools import (
    ToolRegistry,
    register_builtin_tools,
    register_code_tools,
    register_demo_tools,
)
from agentcode.tools.code import resolve_in_root
from agentcode.web.sessions import SessionStore, default_session_dir

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 64 * 1024
#: 单个文件的内容最多返回这么多字符（够看代码，又不至于撑爆前端）
MAX_FILE_CHARS = 20000
#: 不需要登录也能访问的路径（登录页本身是静态资源）
PUBLIC_PATHS = {"/healthz", "/api/login", "/api/me", "/api/logout"}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
}


def is_port_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 0.5) -> bool:
    """探测本机端口是否已经有服务在监听。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _tool_list(mock: bool, settings: Settings | None = None) -> list[dict[str, Any]]:
    """列出某种模式下的可用工具（只做清单，不创建工作目录）。"""
    registry = ToolRegistry()
    active_settings = settings or Settings.from_env()
    register_builtin_tools(
        registry,
        include_search=not mock,
        serpapi_key=active_settings.serpapi_key,
    )
    if mock:
        register_demo_tools(registry)
    else:
        register_code_tools(
            registry,
            root=active_settings.code_root,
            timeout=active_settings.code_timeout,
            output_limit=active_settings.code_output_limit,
            create=False,
        )
    return [registry.get(name).to_dict() for name in registry.names()]  # type: ignore[union-attr]


def agents_payload(settings: Settings | None = None) -> dict[str, Any]:
    """``GET /api/agents`` 的响应内容。"""
    return {
        "agents": [
            {"name": name, "description": default_registry.description_of(name)}
            for name in default_registry.names()
        ],
        "tools": {"mock": _tool_list(True), "real": _tool_list(False, settings)},
    }


def config_payload(settings: Settings) -> dict[str, Any]:
    """``GET /api/config`` 的响应内容（密钥已脱敏）。"""
    masked = settings.masked()
    missing = settings.missing_keys()
    return {
        "model": settings.model,
        "base_url": settings.base_url,
        "api_key": masked["LLM_API_KEY"],
        "serpapi_configured": bool(settings.serpapi_key),
        "max_steps": settings.max_steps,
        "coding_steps": settings.coding_steps,
        "timeout": settings.timeout,
        "memory_turns": settings.memory_turns,
        "max_sessions": settings.max_sessions,
        "code_root": settings.code_root,
        "env_file": settings.env_file,
        "missing_keys": missing,
        "ready": not missing,
    }


class AgentCodeRequestHandler(BaseHTTPRequestHandler):
    """处理静态页面与接口。"""

    server_version = "AgentCodeWeb/0.1"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- 基础设施

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - 覆盖父类签名
        """默认静音访问日志，``--verbose`` 时打印到标准输出。"""
        if not getattr(self.server, "quiet", True):
            super().log_message(format, *args)

    def _begin_request(self) -> None:
        """记录请求起点与请求 id，供结构化日志使用。"""
        self._started = time.perf_counter()
        self._request_id = uuid.uuid4().hex[:8]
        self._account = self._current_account()

    def _current_account(self) -> Account | None:
        """从签名 cookie 解析当前账号。"""
        token = read_cookie(self.headers.get("Cookie"))
        if not token:
            return None
        account_id = read_token(token, self.server.secret_key)  # type: ignore[attr-defined]
        if not account_id:
            return None
        account = self.server.accounts.get_by_id(account_id)  # type: ignore[attr-defined]
        return account if account and account.is_active else None

    def _log_access(self, status: int) -> None:
        """输出一行 JSON 访问日志：请求 id、用户、路径、状态、耗时。"""
        if not getattr(self.server, "access_log", False):
            return
        account = getattr(self, "_account", None)
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "request_id": getattr(self, "_request_id", "-"),
            "method": self.command or "-",
            "path": self.path.split("?", 1)[0],
            "status": status,
            "ms": round((time.perf_counter() - getattr(self, "_started", time.perf_counter())) * 1000, 1),
            "user": account.name if account else "",
            "ip": self.client_address[0] if self.client_address else "",
        }
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    def _store(self) -> SessionStore:
        """取当前用户的会话仓库——每个用户一个独立目录，天然隔离。"""
        if not getattr(self.server, "require_auth", True):
            return self.server.sessions  # type: ignore[attr-defined]
        account = getattr(self, "_account", None)
        if account is None:
            raise PermissionError("未登录")
        return self.server.store_for(account.id)  # type: ignore[attr-defined]

    def _auth_ok(self) -> bool:
        """需要登录的接口统一在这里拦：未登录直接回 401 并返回 False。"""
        if not getattr(self.server, "require_auth", True):
            return True  # 免登录模式（本地自用）
        if getattr(self, "_account", None) is not None:
            return True
        self._send_json({"error": "请先登录。"}, status=401)
        return False

    def _settings(self) -> Settings:
        """每次请求都重新读取环境，保证改完 .env 刷新页面即可生效。"""
        return Settings.from_env(env_file=getattr(self.server, "env_file", None))

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            self.wfile.write(body)
        self._log_access(status)

    def _send_json(
        self,
        payload: dict[str, Any],
        status: int = 200,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)
        self._log_access(status)

    def _client_key(self) -> str:
        """限流用的 key：已登录用账号名，未登录用 IP。"""
        account = getattr(self, "_account", None)
        if account is not None:
            return f"user:{account.name}"
        return f"ip:{self.client_address[0] if self.client_address else 'unknown'}"

    def _reject_by_limit(self, decision: Any) -> None:
        """统一的 429 回应。"""
        headers = {"Retry-After": str(decision.retry_after)} if decision.retry_after else None
        self._send_json({"error": decision.reason}, status=429, extra_headers=headers)

    # -------------------------------------------------------------- 运行状态

    def _registry(self) -> RunRegistry:
        """当前服务进程的运行注册表。"""
        return self.server.registry  # type: ignore[attr-defined]

    def _may_access_run(self, record: Any) -> bool:
        """登录模式下只能看自己的运行；免登录模式不设限。"""
        if not getattr(self.server, "require_auth", True):
            return True
        account = getattr(self, "_account", None)
        if account is None:
            return False
        return record.account_id in (None, account.id)

    def _begin_sse(self) -> None:
        """开始一条 SSE 响应。"""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

    def _write_event(self, event: dict[str, Any]) -> None:
        """按 SSE 协议写一条事件并立刻冲刷。"""
        chunk = (
            f"event: {event['type']}\n"
            f"data: {json.dumps(event.get('data') or {}, ensure_ascii=False)}\n\n"
        )
        self.wfile.write(chunk.encode("utf-8"))
        self.wfile.flush()

    def _stream_run(self, record: Any, from_index: int = 0) -> None:
        """把某个运行的事件流出去。

        浏览器断开只会让**这条订阅**结束，运行本身在注册表里继续跑，
        重新连上来（``/api/run/stream``）就能把漏掉的事件补回来。
        """
        self._begin_sse()
        try:
            self._write_event({"type": "run", "data": record.summary()})
            for event in self._registry().subscribe(record, from_index):
                self._write_event(event)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self._log_access(200)

    def _serve_runs(self) -> None:
        """列出运行记录；带 ``session_id`` 时只看这个会话。"""
        query = parse_qs(urlsplit(self.path).query)
        session_id = (query.get("session_id") or [""])[0]
        account = getattr(self, "_account", None)
        runs = self._registry().list(
            session_id=session_id or None,
            account_id=account.id if account is not None else None,
        )
        self._send_json({"runs": runs})

    def _serve_run_resume(self) -> None:
        """重连：从指定下标继续把某个运行的事件流出去。"""
        query = parse_qs(urlsplit(self.path).query)
        run_id = (query.get("run_id") or [""])[0]
        try:
            from_index = max(0, int((query.get("from") or ["0"])[0]))
        except (TypeError, ValueError):
            from_index = 0

        record = self._registry().get(run_id)
        if record is None:
            self._send_json(
                {"error": "找不到这个运行，它可能已经结束很久、记录被回收了。"}, status=404
            )
            return
        if not self._may_access_run(record):
            self._send_json({"error": "无权访问这个运行。"}, status=403)
            return
        self._stream_run(record, from_index)

    def _serve_static(self, name: str) -> None:
        """返回静态文件，并阻止路径穿越。"""
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR not in target.parents or not target.is_file():
            self._send_json({"error": "静态资源不存在。"}, status=404)
            return
        content_type = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), content_type)

    def _read_json(self) -> dict[str, Any] | None:
        """读取并解析请求体，失败时直接返回 400。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self._send_json({"error": "请求长度不合法。"}, status=400)
            return None
        if length <= 0:
            self._send_json({"error": "请求体为空。"}, status=400)
            return None
        if length > MAX_BODY_BYTES:
            self._send_json({"error": "请求体过大。"}, status=413)
            return None
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json({"error": "请求体不是合法的 JSON。"}, status=400)
            return None
        if not isinstance(payload, dict):
            self._send_json({"error": "请求体必须是 JSON 对象。"}, status=400)
            return None
        return payload

    # ----------------------------------------------------------------- 路由

    def do_GET(self) -> None:  # noqa: N802 - 父类约定的方法名
        self._begin_request()
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self._send_json(
                {
                    "status": "ok",
                    "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "accounts": len(self.server.accounts.list()),  # type: ignore[attr-defined]
                }
            )
            return
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        if path == "/api/me":
            account = getattr(self, "_account", None)
            if account is None:
                self._send_json({"error": "未登录。"}, status=401)
                return
            self._send_json({"account": self._account_payload(account)})
            return
        # 其余接口都需要登录
        if path.startswith("/api/") and not self._auth_ok():
            return
        if path.startswith("/api/"):
            decision = self.server.guard.check_request(self._client_key())  # type: ignore[attr-defined]
            if not decision.allowed:
                self._reject_by_limit(decision)
                return
        if path == "/api/agents":
            self._send_json(agents_payload(self._settings()))
            return
        if path == "/api/config":
            self._send_json(config_payload(self._settings()))
            return
        if path == "/api/sessions":
            self._send_json({"sessions": self._store().list()})
            return
        if path == "/api/session":
            store = self._store()
            query = parse_qs(urlsplit(self.path).query)
            session_id = (query.get("id") or [""])[0]
            record = store.record(session_id) if session_id else None
            self._send_json(
                {
                    "session": record.summary() if record is not None else None,
                    "messages": [dict(message) for message in record.messages]
                    if record is not None
                    else [],
                }
            )
            return
        if path == "/api/file":
            self._serve_code_file()
            return
        if path == "/api/runs":
            self._serve_runs()
            return
        if path == "/api/run/stream":
            self._serve_run_resume()
            return
        self._send_json({"error": f"未找到路径 {path}。"}, status=404)

    def _account_payload(self, account: Account) -> dict[str, Any]:
        """给前端的账号信息：名字、套餐、今日额度。"""
        used, calls = self.server.accounts.usage_today(account.id)  # type: ignore[attr-defined]
        return {
            "name": account.name,
            "plan": account.plan,
            "daily_token_limit": account.daily_token_limit,
            "used_today": used,
            "calls_today": calls,
            "remaining": max(0, account.daily_token_limit - used),
        }

    def _serve_code_file(self) -> None:
        """读取代码工作目录里的文本文件，供页面点击查看。"""
        query = parse_qs(urlsplit(self.path).query)
        relative = (query.get("path") or [""])[0]
        if not relative:
            self._send_json({"error": "缺少 path 参数。"}, status=400)
            return

        root = Path(self._settings().code_root).resolve()
        try:
            target = resolve_in_root(root, relative)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        if not target.is_file():
            self._send_json({"error": f"文件不存在：{relative}"}, status=404)
            return

        try:
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            self._send_json({"error": f"读取失败：{exc}"}, status=500)
            return

        truncated = len(text) > MAX_FILE_CHARS
        self._send_json(
            {
                "path": target.relative_to(root).as_posix(),
                "content": text[:MAX_FILE_CHARS],
                "bytes": target.stat().st_size,
                "truncated": truncated,
            }
        )

    def do_POST(self) -> None:  # noqa: N802 - 父类约定的方法名
        from agentcode.web.runner import run_stream  # 延迟导入，避免循环依赖

        self._begin_request()
        path = self.path.split("?", 1)[0]
        if path not in (
            "/api/login",
            "/api/logout",
            "/api/run",
            "/api/session/reset",
            "/api/sessions/create",
            "/api/sessions/rename",
            "/api/sessions/delete",
        ):
            self._send_json({"error": f"未找到路径 {path}。"}, status=404)
            return

        if path == "/api/login":
            payload = self._read_json()
            if payload is None:
                return
            # 登录单独限流，防密码暴力试探
            decision = self.server.login_guard.check_request(  # type: ignore[attr-defined]
                f"login:{self.client_address[0] if self.client_address else 'unknown'}"
            )
            if not decision.allowed:
                self._reject_by_limit(decision)
                return
            self._handle_login(payload)
            return

        if path == "/api/logout":
            # 登出不需要请求体：必须在读 body 之前处理，否则空 body 会被判 400
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", clear_cookie())
            body = json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self._log_access(200)
            return

        payload = self._read_json()
        if payload is None:
            return

        if not self._auth_ok():
            return
        account = getattr(self, "_account", None)

        session_store: SessionStore = self._store()
        raw_session = str(payload.get("session_id") or "").strip()
        session_id = raw_session or None

        if path == "/api/session/reset":
            removed = session_store.reset(session_id) if session_id else False
            self._send_json({"reset": removed, "session_id": session_id})
            return

        if path == "/api/sessions/create":
            record = session_store.create(name=payload.get("name"))
            self._send_json({"session": record.summary()})
            return

        if path == "/api/sessions/rename":
            renamed = session_store.rename(session_id or "", payload.get("name"))
            record = session_store.record(session_id or "")
            self._send_json(
                {
                    "renamed": renamed,
                    "session": record.summary() if record is not None else None,
                }
            )
            return

        if path == "/api/sessions/delete":
            self._send_json({"deleted": session_store.delete(session_id or "")})
            return

        task = str(payload.get("task") or "").strip()
        if not task:
            self._send_json({"error": "任务不能为空，请输入要交给智能体的问题。"}, status=400)
            return

        agent_name = str(payload.get("agent") or "react")
        requested_mode = payload.get("llm")
        llm_mode = (
            requested_mode
            if requested_mode in ("mock", "openai")
            else getattr(self.server, "llm_mode", "mock")
        )
        raw_max_steps = payload.get("max_steps")
        max_steps = (
            int(raw_max_steps) if isinstance(raw_max_steps, int) and raw_max_steps > 0 else None
        )

        settings = self._settings()
        accounts: AccountStore = self.server.accounts  # type: ignore[attr-defined]
        if account is not None:
            allowed, reason = accounts.check_quota(account)
            if not allowed:
                self._send_json({"error": reason}, status=402)
                return

        # 频率 + 并发闸门：一次任务会调用模型十几次，只在开头查每日额度挡不住并发
        guard_key = self._client_key()
        decision = self.server.guard.acquire(guard_key)  # type: ignore[attr-defined]
        if not decision.allowed:
            self._reject_by_limit(decision)
            return

        def on_finish(finished: Any) -> None:
            """运行结束时的收尾：结算用量、放掉并发闸门。

            挂在这里而不是请求循环里——用户刷新页面把连接掐了，
            额度照样要扣，闸门也照样要放，不然这个用户就被自己锁死了。
            """
            if account is not None:
                answer = finished.answer_event() or {}
                usage = (answer.get("data") or {}).get("usage") or {}
                accounts.record_usage(account.id, int(usage.get("total_tokens") or 0), calls=1)
            self.server.guard.release(guard_key)  # type: ignore[attr-defined]

        record = self._registry().start(
            lambda: run_stream(
                agent_name,
                task,
                llm_mode=llm_mode,
                max_steps=max_steps,
                settings=settings,
                session_store=session_store,
                session_id=session_id,
            ),
            agent=agent_name,
            task=task,
            session_id=session_id,
            account_id=account.id if account is not None else None,
            on_finish=on_finish,
        )
        # 首帧就把 run_id 交给前端：连接断了它能凭这个从断点续上
        self._stream_run(record)

    def _handle_login(self, payload: dict[str, Any]) -> None:
        """校验账号密码并下发签名 cookie。"""
        accounts: AccountStore = self.server.accounts  # type: ignore[attr-defined]
        name = str(payload.get("name") or "").strip()
        password = str(payload.get("password") or "")
        account = accounts.verify(name, password)
        if account is None:
            # 不区分"账号不存在"与"密码错误"，避免账号枚举
            self._send_json({"error": "账号或密码不正确。"}, status=401)
            return

        token = create_token(account.id, self.server.secret_key)  # type: ignore[attr-defined]
        body = json.dumps(
            {"account": self._account_payload(account)}, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Set-Cookie", build_cookie(token))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self._account = account
        self._log_access(200)


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    llm_mode: str = "mock",
    env_file: str | None = None,
    quiet: bool = True,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    session_dir: str | None = None,
    accounts: AccountStore | None = None,
    secret_key: str | None = None,
    require_auth: bool = True,
    access_log: bool = False,
    rate_limit_per_minute: int = DEFAULT_PER_MINUTE,
    max_concurrent_runs: int = DEFAULT_MAX_CONCURRENT,
) -> ThreadingHTTPServer:
    """创建（但不启动）网页服务，``port=0`` 时由系统分配端口。

    ``require_auth=False`` 表示免登录（本地自用/自动化测试）；
    面向公网时必须保持默认的 ``True``。
    """
    server = ThreadingHTTPServer((host, port), AgentCodeRequestHandler)
    server.daemon_threads = True
    server.llm_mode = llm_mode  # type: ignore[attr-defined]
    server.env_file = env_file  # type: ignore[attr-defined]
    server.quiet = quiet  # type: ignore[attr-defined]
    server.require_auth = require_auth  # type: ignore[attr-defined]
    server.access_log = access_log  # type: ignore[attr-defined]
    server.guard = UsageGuard(  # type: ignore[attr-defined]
        per_minute=rate_limit_per_minute, max_concurrent=max_concurrent_runs
    )
    #: 登录接口单独限流（比常规接口更严，防暴力试探）
    server.login_guard = UsageGuard(per_minute=10, max_concurrent=50)  # type: ignore[attr-defined]
    #: 运行注册表：让任务的生命周期独立于浏览器的连接（刷新/断网都能续上）
    server.registry = RunRegistry()  # type: ignore[attr-defined]
    base_dir = Path(session_dir or default_session_dir())
    server.session_base_dir = base_dir  # type: ignore[attr-defined]
    server.max_sessions = max_sessions  # type: ignore[attr-defined]
    # 免登录模式下的共享仓库（也用于把老接口跑通）
    server.sessions = SessionStore(directory=base_dir, max_sessions=max_sessions)  # type: ignore[attr-defined]
    server.accounts = accounts or AccountStore(  # type: ignore[attr-defined]
        os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH
    )
    server.secret_key = secret_key or os.environ.get("AGENT_SECRET_KEY") or uuid.uuid4().hex
    server.store_lock = threading.Lock()  # type: ignore[attr-defined]
    server.user_stores = {}  # type: ignore[attr-defined]

    def store_for(account_id: str) -> SessionStore:
        """每个用户一个独立目录的会话仓库（懒加载 + 缓存）。"""
        with server.store_lock:  # type: ignore[attr-defined]
            store = server.user_stores.get(account_id)  # type: ignore[attr-defined]
            if store is None:
                store = SessionStore(
                    directory=base_dir / account_id, max_sessions=max_sessions
                )
                server.user_stores[account_id] = store  # type: ignore[attr-defined]
            return store

    server.store_for = store_for  # type: ignore[attr-defined]
    return server


def serve(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    llm_mode: str = "mock",
    env_file: str | None = None,
    open_browser: bool = False,
    quiet: bool = True,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    session_dir: str | None = None,
    require_auth: bool = True,
    access_log: bool = True,
) -> None:
    """启动网页服务并阻塞，直到用户按 Ctrl+C。"""
    accounts = AccountStore(os.environ.get("AGENT_DB_PATH") or DEFAULT_DB_PATH)
    settings_for_limits = Settings.from_env(env_file=env_file)
    boot_password: str | None = None
    if require_auth and not accounts.list():
        # 第一次启动时自动建一个管理员账号，只在这里打印一次密码（类似 Jupyter 的 token）
        boot_password = secrets.token_urlsafe(9)
        accounts.create("admin", boot_password, plan="owner")

    try:
        server = create_server(
            host,
            port,
            llm_mode=llm_mode,
            env_file=env_file,
            quiet=quiet,
            max_sessions=max_sessions,
            session_dir=session_dir,
            accounts=accounts,
            require_auth=require_auth,
            access_log=access_log,
            rate_limit_per_minute=settings_for_limits.rate_limit_per_minute,
            max_concurrent_runs=settings_for_limits.max_concurrent_runs,
        )
    except OSError as exc:
        print(f"启动失败：{host}:{port} 无法监听（{exc}）。")
        print(f"端口可能已被占用，请换一个端口，例如 --port {port + 1}。")
        return

    url = f"http://{host}:{server.server_port}"
    print(f"AgentCode 网页已启动：{url}")
    print(f"默认模型模式：{llm_mode}（页面上不显示模式，一切走这个设置）")
    print(
        f"限流：每人每分钟 {settings_for_limits.rate_limit_per_minute} 次请求，"
        f"同时最多 {settings_for_limits.max_concurrent_runs} 个任务；"
        f"单次任务 token 预算 {settings_for_limits.run_token_budget}"
    )
    if require_auth:
        if boot_password:
            print("=" * 56)
            print(f"已创建初始账号　用户名：admin　密码：{boot_password}")
            print("请立刻记下这个密码，它只显示这一次。")
            print("=" * 56)
        print("登录后才能使用；需要给他人开账号：agentcode user add <名字>")
    else:
        print("⚠ 免登录模式：任何能访问这个端口的人都能用你的模型额度，仅限本机自用。")
    print("这个窗口就是服务本身：关闭窗口或按 Ctrl+C 即可停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务。")
    finally:
        server.server_close()
