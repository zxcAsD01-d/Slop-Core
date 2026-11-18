import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import CallToolResult

from grudge_lab.models import SourceConfig
from grudge_lab.plugin import PromptPlugin
from grudge_lab.redteam import generate_redteam_cases, scenario_catalog
from grudge_lab.sample import create_sample


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_tools_index_search_and_idempotent_use(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = create_sample(root / "data" / "demo.db")
            plugin = PromptPlugin(SourceConfig(str(source)), root / ".state" / "memory.db")
            plugin.set_consent("zhou", True)
            project = Path(__file__).resolve().parents[1]
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(project / "run.py"), "--root", str(root), "mcp"], cwd=str(project))
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({item.name for item in tools.tools},
                        {"list_people", "search_person_materials", "get_material_detail",
                         "confirm_material_use", "start_index", "get_index_status", "redteam_cases", "redteam_catalog"})
                    people = self.data(await session.call_tool("list_people", {}))
                    self.assertEqual(people["people"][0]["person_id"], "zhou")
                    await self.exercise_materials(session)

    async def exercise_materials(self, session):
        job = self.data(await session.call_tool("start_index", {"person_id": "zhou"}))
        for _ in range(100):
            status = self.data(await session.call_tool("get_index_status", {"job_id": job["id"]}))
            if status["status"] not in {"queued", "running"}:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(status["status"], "completed", status)
        result = self.data(await session.call_tool("search_person_materials",
                          {"person_id": "zhou", "query": "四十页"}))
        material = result["materials"][0]
        args = {"person_id": "zhou", "material_id": material["id"],
                "idempotency_key": material["use_receipt"]}
        first = self.data(await session.call_tool("confirm_material_use", args))
        second = self.data(await session.call_tool("confirm_material_use", args))
        self.assertTrue(first["counted"])
        self.assertFalse(second["counted"])
        self.assertEqual(second["use_count"], 1)
        blocked = await session.call_tool("search_person_materials", {"person_id": "lin"})
        self.assertTrue(blocked.is_error)

    async def test_redteam_mcp_isolated_from_local_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = Path(__file__).resolve().parents[1]
            parameters = StdioServerParameters(command=sys.executable,
                args=[str(project / "run.py"), "--root", str(root), "mcp", "--redteam-only"],
                cwd=str(project))
            async with stdio_client(parameters) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    self.assertEqual({item.name for item in tools.tools}, {"redteam_cases", "redteam_catalog"})
                    catalog = self.data(await session.call_tool("redteam_catalog", {}))
                    self.assertEqual(catalog["scenarios"], scenario_catalog())
                    args = {"category": "evidence", "intensity": "standard", "seed": 42, "limit": 2}
                    result = self.data(await session.call_tool("redteam_cases", args))
                    self.assertEqual(result, generate_redteam_cases(**args))
                    invalid = await session.call_tool("redteam_cases", {"scenario": "custom"})
                    self.assertTrue(invalid.is_error)
                    invalid_limit = await session.call_tool("redteam_cases", {"limit": 0})
                    self.assertTrue(invalid_limit.is_error)
            self.assertEqual(list(root.iterdir()), [])

    def data(self, result: CallToolResult):
        self.assertFalse(result.is_error, result)
        if result.structured_content is not None:
            return result.structured_content
        return json.loads(result.content[0].text)


if __name__ == "__main__":
    unittest.main()
