"""把 .env 里的 LLM_MODEL_ID 换成指定模型，并留下可回退的注释。

用法：
    D:\\Anaconda\\python.exe scripts\\switch_model.py deepseek-flash

只改模型那一行，其余内容原样保留；不会打印任何密钥。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_ENV = Path(r"D:\agent\.env")
KEY = "LLM_MODEL_ID"


def main() -> int:
    parser = argparse.ArgumentParser(description="切换 .env 里的模型")
    parser.add_argument("model", help="新的模型名，例如 deepseek-flash")
    parser.add_argument("--env", default=str(DEFAULT_ENV), help=".env 路径")
    args = parser.parse_args()

    env_path = Path(args.env)
    if not env_path.is_file():
        print(f"找不到配置文件：{env_path}")
        return 1

    text = env_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^\s*{KEY}\s*=\s*(.+)$", re.MULTILINE)
    match = pattern.search(text)
    if not match:
        print(f"文件里没有 {KEY}，未做改动。")
        return 1

    old_value = match.group(1).strip()
    if old_value == args.model:
        print(f"当前已经是 {args.model}，无需修改。")
        return 0

    note = f"# 原来的模型（更慢但可能更强）：{old_value}"
    replacement = f"{KEY}={args.model}"
    if note not in text:
        replacement = f"{note}\n{replacement}"
    updated = pattern.sub(replacement, text, count=1)
    env_path.write_text(updated, encoding="utf-8")

    keys = [
        line.split("=", 1)[0].strip()
        for line in updated.splitlines()
        if "=" in line and not line.strip().startswith("#")
    ]
    print(f"{KEY}: {old_value} → {args.model}")
    print(f"文件现有键：{', '.join(keys)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
