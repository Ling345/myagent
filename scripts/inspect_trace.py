"""把轨迹 JSON 里每一步的工具、结果摘要打印出来，方便定位卡在哪。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description="查看轨迹 JSON")
    parser.add_argument("path", nargs="?", default="traces/coding-debug.json")
    parser.add_argument("--width", type=int, default=110, help="每条摘要截断长度")
    args = parser.parse_args()

    payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
    usage = payload.get("usage", {})
    print(
        f"成功：{payload.get('success')} | 步数：{len(payload.get('steps', []))} | "
        f"耗时：{payload.get('duration_ms', 0) / 1000:.1f} 秒 | "
        f"模型调用：{usage.get('calls')} 次 | token：约 {usage.get('total_tokens')}"
    )
    if payload.get("error"):
        print(f"结束原因：{payload['error']}")

    for step in payload.get("steps", []):
        detail = step.get("observation") or step.get("error") or step.get("answer") or ""
        detail = " ".join(str(detail).split())[: args.width]
        tool = step.get("tool") or "-"
        print(f"[{step.get('index')}] 工具={tool:<12} {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
