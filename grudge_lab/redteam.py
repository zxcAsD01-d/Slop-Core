"""离线选取和导出可复现的虚构 AI 压测用例。"""

import hashlib
import json
from typing import Iterator, Literal, TypedDict, get_args

from .errors import AppError
from .redteam_catalog import Category, SCENARIOS, Scenario, ScenarioName as ScenarioName

Intensity = Literal["standard", "high"]
SCHEMA_VERSION = 1
NOTICE = "严格谨慎使用：仅用于隔离环境的 AI 对抗测试。"


class ScenarioInfo(TypedDict):
    id: str
    title: str
    category: Category
    target_role: str


class RedTeamCase(ScenarioInfo):
    notice: str
    turns: list[str]
    turn_checks: list[str]
    pass_criteria: str


class RedTeamBatch(TypedDict):
    schema_version: int
    suite_id: str
    intensity: str
    cases: list[RedTeamCase]


def scenario_catalog() -> list[ScenarioInfo]:
    return [_scenario_info(name, case) for name, case in SCENARIOS.items()]


def _scenario_info(name: str, case: Scenario) -> ScenarioInfo:
    return {"id": name, "title": case.title, "category": case.category, "target_role": case.target_role}


def _select_scenarios(scenario: str, category: str | None, seed: int | None,
                      limit: int | None) -> list[str]:
    if scenario != "all" and scenario not in SCENARIOS:
        raise AppError("红队场景无效")
    if category is not None and category not in get_args(Category):
        raise AppError("红队类别无效")
    if seed is not None and not 0 <= seed <= 2**32 - 1:
        raise AppError("红队种子必须在 0–4294967295 之间")
    if limit is not None and not 1 <= limit <= len(SCENARIOS):
        raise AppError(f"红队数量必须在 1–{len(SCENARIOS)} 之间")

    names = list(SCENARIOS) if scenario == "all" else [scenario]
    names = [name for name in names if category is None or SCENARIOS[name].category == category]
    if not names:
        raise AppError("指定场景不属于所选类别")
    if seed is not None:
        names.sort(key=lambda name: hashlib.sha256(f"{seed}:{name}".encode()).digest())
    return names[:limit]


def _build_case(name: str, turn_count: int) -> RedTeamCase:
    case = SCENARIOS[name]
    return {**_scenario_info(name, case), "notice": NOTICE, "turns": list(case.turns[:turn_count]),
            "turn_checks": list(case.turn_checks[:turn_count]), "pass_criteria": case.pass_criteria}


def generate_redteam_cases(scenario: str = "all", intensity: str = "high", *,
                           category: str | None = None, seed: int | None = None,
                           limit: int | None = None) -> RedTeamBatch:
    if intensity not in get_args(Intensity):
        raise AppError("红队强度只能为 standard 或 high")
    names = _select_scenarios(scenario, category, seed, limit)
    turn_count = 3 if intensity == "high" else 2
    cases = [_build_case(name, turn_count) for name in names]
    canonical = json.dumps([SCHEMA_VERSION, intensity, cases], ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    suite_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {"schema_version": SCHEMA_VERSION, "suite_id": suite_id, "intensity": intensity, "cases": cases}


def redteam_jsonl(batch: RedTeamBatch) -> Iterator[str]:
    metadata = {"schema_version": batch["schema_version"], "suite_id": batch["suite_id"],
                "intensity": batch["intensity"]}
    for case in batch["cases"]:
        yield json.dumps({**metadata, **case}, ensure_ascii=False, separators=(",", ":"))
