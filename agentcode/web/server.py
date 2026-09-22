"""零依赖网页服务：标准库 http.server + Server-Sent Events。

只监听本机地址，不做鉴权；请勿把它暴露到公网。
"""

from __future__ import annotations

import json
import socket
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from agentcode import agents  # noqa: F401  导入即注册内置智能体
from agentcode.config import DEFAULT_MAX_SESSIONS, Settings
from agentcode.core.registry import default_registry
from agentcode.tools import (
    ToolRegistry,
    register_builtin_tools,
    register_code_tools,
    register_demo_tools,
)
from agentcode.web.sessions import SessionStore, default_session_dir

STATIC_DIR = Path(__file__).resolve().parent / "static"
MAX_BODY_BYTES = 64 * 1024
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
    register_builtin_tools(registry, include_search=not mock)
    if mock:
        register_demo_tools(registry)
    else:
        active = settings or Settings.from_env()
        register_code_tools(
            registry,
            root=active.code_root,
            timeout=active.code_timeout,
            output_limit=active.code_output_limit,
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

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

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
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._serve_static("index.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/") :])
            return
        if path == "/api/agents":
            self._send_json(agents_payload(self._settings()))
            return
        if path == "/api/config":
            self._send_json(config_payload(self._settings()))
            return
        if path == "/api/sessions":
            store: SessionStore = self.server.sessions  # type: ignore[attr-defined]
            self._send_json({"sessions": store.list()})
            return
        if path == "/api/session":
            store = self.server.sessions  # type: ignore[attr-defined]
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
        self._send_json({"error": f"未找到路径 {path}。"}, status=404)

    def do_POST(self) -> None:  # noqa: N802 - 父类约定的方法名
        from agentcode.web.runner import run_stream  # 延迟导入，避免循环依赖

        path = self.path.split("?", 1)[0]
        if path not in (
            "/api/run",
            "/api/session/reset",
            "/api/sessions/create",
            "/api/sessions/rename",
            "/api/sessions/delete",
        ):
            self._send_json({"error": f"未找到路径 {path}。"}, status=404)
            return

        payload = self._read_json()
        if payload is None:
            return

        session_store: SessionStore = self.server.sessions  # type: ignore[attr-defined]
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
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()

        try:
            for event in run_stream(
                agent_name,
                task,
                llm_mode=llm_mode,
                max_steps=max_steps,
                settings=settings,
                session_store=session_store,
                session_id=session_id,
            ):
                chunk = (
                    f"event: {event['type']}\n"
                    f"data: {json.dumps(event['data'], ensure_ascii=False)}\n\n"
                )
                self.wfile.write(chunk.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            # 浏览器提前关闭连接（例如用户刷新页面）时静默结束
            pass


def create_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    llm_mode: str = "mock",
    env_file: str | None = None,
    quiet: bool = True,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    session_dir: str | None = None,
) -> ThreadingHTTPServer:
    """创建（但不启动）网页服务，``port=0`` 时由系统分配端口。"""
    server = ThreadingHTTPServer((host, port), AgentCodeRequestHandler)
    server.daemon_threads = True
    server.llm_mode = llm_mode  # type: ignore[attr-defined]
    server.env_file = env_file  # type: ignore[attr-defined]
    server.quiet = quiet  # type: ignore[attr-defined]
    server.sessions = SessionStore(  # type: ignore[attr-defined]
        directory=session_dir or default_session_dir(), max_sessions=max_sessions
    )
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
) -> None:
    """启动网页服务并阻塞，直到用户按 Ctrl+C。"""
    try:
        server = create_server(
            host,
            port,
            llm_mode=llm_mode,
            env_file=env_file,
            quiet=quiet,
            max_sessions=max_sessions,
            session_dir=session_dir,
        )
    except OSError as exc:
        print(f"启动失败：{host}:{port} 无法监听（{exc}）。")
        print(f"端口可能已被占用，请换一个端口，例如 --port {port + 1}。")
        return

    url = f"http://{host}:{server.server_port}"
    print(f"AgentCode 网页已启动：{url}")
    print(f"默认模型模式：{llm_mode}（页面上不显示模式，一切走这个设置）")
    print("同一个页面标签页会自动延续上下文记忆，想重新开始就点页面上的「新会话」。")
    print("这个窗口就是服务本身：关闭窗口或按 Ctrl+C 即可停止。")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止服务。")
    finally:
        server.server_close()
