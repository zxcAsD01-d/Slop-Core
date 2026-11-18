"""用临时 SQLite 数据库测量素材检索冷启动和凭证复用。"""

import argparse
import json
import math
import statistics
import tempfile
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

from grudge_lab.models import Candidate, Evidence, SourceConfig
from grudge_lab.plugin import PromptPlugin
from grudge_lab.sample import create_sample

PERSON = "zhou"
RESULT_LIMIT = 50


def make_fixture(root: Path) -> PromptPlugin:
    source = create_sample(root / "demo.db")
    plugin = PromptPlugin(SourceConfig(str(source)), root / "memory.db")
    candidates = [
        Candidate("preference", f"合成示例 {number}", "偏好明确的验收条件。", "请补充验收条件。",
                  [Evidence(str(number), "我偏好明确的验收条件。", "2025-01-01")], topic="验收")
        for number in range(200)
    ]
    plugin.set_consent(PERSON, True)
    key = plugin.source.key
    plugin.store.save_indexed_batch(key, PERSON, "benchmark", candidates)
    plugin.store.activate_generation(key, PERSON, "benchmark", "benchmark", len(candidates),
                                     plugin.reader.change_token())
    return plugin


def measure(plugin: PromptPlugin, samples: int) -> dict[str, float | int]:
    connections = 0
    connect = plugin.store.connect

    @contextmanager
    def count_connection():
        nonlocal connections
        connections += 1
        with connect() as connection:
            yield connection

    durations = []
    with patch.object(plugin.store, "connect", count_connection):
        for _ in range(samples):
            started = perf_counter()
            result = plugin.search_person_materials(PERSON, query="验收条件", limit=RESULT_LIMIT)
            durations.append((perf_counter() - started) * 1000)
            if len(result["materials"]) != RESULT_LIMIT:
                raise RuntimeError("基准查询未返回预期数量的素材")
    warm = sorted(durations[1:])
    return {"result_limit": RESULT_LIMIT, "warm_samples": len(warm),
            "cold_ms": round(durations[0], 3), "warm_p50_ms": round(statistics.median(warm), 3),
            "warm_p95_ms": round(warm[math.ceil(len(warm) * 0.95) - 1], 3),
            "sqlite_connections_per_search": connections // samples}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=61, help="总查询次数，包含首次查询")
    args = parser.parse_args()
    if args.samples < 2:
        parser.error("至少运行两次查询")
    with tempfile.TemporaryDirectory() as temporary:
        print(json.dumps(measure(make_fixture(Path(temporary)), args.samples), indent=2))


if __name__ == "__main__":
    main()
