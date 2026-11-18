import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict
from urllib.parse import parse_qs, urlsplit

from .errors import AppError
from .indexing import IndexManager, candidate_cache_key
from .models import SourceConfig, required_text
from .plugin import PromptPlugin
from .settings import Settings
from .source import SourceReader


LOGGER = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"


class Application:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.settings = Settings(root)
        self.token = secrets.token_urlsafe(32)
        self._managers: Dict[str, IndexManager] = {}
        self._manager_lock = threading.RLock()

    def plugin(self) -> PromptPlugin:
        config = self.settings.public()
        return PromptPlugin(SourceConfig(**config["source"]),
                            self.root / ".state" / "memory.db", config["limit"])

    def index_manager(self) -> IndexManager:
        plugin = self.plugin()
        with self._manager_lock:
            llm = self.settings.llm()
            key = plugin.source.key + candidate_cache_key(llm)
            if key not in self._managers:
                self._managers[key] = IndexManager(plugin.source, plugin.store, llm)
                self._managers[key].resume_pending()
            return self._managers[key]

    def get(self, path: str, query: Dict[str, list]) -> Dict[str, object]:
        if path == "/api/config":
            return self.settings.public()
        if path == "/api/users":
            return {"users": self.plugin().reader.users()}
        if path == "/api/profile":
            user_id = required_text({"user_id": query.get("user_id", [""])[0]}, "user_id")
            return self.plugin().profile(user_id)
        if path == "/api/index-status":
            return self.index_manager().status(query.get("job_id", [""])[0])
        if path == "/api/materials":
            user_id = required_text({"user_id": query.get("user_id", [""])[0]}, "user_id")
            return self.plugin().search_person_materials(
                user_id, query.get("query", [""])[0], query.get("topic", [""])[0],
            )
        raise AppError("接口不存在", 404)

    def post(self, path: str, data: Dict[str, object]) -> Dict[str, object]:
        if path == "/api/schema":
            config = SourceConfig(required_text(data, "path", 4096))
            return {"tables": SourceReader(config).schema()}
        if path == "/api/config":
            Settings._validate(data)
            SourceReader(SourceConfig(**data["source"])).validate()
            self.settings.update(data)
            return self.settings.public()
        plugin = self.plugin()
        if path == "/api/index":
            person = data.get("user_id", "")
            if not isinstance(person, str):
                raise AppError("user_id 必须是字符串")
            return self.index_manager().start(person)
        user_id = required_text(data, "user_id")
        if path == "/api/consent":
            if not isinstance(data.get("enabled"), bool):
                raise AppError("enabled 必须是布尔值")
            plugin.set_consent(user_id, data["enabled"])
            return {"enabled": data["enabled"]}
        if path == "/api/analyze":
            return plugin.analyze(user_id, self.settings.llm())
        if path == "/api/generate":
            intensity, budget = data.get("intensity", 2), data.get("budget", 6000)
            if type(intensity) is not int or type(budget) is not int:
                raise AppError("表达档位和预算必须是整数")
            return plugin.generate(user_id, intensity, budget)
        if path == "/api/delete":
            if type(data.get("memory_id")) is not int:
                raise AppError("memory_id 必须是整数")
            plugin.delete_memory(user_id, data["memory_id"])
            return {"deleted": True}
        if path == "/api/confirm-use":
            material_id = data.get("material_id")
            if type(material_id) is not int:
                raise AppError("material_id 必须是整数")
            return plugin.confirm_material_use(user_id, material_id, required_text(data, "idempotency_key", 128))
        raise AppError("接口不存在", 404)


def handler_for(app: Application):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._handle(False)

        def do_POST(self) -> None:
            self._handle(True)

        def _handle(self, post: bool) -> None:
            try:
                self._check_host()
                parsed = urlsplit(self.path)
                if parsed.path.startswith("/api/"):
                    self._authorize()
                    result = app.post(parsed.path, self._body()) if post else app.get(
                        parsed.path, parse_qs(parsed.query))
                    self._json(result)
                elif not post:
                    self._static(parsed.path)
                else:
                    raise AppError("接口不存在", 404)
            except AppError as error:
                self._json({"error": str(error)}, error.status)
            except (BrokenPipeError, ConnectionResetError):
                LOGGER.info("浏览器已断开连接")
            except Exception:
                LOGGER.exception("请求处理失败")
                self._json({"error": "服务内部错误，详情见启动终端"}, 500)

        def _check_host(self) -> None:
            port = self.server.server_port
            allowed = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
            if self.headers.get("Host") not in allowed:
                raise AppError("仅允许本机访问", 403)
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in allowed}:
                raise AppError("不允许跨站请求", 403)

        def _authorize(self) -> None:
            token = self.headers.get("X-Workbench-Token", "")
            if not secrets.compare_digest(token, app.token):
                raise AppError("会话已失效，请刷新页面", 403)

        def _body(self) -> Dict[str, object]:
            if self.headers.get_content_type() != "application/json":
                raise AppError("请求必须使用 application/json", 415)
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise AppError("Content-Length 无效") from error
            if not 0 < length <= 1_048_576:
                raise AppError("请求体为空或超过 1 MB", 413)
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                raise AppError("请求体不是有效 JSON") from error
            if not isinstance(data, dict):
                raise AppError("请求体必须是 JSON 对象")
            return data

        def _static(self, path: str) -> None:
            resources = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                         "/style.css": ("style.css", "text/css"), "/corpus.css": ("corpus.css", "text/css"),
                         "/favicon.svg": ("favicon.svg", "image/svg+xml")}
            if path not in resources:
                raise AppError("页面不存在", 404)
            name, content_type = resources[path]
            content = (STATIC / name).read_bytes()
            if name == "index.html":
                content = content.replace(b"__SESSION_TOKEN__", app.token.encode())
            self._send(content, content_type + "; charset=utf-8", 200)

        def _json(self, data: Dict[str, object], status: int = 200) -> None:
            self._send(json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8", status)

        def _send(self, content: bytes, content_type: str, status: int) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; "
                             "script-src 'self'; img-src 'self' data:; connect-src 'self'; "
                             "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(content)

        def log_message(self, format: str, *args: object) -> None:
            LOGGER.info("%s %s", self.address_string(), format % args)

    return Handler


def serve(root: Path, port: int) -> None:
    app = Application(root)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(app))
    except OSError as error:
        raise AppError("启动失败，请检查端口是否占用：" + str(error), 500) from error
    print("\n  GRUDGE LAB · 对话证据工作台\n  http://127.0.0.1:" + str(server.server_port) + "\n  Ctrl+C 停止\n", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
