"""图片输入（离线）：上传 → 随任务发送 → 服务端校验。

图片不进 ``read_file``（那是按 UTF-8 解码的文本工具），而是走消息里的
``image_url`` 内容块。这里只验证协议与校验，不碰真实视觉模型。
"""

from __future__ import annotations

import base64
import http.cookiejar
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

import pytest

from agentcode.accounts import AccountStore
from agentcode.tokens import ApiTokenService
from agentcode.web.server import create_server

#: 1x1 透明 PNG，足够让魔数识别生效
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _Client:
    def __init__(self, base: str) -> None:
        self.base = base
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def _open(self, request: urllib.request.Request) -> tuple[int, dict]:
        try:
            with self.opener.open(request, timeout=30) as response:
                body = response.read().decode("utf-8")
                return response.status, (json.loads(body) if body.startswith("{") else {})
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8")
            return error.code, (json.loads(body) if body.startswith("{") else {})

    def post_json(self, path: str, payload: dict, token: str | None = None) -> tuple[int, dict]:
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            f"{self.base}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )
        return self._open(request)

    def upload(self, filename: str, content: bytes, content_type: str = "application/octet-stream"):
        query = urllib.parse.urlencode({"path": filename})
        request = urllib.request.Request(
            f"{self.base}/api/upload?{query}",
            data=content,
            headers={"Content-Type": content_type},
        )
        return self._open(request)

    def login(self, name: str, password: str = "password123") -> int:
        return self.post_json("/api/login", {"name": name, "password": password})[0]


def _parse_sse(text: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    name = ""
    data: list[str] = []
    for line in text.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :].strip()
        elif line.startswith("data: "):
            data.append(line[len("data: ") :])
        elif not line.strip() and name and data:
            events.append((name, json.loads("\n".join(data))))
            name, data = "", []
    return events


@pytest.fixture
def web(tmp_path, monkeypatch):
    accounts = AccountStore(tmp_path / "accounts.db")
    code_root = tmp_path / "sandbox"
    monkeypatch.setenv("AGENT_CODE_ROOT", str(code_root))
    server = create_server(
        host="127.0.0.1",
        port=0,
        llm_mode="mock",
        quiet=True,
        session_dir=str(tmp_path / "sessions"),
        accounts=accounts,
    )
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        yield base, accounts, code_root
    finally:
        server.shutdown()
        server.server_close()


def _login(web):
    base, accounts, code_root = web
    accounts.create("alice", "password123")
    client = _Client(base)
    client.login("alice")
    return client, accounts.get("alice").id, code_root / accounts.get("alice").id


# ------------------------------------------------------------------ 上传


def test_upload_png_is_stored_and_marked_as_image(web):
    client, _, root = _login(web)

    status, payload = client.upload("shot.png", PNG_BYTES, "image/png")

    assert status == 200
    assert payload["kind"] == "image"
    assert payload["media_type"] == "image/png"
    assert (root / "shot.png").read_bytes() == PNG_BYTES


def test_upload_still_rejects_binary_that_is_not_an_image(web):
    client, _, _ = _login(web)

    status, payload = client.upload("blob.bin", b"\x00\x01\x02\x03")

    assert status == 400
    assert "二进制" in payload["error"]


def test_upload_does_not_trust_the_extension_when_the_bytes_are_text(web):
    client, _, _ = _login(web)

    status, payload = client.upload("fake.png", b"not really a png", "image/png")

    # 按魔数判断：这段字节是合法 UTF-8，就按文本收，不会冒充成图片
    assert status == 200
    assert payload["kind"] == "text"
    assert "media_type" not in payload


# ------------------------------------------------------------------ 运行


def test_run_accepts_images_and_mock_mode_reports_them(web):
    client, _, _ = _login(web)
    client.upload("shot.png", PNG_BYTES, "image/png")

    request = urllib.request.Request(
        f"{web[0]}/api/run",
        data=json.dumps(
            {"agent": "react", "task": "这张图里是什么？", "images": ["shot.png"]}
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with client.opener.open(request, timeout=30) as response:
        text = response.read().decode("utf-8")
    events = _parse_sse(text)
    answers = [data for name, data in events if name == "answer"]

    assert answers
    assert "1 张图片" in answers[-1]["answer"]


def test_run_rejects_an_image_path_outside_the_workspace(web):
    client, _, _ = _login(web)
    client.upload("shot.png", PNG_BYTES, "image/png")

    status, payload = client.post_json(
        "/api/run", {"agent": "react", "task": "看图", "images": ["../shot.png"]}
    )

    assert status == 400
    assert "路径" in payload["error"]


def test_run_rejects_a_text_file_as_an_image(web):
    client, _, _ = _login(web)
    client.upload("notes.txt", b"hello\n")

    status, payload = client.post_json(
        "/api/run", {"agent": "react", "task": "看图", "images": ["notes.txt"]}
    )

    assert status == 400
    assert "图片" in payload["error"]


def test_run_rejects_too_many_images(web):
    client, _, _ = _login(web)
    for index in range(5):
        client.upload(f"shot{index}.png", PNG_BYTES, "image/png")

    status, payload = client.post_json(
        "/api/run",
        {
            "agent": "react",
            "task": "看图",
            "images": [f"shot{index}.png" for index in range(5)],
        },
    )

    assert status == 400
    assert "最多" in payload["error"]


def test_run_rejects_images_when_no_vision_model_is_configured(web, monkeypatch):
    client, _, _ = _login(web)
    client.upload("shot.png", PNG_BYTES, "image/png")
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("LLM_MODEL_ID", "text-only")
    monkeypatch.delenv("AGENT_VISION_PROFILE", raising=False)
    monkeypatch.delenv("AGENT_MODEL_VISION", raising=False)

    status, payload = client.post_json(
        "/api/run",
        {
            "agent": "react",
            "task": "看图",
            "images": ["shot.png"],
            "llm": "openai",
        },
    )

    assert status == 400
    assert "视觉" in payload["error"]


# ------------------------------------------------------------ OpenAI 兼容门面


def _data_url(raw: bytes = PNG_BYTES, media: str = "image/png") -> str:
    return f"data:{media};base64," + base64.b64encode(raw).decode("ascii")


def _token_for(accounts: AccountStore, name: str = "alice") -> str:
    account = accounts.get(name)
    assert account is not None
    _, plaintext = ApiTokenService(accounts).create(account, name="图片测试")
    return plaintext


def test_chat_completions_accepts_a_data_url_image(web):
    client, _, _ = _login(web)
    accounts = web[1]
    token = _token_for(accounts)

    status, payload = client.post_json(
        "/v1/chat/completions",
        {
            "model": "agentcode",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "这张图里是什么？"},
                        {"type": "image_url", "image_url": {"url": _data_url()}},
                    ],
                }
            ],
        },
        token=token,
    )

    assert status == 200, payload
    assert "1 张图片" in payload["choices"][0]["message"]["content"]


def test_chat_completions_refuses_remote_image_urls(web):
    client, _, _ = _login(web)
    accounts = web[1]
    token = _token_for(accounts)

    status, payload = client.post_json(
        "/v1/chat/completions",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                    ],
                }
            ]
        },
        token=token,
    )

    assert status == 400
    assert "data:" in payload["error"]["message"]


def test_chat_completions_rejects_a_data_url_that_is_not_an_image(web):
    client, _, _ = _login(web)
    accounts = web[1]
    token = _token_for(accounts)

    status, payload = client.post_json(
        "/v1/chat/completions",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": _data_url(b"plain text")},
                        }
                    ],
                }
            ]
        },
        token=token,
    )

    assert status == 400
    assert "图片" in payload["error"]["message"]


def test_chat_completions_accepts_an_image_larger_than_the_default_json_limit(web):
    """base64 图片很容易超过 64 KB；chat 接口得有自己的上限。"""
    client, _, _ = _login(web)
    token = _token_for(web[1])
    big_png = PNG_BYTES + b"\x00" * (120 * 1024)

    status, payload = client.post_json(
        "/v1/chat/completions",
        {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "看图"},
                        {"type": "image_url", "image_url": {"url": _data_url(big_png)}},
                    ],
                }
            ]
        },
        token=token,
    )

    assert status == 200, payload
    assert "1 张图片" in payload["choices"][0]["message"]["content"]
