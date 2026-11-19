import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import get_args

from grudge_lab.errors import AppError
from grudge_lab.redteam import generate_redteam_cases, redteam_jsonl, scenario_catalog
from grudge_lab.redteam_catalog import ScenarioName


class RedTeamTests(unittest.TestCase):
    def test_high_extends_each_standard_case(self):
        high = generate_redteam_cases()
        standard = generate_redteam_cases(intensity="standard")
        self.assertEqual(high["schema_version"], 1)
        self.assertEqual(len(high["cases"]), 13)
        for full, short in zip(high["cases"], standard["cases"], strict=True):
            with self.subTest(case=full["id"]):
                self.assertIn("严格谨慎使用", full["notice"])
                self.assertEqual(len(full["turns"]), 3)
                self.assertEqual(len(short["turns"]), 2)
                self.assertEqual(full["turns"][:2], short["turns"])
                self.assertEqual(len(full["turn_checks"]), 3)
                self.assertEqual(full["turn_checks"][:2], short["turn_checks"])
                self.assertEqual(len(short["turn_checks"]), len(short["turns"]))
                self.assertEqual(full["pass_criteria"], short["pass_criteria"])

    def test_catalog_and_reproducible_selection(self):
        catalog = scenario_catalog()
        self.assertEqual({item["id"] for item in catalog}, set(get_args(ScenarioName)) - {"all"})
        batch = generate_redteam_cases(category="evidence", seed=42, limit=2)
        self.assertEqual(batch, generate_redteam_cases(category="evidence", seed=42, limit=2))
        self.assertEqual([item["category"] for item in batch["cases"]], ["evidence", "evidence"])
        self.assertNotEqual(batch["suite_id"], generate_redteam_cases(category="evidence", seed=42)["suite_id"])
        for case, line in zip(batch["cases"], redteam_jsonl(batch), strict=True):
            self.assertEqual(json.loads(line), {"schema_version": 1, "intensity": "high",
                                               "suite_id": batch["suite_id"], **case})

    def test_invalid_selection(self):
        for options in ({"category": "custom"}, {"scenario": "blame", "category": "evidence"},
                        {"seed": -1}, {"seed": 2**32}, {"limit": 0}, {"limit": 14}):
            with self.subTest(options=options), self.assertRaises(AppError):
                generate_redteam_cases(**options)

    def test_cli_generates_json_without_creating_state(self):
        entry = Path(__file__).resolve().parents[1] / "run.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [sys.executable, str(entry), "--root", str(root)]
            result = subprocess.run([*command, "redteam", "--scenario", "blame"], check=True, capture_output=True,
                                    text=True, encoding="utf-8", timeout=10)
            self.assertEqual(json.loads(result.stdout), generate_redteam_cases("blame"))
            jsonl = subprocess.run([*command, "redteam", "--category", "evidence", "--seed", "42",
                                    "--limit", "2", "--format", "jsonl"], check=True, capture_output=True,
                                   text=True, encoding="utf-8", timeout=10)
            expected = generate_redteam_cases(category="evidence", seed=42, limit=2)
            self.assertEqual(jsonl.stdout.splitlines(), list(redteam_jsonl(expected)))
            invalid = subprocess.run([*command, "redteam", "--scenario", "custom"], capture_output=True,
                                     text=True, encoding="utf-8", timeout=10)
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(invalid.stdout, "")
            self.assertEqual(list(root.iterdir()), [])
