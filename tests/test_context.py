"""运行上下文测试：步骤观察者回调。"""

from __future__ import annotations

from agentcode.core.context import RunContext
from agentcode.core.result import Step


def test_add_step_invokes_on_step_callback():
    seen: list[Step] = []
    ctx = RunContext(task="任务", on_step=seen.append)
    step = Step(index=1, thought="先想一想")
    ctx.add_step(step)
    assert seen == [step]


def test_add_step_survives_failing_callback():
    """观察者（例如网页推送）出错不应影响智能体主流程。"""

    def boom(step: Step) -> None:
        raise RuntimeError("观察者坏了")

    ctx = RunContext(on_step=boom)
    ctx.add_step(Step(index=1))
    assert len(ctx.steps) == 1


def test_default_context_has_no_callback():
    assert RunContext().on_step is None


def test_to_dict_is_unaffected_by_callback():
    ctx = RunContext(task="任务", on_step=lambda step: None)
    ctx.add_step(Step(index=1, thought="想", observation="观察"))
    payload = ctx.to_dict()
    assert payload["task"] == "任务"
    assert payload["steps"][0]["observation"] == "观察"
