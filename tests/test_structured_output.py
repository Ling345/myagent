"""结构化输出：要求模型回 JSON、校验、不合格就把问题回给模型重试（离线）。"""

from __future__ import annotations

from agentcode.agents import PlanAndSolveAgent
from agentcode.llm import ScriptedLLM
from agentcode.llm.structured import PLAN_SCHEMA, ask_json, parse_json_object, validate_json
from agentcode.tools import ToolRegistry


# ---------------------------------------------------------------- 抠 JSON


def test_plain_json_is_parsed():
    assert parse_json_object('{"steps": ["一", "二"]}') == {"steps": ["一", "二"]}


def test_fenced_json_is_parsed():
    text = '这是我的计划：\n```json\n{"steps": ["一"]}\n```\n以上。'

    assert parse_json_object(text) == {"steps": ["一"]}


def test_json_with_noise_around_it_is_parsed():
    assert parse_json_object('好的，结果是 {"steps": ["一"]} 请查收') == {"steps": ["一"]}


def test_broken_json_returns_none():
    assert parse_json_object("我建议先做需求，再做设计。") is None
    assert parse_json_object('{"steps": ["一", }') is None


# ---------------------------------------------------------------- 校验


def test_validation_passes_a_good_payload():
    assert validate_json({"steps": ["一", "二"]}, PLAN_SCHEMA) == []


def test_validation_reports_a_missing_key():
    problems = validate_json({}, PLAN_SCHEMA)

    assert any("steps" in item for item in problems)


def test_validation_reports_a_wrong_type():
    problems = validate_json({"steps": "一"}, PLAN_SCHEMA)

    assert any("列表" in item for item in problems)


def test_validation_reports_a_bad_item_type():
    problems = validate_json({"steps": ["一", 2]}, PLAN_SCHEMA)

    assert any("字" in item for item in problems)


def test_validation_reports_an_empty_list():
    problems = validate_json({"steps": []}, PLAN_SCHEMA)

    assert any("空" in item for item in problems)


# ---------------------------------------------------------------- 让模型回 JSON


def test_ask_json_returns_the_payload_on_the_first_try():
    llm = ScriptedLLM(['```json\n{"steps": ["一", "二"]}\n```'])

    answer = ask_json(llm, [{"role": "user", "content": "拆解任务"}], PLAN_SCHEMA)

    assert answer.payload == {"steps": ["一", "二"]}
    assert answer.problems == []
    assert len(llm.calls) == 1


def test_ask_json_retries_with_the_problems_when_the_reply_is_bad():
    llm = ScriptedLLM(['{"steps": "一"}', '```json\n{"steps": ["一"]}\n```'])

    answer = ask_json(llm, [{"role": "user", "content": "拆解任务"}], PLAN_SCHEMA)

    assert answer.payload == {"steps": ["一"]}
    assert len(llm.calls) == 2
    # 重试时要告诉模型哪里不对，而不是原样再问一遍
    assert "列表" in llm.calls[1][-1]["content"]


def test_ask_json_gives_up_and_reports_why():
    llm = ScriptedLLM(["我不知道", "我还是不知道"])

    answer = ask_json(llm, [{"role": "user", "content": "拆解任务"}], PLAN_SCHEMA)

    assert answer.payload is None
    assert answer.problems
    assert answer.text == "我还是不知道"


# ---------------------------------------------------------------- 用到智能体上


def test_planner_accepts_a_json_plan():
    llm = ScriptedLLM(
        [
            '```json\n{"steps": ["算苹果数", "求和"]}\n```',
            "周一15个",
            "总共45个",
        ]
    )

    result = PlanAndSolveAgent(llm=llm, tools=ToolRegistry()).run("三天共卖出多少苹果")

    assert result.success is True
    assert result.extra["plan"] == ["算苹果数", "求和"]
    assert result.answer == "总共45个"
