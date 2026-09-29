# AgentCode 代码执行沙箱镜像
#
# 为什么需要它：官方的 python:slim 镜像里没有 pytest，
# 而容器运行时是断网的（--network none），装不了包。
# 所以把测试依赖在**构建期**烤进镜像，运行期依旧完全断网。
#
# 构建（默认从 PyPI 装依赖）：
#   docker build -t agentcode-sandbox:1.0 docker/sandbox
#
# 国内网络可以从清华镜像装：
#   docker build -t agentcode-sandbox:1.0 \
#     --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple docker/sandbox
FROM python:3.13-slim

# 构建期用哪个 pip 源；运行期不需要网络
ARG PIP_INDEX_URL=https://pypi.org/simple

# 沙箱里要用的测试依赖（构建进镜像，运行时不联网）
RUN pip install --no-cache-dir --index-url "${PIP_INDEX_URL}" pytest

# 让 python 输出不缓冲，方便实时看到 stdout
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /workspace
