import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import get_args

from .errors import AppError
from .indexing import IndexManager
from .plugin import PromptPlugin
from .redteam import Intensity, generate_redteam_cases, redteam_jsonl, scenario_catalog
from .redteam_catalog import Category, ScenarioName
from .sample import create_sample
from .server import serve
from .settings import Settings


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="GRUDGE LAB · 持久记忆与提示词块生成器")
    result.add_argument("--root", type=Path, default=Path.cwd(), help="数据和配置所在目录")
    commands = result.add_subparsers(dest="command")
    web = commands.add_parser("serve", help="打开本地工作台")
    web.add_argument("--port", type=int, default=8765)
    seed = commands.add_parser("seed", help="创建虚构测试数据库；已有文件不会覆盖")
    seed.add_argument("--path", type=Path)
    commands.add_parser("users", help="列出当前映射的用户")
    consent = commands.add_parser("consent", help="开启或撤回已获用户同意的记忆")
    consent.add_argument("user_id")
    consent.add_argument("--revoke", action="store_true")
    analysis = commands.add_parser("analyze", help="分析并持久化证据记忆")
    analysis.add_argument("user_id")
    export = commands.add_parser("export", help="生成提示词块")
    export.add_argument("user_id")
    export.add_argument("--intensity", type=int, choices=[1, 2], default=2)
    export.add_argument("--budget", type=int, default=6000)
    export.add_argument("--output", type=Path)
    index = commands.add_parser("index", help="建立或增量更新已授权人物的全文索引，可断点续跑")
    index.add_argument("user_id", nargs="?", default="")
    status = commands.add_parser("index-status", help="查询索引进度")
    status.add_argument("job_id", nargs="?", default="")
    search = commands.add_parser("search", help="按人物检索全文素材和提示词")
    search.add_argument("user_id")
    search.add_argument("--query", default="")
    search.add_argument("--topic", default="")
    search.add_argument("--limit", type=int, default=10)
    detail = commands.add_parser("material", help="获取一条素材及证据")
    detail.add_argument("user_id")
    detail.add_argument("material_id", type=int)
    used = commands.add_parser("confirm-use", help="素材实际采用后幂等确认使用")
    used.add_argument("user_id")
    used.add_argument("material_id", type=int)
    used.add_argument("idempotency_key")
    redteam = commands.add_parser("redteam", help="导出虚构 AI 多轮压力测试用例")
    redteam.add_argument("--scenario", choices=get_args(ScenarioName), default="all")
    redteam.add_argument("--intensity", choices=get_args(Intensity), default="high")
    redteam.add_argument("--category", choices=get_args(Category))
    redteam.add_argument("--seed", type=int, help="以固定种子决定用例顺序")
    redteam.add_argument("--limit", type=int, help="最多导出的用例数")
    redteam.add_argument("--format", choices=("json", "jsonl"), default="json")
    commands.add_parser("redteam-catalog", help="列出虚构 AI 压测场景")
    mcp = commands.add_parser("mcp", help="运行本机 stdio MCP 服务")
    mcp.add_argument("--redteam-only", action="store_true", help="仅开放 AI 压测用例生成")
    return result


def main() -> None:
    arguments = parser().parse_args()
    root = arguments.root.expanduser().resolve()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        dispatch(arguments, root)
    except (AppError, OSError) as error:
        print("错误：" + str(error), file=sys.stderr)
        raise SystemExit(1) from error
    except KeyboardInterrupt:
        print("已停止；索引检查点已保存，下次 index 或 MCP 启动会继续。", file=sys.stderr)
        raise SystemExit(130)


def export_redteam(arguments: argparse.Namespace) -> None:
    batch = generate_redteam_cases(arguments.scenario, arguments.intensity, category=arguments.category,
                                   seed=arguments.seed, limit=arguments.limit)
    if arguments.format == "jsonl":
        for line in redteam_jsonl(batch):
            print(line)
    else:
        print(json.dumps(batch, ensure_ascii=False, indent=2))


def dispatch(arguments: argparse.Namespace, root: Path) -> None:
    command = arguments.command or "serve"
    if command == "seed":
        print("已创建：" + str(create_sample(arguments.path or root / "data" / "demo.db")))
        return
    if command == "serve":
        if not (root / "data" / "demo.db").exists():
            create_sample(root / "data" / "demo.db")
        serve(root, getattr(arguments, "port", 8765))
        return
    if command == "mcp":
        from .mcp_server import run_mcp
        run_mcp(root, redteam_only=arguments.redteam_only)
        return
    if command == "redteam":
        export_redteam(arguments)
        return
    if command == "redteam-catalog":
        print(json.dumps({"scenarios": scenario_catalog()}, ensure_ascii=False, indent=2))
        return
    settings = Settings(root)
    plugin = PromptPlugin(settings.source(), root / ".state" / "memory.db", settings.public()["limit"])
    if command == "users":
        print(json.dumps(plugin.reader.users(), ensure_ascii=False, indent=2))
    elif command == "consent":
        plugin.set_consent(arguments.user_id, not arguments.revoke)
        print("已撤回并清除记忆" if arguments.revoke else "已开启记忆")
    elif command == "analyze":
        print(json.dumps(plugin.analyze(arguments.user_id, settings.llm()), ensure_ascii=False, indent=2))
    elif command == "export":
        result = plugin.generate(arguments.user_id, arguments.intensity, arguments.budget)
        if arguments.output:
            arguments.output.parent.mkdir(parents=True, exist_ok=True)
            arguments.output.write_text(str(result["text"]), encoding="utf-8")
            print("已导出：" + str(arguments.output.resolve()))
        else:
            print(result["text"])
    elif command in {"index", "index-status"}:
        manager = IndexManager(plugin.source, plugin.store, settings.llm())
        if command == "index-status":
            result = manager.status(arguments.job_id)
        else:
            result = manager.start(arguments.user_id)
            print("索引任务：" + str(result["id"]), file=sys.stderr)
            while result["status"] in {"queued", "running"}:
                time.sleep(0.2)
                result = manager.status(str(result["id"]))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if result["status"] == "completed_with_errors":
            raise AppError("索引未全部完成，详情见 errors")
    elif command == "search":
        print(json.dumps(plugin.search_person_materials(
            arguments.user_id, arguments.query, arguments.topic, arguments.limit,
        ), ensure_ascii=False, indent=2))
    elif command == "material":
        print(json.dumps(plugin.material_detail(arguments.user_id, arguments.material_id),
                         ensure_ascii=False, indent=2))
    elif command == "confirm-use":
        print(json.dumps(plugin.confirm_material_use(
            arguments.user_id, arguments.material_id, arguments.idempotency_key,
        ), ensure_ascii=False, indent=2))
