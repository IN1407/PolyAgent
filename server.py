"""Run PolyAgent locally: python3 server.py"""

import argparse
import json
import mimetypes
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from polyagent.app import PolyAgent
from polyagent.codex import CodexError
from polyagent.deepseek import DeepSeekError
from polyagent.grok import GrokError
from polyagent.antigravity import AntigravityError
from polyagent.antigravity_cli import AntigravityCliError
from polyagent.opencode import OpenCodeError


ROOT = Path(__file__).resolve().parent


class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, format, *args):
        if "/events" in self.path:
            return
        super().log_message(format, *args)

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length > 1_000_000:
            raise ValueError("Request is too large")
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            raise ValueError("Expected application/json")
        return json.loads(self.rfile.read(length) or b"{}")

    def _static(self, path):
        if path == "/":
            path = "/index.html"
        file = (ROOT / "static" / path.lstrip("/")).resolve()
        if ROOT / "static" not in file.parents or not file.is_file():
            self.send_error(404)
            return
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", mimetypes.guess_type(str(file))[0] or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _open_project_item(self, project_id, name):
        if not isinstance(name, str):
            raise ValueError("Project file path must be text")
        root = Path(self.app.project(project_id)["path"]).resolve()
        target = (root / name).resolve() if name else root
        if target != root and root not in target.parents:
            raise ValueError("File must be inside the selected project")
        if not target.exists():
            raise ValueError("That project file no longer exists")
        if target != root and (not target.is_file() or target.suffix.lower() not in {
                ".html", ".htm", ".txt", ".md", ".css", ".js", ".json", ".csv",
                ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".pdf"}):
            raise ValueError("Open this file from the project folder")
        if sys.platform == "darwin":
            text_file = target != root and target.suffix.lower() in {".txt", ".md", ".css", ".js", ".json", ".csv"}
            subprocess.Popen(["open", *(["-t"] if text_file else []), str(target)])
        elif os.name == "nt":
            os.startfile(str(target))
        else:
            subprocess.Popen(["xdg-open", str(target)])
        return {"opened": True}

    def do_GET(self):
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        try:
            if path == "/api/state":
                return self._json({"projects": self.app.projects(), "models": self.app.models(),
                                   "connection": self.app.connection_status(),
                                   "connections": self.app.connections(),
                                   "harnesses": self.app.harnesses(),
                                   "harness_modes": self.app.harness_modes()})
            if path.startswith("/api/projects/") and path.endswith("/events"):
                project_id = path.split("/")[3]
                since = int(parse_qs(parts.query).get("since", ["0"])[0])
                return self._json({"events": self.app.events(project_id, since),
                                   "chats": self.app.chats(project_id),
                                   "conversations": self.app.conversations(project_id)})
            if path.startswith("/api/"):
                return self._json({"error": "Not found"}, 404)
            return self._static(path)
        except (ValueError, CodexError, AntigravityError, AntigravityCliError, OpenCodeError,
                DeepSeekError, GrokError) as exc:
            return self._json({"error": str(exc)}, 400)

    def do_POST(self):
        parts = urlsplit(self.path)
        path = unquote(parts.path)
        origin = self.headers.get("Origin")
        if origin and origin != f"http://{self.headers.get('Host')}":
            return self._json({"error": "Origin mismatch"}, 403)
        try:
            body = self._body()
            if path == "/api/hooks/antigravity":
                return self._json(self.app.handle_antigravity_hook(body))
            if path == "/api/hooks/opencode":
                return self._json(self.app.handle_opencode_hook(body))
            if path == "/api/hooks/grok":
                return self._json(self.app.handle_grok_hook(body))
            if path == "/api/connect/codex":
                return self._json(self.app.connect_codex())
            if path == "/api/connect/antigravity":
                return self._json(self.app.connect_antigravity())
            if path == "/api/connect/opencode":
                return self._json(self.app.connect_opencode())
            if path == "/api/connect/deepseek_harness":
                return self._json(self.app.connect_deepseek())
            if path == "/api/connect/grok_build":
                return self._json(self.app.connect_grok())
            if path == "/api/projects":
                return self._json(self.app.create_project(body.get("name", ""), body.get("path")), 201)
            match = path.split("/")
            if len(match) == 5 and match[1:3] == ["api", "harnesses"] and match[4] == "settings":
                return self._json(self.app.save_execution_settings({}, match[3], body))
            if len(match) == 5 and match[1:3] == ["api", "projects"]:
                project_id, action = match[3], match[4]
                if action == "config":
                    return self._json(self.app.configure_project(project_id, body))
                if action == "send":
                    return self._json(self.app.send(project_id, body.get("message", ""),
                                                   body.get("conversation_id"), body.get("settings"),
                                                   body.get("settings_harness")))
                if action == "conversations":
                    return self._json(self.app.create_conversation(project_id), 201)
                if action == "research":
                    return self._json(self.app.research_model(project_id, body.get("model_id", "")))
                if action == "interrupt-all":
                    return self._json(self.app.interrupt_all(project_id, body.get("conversation_id", "")))
                if action == "open-file":
                    return self._json(self._open_project_item(project_id, body.get("path", "")))
            if len(match) == 6 and match[1:3] == ["api", "projects"] and match[4] == "interrupt":
                return self._json(self.app.interrupt_chat(match[3], match[5]))
            if path.startswith("/api/models/") and path.rsplit("/", 1)[-1] in {"override", "enabled"}:
                model_id, action = path[len("/api/models/"):].rsplit("/", 1)
                if action == "override":
                    self.app.set_override(model_id, body)
                    return self._json({"saved": True})
                if action == "enabled":
                    self.app.set_model_enabled(model_id, body.get("enabled", False))
                    return self._json({"saved": True})
            if len(match) == 4 and match[1:3] == ["api", "approvals"]:
                return self._json(self.app.answer_approval(match[3], body.get("decision")))
            if len(match) == 4 and match[1:3] == ["api", "questions"]:
                return self._json(self.app.answer_user_input(match[3], body.get("answers", {})))
            if len(match) == 4 and match[1:3] == ["api", "model-choices"]:
                return self._json(self.app.answer_model_choice(match[3], body.get("model")))
            return self._json({"error": "Not found"}, 404)
        except (ValueError, CodexError, AntigravityError, AntigravityCliError, OpenCodeError,
                DeepSeekError, GrokError, json.JSONDecodeError) as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:
            return self._json({"error": f"Server error: {exc}"}, 500)


def main():
    parser = argparse.ArgumentParser(description="PolyAgent local web application")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--codex-home", help="Optional Codex home directory")
    args = parser.parse_args()
    Handler.app = PolyAgent(ROOT, codex_home=args.codex_home)
    Handler.app.hook_base_url = f"http://127.0.0.1:{args.port}"
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"PolyAgent: http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        Handler.app.close()


if __name__ == "__main__":
    main()
