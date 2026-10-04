# AgentCode 应用镜像。
#
# 两个关键点，别删：
#
# 1. **必须带 docker CLI**。应用本身不执行用户代码——它是让 `run_python`
#    去起一次性沙箱容器（见 docker/sandbox/Dockerfile）。没有 docker 命令，
#    AGENT_EXECUTION_BACKEND=docker 会在第一次执行时就报"无法执行 docker 命令"。
#    这里从官方 docker:cli 镜像拷贝一个客户端，比在 Debian 里装 docker.io
#    （会连守护进程一起装进来，多两百多兆）干净得多。
#
# 2. **沙箱镜像不在这个文件里**。它是另一个镜像，构建命令见 docs/deploy.md。

FROM docker:cli AS dockercli

FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先只拷依赖清单，改代码时不用重装依赖
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 要能调 docker 起沙箱容器
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

COPY . .

# 数据目录：账号库、会话、代码工作目录都落在这里，compose 会挂一个卷上来
ENV AGENT_DB_PATH=/data/agentcode.db \
    AGENT_WEB_SESSION_DIR=/data/web-sessions \
    AGENT_CODE_ROOT=/data/sandbox \
    AGENT_TRACE_DIR=/data/traces
RUN mkdir -p /data

EXPOSE 8000

# 容器里必须绑 0.0.0.0，否则外面进不来（默认是 127.0.0.1，只给本机自用）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"

CMD ["python", "-m", "agentcode", "web", "--host", "0.0.0.0", "--port", "8000", "--verbose"]
