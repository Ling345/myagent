"""批量测试生成：给一批源码文件补 pytest 测试。

这是产品**收窄之后的主打场景**——不是"什么都能聊的 agent"，
而是"给你没测试的 Python 代码自动补测试"。放进 CI 就是一道门禁，
放进预提交钩子就是"改完顺手补测试"。

几处刻意的设计：

- **测试写到源文件旁边**（或 ``--out`` 指定的目录），而不是留在 agent 的工作目录里
  ——后者是内部实现，用户不该去那儿找产物。
- **已有测试文件默认跳过**，要覆盖必须显式 ``--force``。静默覆盖别人写好的测试，
  是最容易让人拉黑一个工具的行为。
- **"模型说成功了"不算成功**：工作目录里必须真的有测试文件落下来，
  否则报失败。不然用户看到一片绿，实际什么都没生成。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

#: 一次最多处理多少个文件。防止有人对着一万行的仓库直接跑，把额度烧光
DEFAULT_LIMIT = 20

#: 这些文件不是"待补测试的源码"
SKIP_NAMES = frozenset({"setup.py", "conftest.py", "__init__.py", "__main__.py"})

#: 目录名里带这些就跳过
SKIP_DIRS = frozenset({"__pycache__", ".git", ".venv", "venv", "node_modules", "build", "dist"})

#: 跑一个文件：给它任务描述与工作目录，返回带 success / answer / error 的结果
AgentRunner = Callable[[str, Path], Any]


def is_test_file(path: Path) -> bool:
    """这是不是一个测试文件（而不是待补测试的源码）。"""
    name = path.name
    if name == "conftest.py":
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
        return True
    # tests/ 目录下的都算
    return any(part in {"tests", "test"} for part in path.parts[:-1])


def discover_targets(path: str | Path, *, limit: int = DEFAULT_LIMIT) -> list[Path]:
    """找出该为哪些文件生成测试。

    传目录就递归找 ``.py``；传单个文件就直接用它（但如果它本身是测试文件，
    返回空——你已经写好的测试不该被当成"缺测试的源码"）。
    """
    target = Path(path).resolve()
    if target.is_file():
        if is_test_file(target) or target.suffix != ".py":
            return []
        return [target]
    if not target.is_dir():
        return []

    found: list[Path] = []
    for candidate in sorted(target.rglob("*.py")):
        relative = candidate.relative_to(target)
        if any(part in SKIP_DIRS or part.startswith(".") for part in relative.parts[:-1]):
            continue
        if candidate.name.startswith("."):
            continue
        if candidate.name in SKIP_NAMES:
            continue
        if is_test_file(candidate):
            continue
        if relative.parts and relative.parts[0] in {"tests", "test"}:
            continue
        found.append(candidate)
        if len(found) >= max(1, int(limit)):
            break
    return found


def default_test_path(source: Path) -> Path:
    """源文件对应的测试文件路径：``calc.py`` → ``test_calc.py``（放旁边）。"""
    return source.with_name(f"test_{source.stem}.py")


@dataclass(frozen=True)
class GenerationResult:
    """一个文件的生成结果。"""

    source: Path
    dest: Path
    #: written / skipped / failed
    status: str
    reason: str = ""
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """跳过不算失败——用户已经有测试了，这是好事。"""
        return self.status in ("written", "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source),
            "dest": str(self.dest),
            "status": self.status,
            "reason": self.reason,
            "duration_ms": round(self.duration_ms, 1),
        }


def _find_written_test(workspace: Path, stem: str, *, since: float) -> Path | None:
    """在工作目录里找 agent 刚写出来的测试文件。

    优先按约定找 ``test_<stem>.py``；模型要是起了别的名字，就退一步找
    最新写出来的那个 ``test_*.py``——总比白跑一趟强。
    """
    preferred = workspace / f"test_{stem}.py"
    if preferred.is_file():
        return preferred
    fresh: list[Path] = []
    for candidate in workspace.glob("test_*.py"):
        try:
            if candidate.is_file() and candidate.stat().st_mtime >= since:
                fresh.append(candidate)
        except OSError:
            continue
    if not fresh:
        return None
    return max(fresh, key=lambda item: item.stat().st_mtime)


def generate_for(
    source: str | Path,
    *,
    workspace: str | Path,
    run_agent: AgentRunner,
    out_dir: str | Path | None = None,
    force: bool = False,
) -> GenerationResult:
    """给一个源文件生成测试，并把结果写回源文件旁边。"""
    started = time.perf_counter()
    src = Path(source).resolve()
    workdir = Path(workspace).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    dest = (Path(out_dir).resolve() if out_dir else src.parent) / f"test_{src.stem}.py"

    if dest.exists() and not force:
        return GenerationResult(
            source=src,
            dest=dest,
            status="skipped",
            reason=f"{dest.name} 已经存在，跳过（要覆盖请加 --force）",
        )

    before = time.time()
    shutil.copyfile(src, workdir / src.name)
    task = (
        f"为 {src.name} 生成 pytest 测试用例，覆盖正常路径与边界情况。"
        "写完之后跑一遍，不通过就改到通过。"
    )
    outcome = run_agent(task, workdir)
    duration = (time.perf_counter() - started) * 1000

    if not getattr(outcome, "success", False):
        reason = getattr(outcome, "error", None) or "智能体没有跑通"
        return GenerationResult(src, dest, "failed", reason=str(reason), duration_ms=duration)

    produced = _find_written_test(workdir, src.stem, since=before)
    if produced is None:
        return GenerationResult(
            src,
            dest,
            "failed",
            reason="智能体没有生成测试文件（工作目录里找不到 test_*.py）",
            duration_ms=duration,
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(produced, dest)
    return GenerationResult(src, dest, "written", duration_ms=duration)
