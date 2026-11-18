import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from grudge_lab.redteam import generate_redteam_cases


class RedTeamTests(unittest.TestCase):
    def test_high_extends_each_standard_case(self):
        high = generate_redteam_cases()
        standard = generate_redteam_cases(intensity="standard")
        self.assertEqual(high["schema_version"], 1)
        self.assertEqual(len(high["cases"]), 5)
        for full, short in zip(high["cases"], standard["cases"], strict=True):
            with self.subTest(case=full["id"]):
                self.assertIn("严格谨慎使用", full["notice"])
                self.assertEqual(len(full["turns"]), 3)
                self.assertEqual(len(short["turns"]), 2)
                self.assertEqual(full["turns"][:2], short["turns"])
                self.assertEqual(full["pass_criteria"], short["pass_criteria"])

    def test_cli_generates_json_without_creating_state(self):
        entry = Path(__file__).resolve().parents[1] / "run.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = [sys.executable, str(entry), "--root", str(root), "redteam", "--scenario"]
            result = subprocess.run([*command, "blame"], check=True, capture_output=True,
                                    text=True, encoding="utf-8", timeout=10)
            self.assertEqual(json.loads(result.stdout), generate_redteam_cases("blame"))
            invalid = subprocess.run([*command, "custom"], capture_output=True,
                                     text=True, encoding="utf-8", timeout=10)
            self.assertEqual(invalid.returncode, 2)
            self.assertEqual(invalid.stdout, "")
            self.assertEqual(list(root.iterdir()), [])
