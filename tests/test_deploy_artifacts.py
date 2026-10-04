"""部署产物的护栏测试。

这些不是"跑一遍看看"的测试，而是把**当初为什么这么写**钉下来的回归：
每一条断言背后都对应一个真踩过的坑，改坏了要有人拦住。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# ------------------------------------------------------------------ 应用镜像


def test_app_image_binds_all_interfaces():
    """默认是 127.0.0.1，容器里不改成 0.0.0.0 外面根本进不来。"""
    assert '"--host", "0.0.0.0"' in _read("Dockerfile")


def test_app_image_ships_the_docker_cli():
    """应用自己不起沙箱？不，它要调 docker 去起**兄弟容器**。
    镜像里没有 docker 命令的话，代码执行会在第一次就报"无法执行 docker 命令"。"""
    dockerfile = _read("Dockerfile")
    assert "FROM docker:cli AS dockercli" in dockerfile
    assert "COPY --from=dockercli /usr/local/bin/docker" in dockerfile


def test_app_image_has_a_healthcheck():
    assert "HEALTHCHECK" in _read("Dockerfile")
    assert "/healthz" in _read("Dockerfile")


def test_dockerignore_keeps_secrets_and_runtime_data_out():
    ignore = _read(".dockerignore")
    assert ".env" in ignore
    assert "traces" in ignore
    assert ".git" in ignore


# ------------------------------------------------------------------ compose


def test_compose_mounts_data_with_identical_paths():
    """这条最关键：应用调宿主 Docker 起沙箱时传的是**容器内**的路径，
    而宿主 Docker 按**宿主机**的路径去找。两边不一致的话，沙箱挂载会指向
    一个不存在的目录——容器起得来，里面却看不到任何文件。"""
    compose = _read("docker-compose.yml")
    assert "${AGENTCODE_DATA:-/srv/agentcode/data}:${AGENTCODE_DATA:-/srv/agentcode/data}" in compose


def test_compose_publishes_the_app_to_loopback_only():
    """公网入口交给反向代理，不要把应用裸奔在 0.0.0.0:8000 上。"""
    assert '"127.0.0.1:8000:8000"' in _read("docker-compose.yml")


def test_compose_mounts_the_docker_socket():
    """要起兄弟容器就得能调宿主 Docker——这个取舍写在 docs/deploy.md 里。"""
    compose = _read("docker-compose.yml")
    assert "/var/run/docker.sock:/var/run/docker.sock" in compose
    assert "安全上的取舍" in _read("docs/deploy.md")


def test_compose_overrides_the_local_docker_binary_setting():
    """开发机上可能是 `wsl -d Ubuntu -- docker`，那套在容器里是错的。"""
    assert "AGENT_DOCKER_BINARY: docker" in _read("docker-compose.yml")


def test_compose_env_file_is_optional_and_outside_the_repo_root():
    """根目录放 .env 会把开发时 D:\\agent\\.env 里的本地配置遮掉。"""
    compose = _read("docker-compose.yml")
    assert "deploy/.env" in compose
    assert "required: false" in compose


def test_compose_uses_the_sandbox_image_by_name():
    assert "AGENT_DOCKER_IMAGE: agentcode-sandbox:1.0" in _read("docker-compose.yml")


# ------------------------------------------------------------------ 反向代理


def test_caddy_disables_flush_buffering_for_sse():
    """不关 flush 的话，流式输出会被代理攒着不发，页面要等很久才出东西。"""
    assert "flush_interval -1" in _read("deploy/Caddyfile")


def test_caddy_proxies_to_the_app_service():
    assert "reverse_proxy app:8000" in _read("deploy/Caddyfile")


# ------------------------------------------------------------------ 文档


def test_deploy_doc_covers_the_essentials():
    doc = _read("docs/deploy.md")
    for topic in ("沙箱镜像", "AGENT_SECRET_KEY", "docker.sock", "备份", "HTTPS"):
        assert topic in doc, f"部署文档里应该讲到 {topic}"


def test_deploy_doc_warns_about_path_consistency():
    assert "两边一致" in _read("docs/deploy.md") or "路径必须" in _read("docs/deploy.md")
