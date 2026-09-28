"""把仓库里被 git 跟踪的文件打包成交付压缩包（默认排除密钥与运行产物）。

用法：
    D:\\Anaconda\\python.exe scripts\\package_submission.py --name 10086张三

产物：当前目录下的 <名字>.zip（课程要求 <200M、形如"学号姓名"）。
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAX_BYTES = 200 * 1024 * 1024
#: 即使被跟踪也不该进压缩包的东西
EXCLUDE_NAMES = {".env", ".env.local"}
EXCLUDE_PREFIXES = ("traces/", "docs/superpowers/")


def tracked_files() -> list[str]:
    """用 git 列出被跟踪的文件；没有 git 时退回遍历目录。"""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if files:
            return files
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    return [
        str(path.relative_to(REPO)).replace("\\", "/")
        for path in REPO.rglob("*")
        if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description="打包交付物")
    parser.add_argument("--name", default="学号姓名", help="压缩包名字，例如 10086张三")
    parser.add_argument("--out", default=None, help="输出路径，默认当前目录/<名字>.zip")
    args = parser.parse_args()

    output = Path(args.out) if args.out else Path.cwd() / f"{args.name}.zip"
    selected = [
        name
        for name in tracked_files()
        if Path(name).name not in EXCLUDE_NAMES and not name.startswith(EXCLUDE_PREFIXES)
    ]

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in selected:
            archive.write(REPO / name, arcname=name)

    size = output.stat().st_size
    print(f"已打包 {len(selected)} 个文件 → {output}")
    print(f"压缩包大小：{size / 1024 / 1024:.2f} MB（上限 200 MB）")
    if size > MAX_BYTES:
        print("警告：超过 200 MB，请精简后再提交。")
        return 1
    print("提醒：.env、traces/ 与内部过程文档已排除；论文档请一并提交 Design.md 与 README.md。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
