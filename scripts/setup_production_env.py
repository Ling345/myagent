"""给 .env 补上生产相关配置（只追加缺失项，不动已有内容，不打印密钥）。

用法：
    D:\\Anaconda\\python.exe scripts\\setup_production_env.py
"""

from __future__ import annotations

import re
import secrets
import sys
from pathlib import Path

ENV_PATH = Path(r"D:\agent\.env")

ADDITIONS = [
    (
        "AGENT_SECRET_KEY",
        "# 登录 cookie 的签名密钥：固定它，重启服务后用户才不用重新登录",
        lambda: secrets.token_hex(32),
    ),
    (
        "AGENT_ALLOW_CODE_TOOLS",
        "# 面向公网默认禁止执行代码（本地 CLI 不受影响）",
        lambda: "false",
    ),
    (
        "AGENT_DAILY_TOKEN_LIMIT",
        "# 每个账号每天的 token 上限（超额直接拒绝）",
        lambda: "50000",
    ),
]


def main() -> int:
    text = ENV_PATH.read_text(encoding="utf-8")
    existing = set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", text, re.MULTILINE))
    if not text.endswith("\n"):
        text += "\n"

    added: list[str] = []
    for key, comment, factory in ADDITIONS:
        if key in existing:
            continue
        text += f"\n{comment}\n{key}={factory()}\n"
        added.append(key)

    if added:
        ENV_PATH.write_text(text, encoding="utf-8")
    print("已补充：" + ("、".join(added) if added else "无（都已存在）"))
    keys = re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", text, re.MULTILINE)
    print("当前键：" + "、".join(keys))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
