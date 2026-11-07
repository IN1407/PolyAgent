"""DeepSeek Harness CLI ACP transport over JSON-RPC stdio; no provider SDK."""

import json
import subprocess
import threading
from queue import Empty, Queue


class DeepSeekError(RuntimeError):
    pass


class DeepSeekClient:
    def __init__(self, executable, on_update=None, on_permission=None, *, command=None, cwd=None):
        self.executable = executable
        self.on_update = on_update
        self.on_permission = on_permission
        self.process = None
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.next_id = 1
        self.stderr = []
        self.active_sessions = set()
        self.command = command or [self.executable, "--profile", "acp"]
        self.cwd = cwd

    def connect(self):
        try:
            self.process = subprocess.Popen(self.command, cwd=self.cwd,
                                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, text=True, bufsize=1)
        except OSError as exc:
            raise DeepSeekError(f"Could not start DeepSeek Harness: {exc}") from exc
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=lambda: self.stderr.extend(self.process.stderr), daemon=True).start()
        self.call("initialize", {"protocolVersion": 1,
                                 "clientCapabilities": {"fs": {"readTextFile": False,
                                                               "writeTextFile": False},
                                                        "terminal": False}}, timeout=45)
        self.notify("initialized", {})
        return self

    def _write(self, message):
        with self.write_lock:
            if not self.process or self.process.poll() is not None:
                raise DeepSeekError("DeepSeek Harness ACP process is not running")
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def _read(self):
        for line in self.process.stdout:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in item and ("result" in item or "error" in item):
                with self.pending_lock:
                    waiter = self.pending.pop(item["id"], None)
                if waiter:
                    waiter.put(item)
            elif "id" in item and "method" in item:
                if self.on_permission and item["method"] == "session/request_permission":
                    try:
                        self.on_permission(self, item)
                    except Exception:
                        self.answer(item["id"], {"outcome": {"outcome": "cancelled"}})
                else:
                    self.answer(item["id"], {"outcome": {"outcome": "cancelled"}})
            elif item.get("method") == "session/update" and self.on_update:
                try:
                    self.on_update(item.get("params") or {})
                except Exception:
                    pass
        with self.pending_lock:
            waiters = list(self.pending.values())
            self.pending.clear()
        for waiter in waiters:
            waiter.put({"error": {"message": "DeepSeek Harness ACP process exited"}})

    def call(self, method, params, timeout=60):
        waiter = Queue(maxsize=1)
        with self.pending_lock:
            request_id = self.next_id
            self.next_id += 1
            self.pending[request_id] = waiter
        try:
            self._write({"jsonrpc": "2.0", "id": request_id,
                         "method": method, "params": params})
            try:
                response = waiter.get(timeout=timeout)
            except Empty as exc:
                raise DeepSeekError(f"DeepSeek Harness {method} timed out") from exc
            if "error" in response:
                error = response["error"]
                raise DeepSeekError(error.get("message", str(error)) if isinstance(error, dict) else str(error))
            return response.get("result") or {}
        finally:
            with self.pending_lock:
                self.pending.pop(request_id, None)

    def notify(self, method, params):
        self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def answer(self, request_id, result):
        self._write({"jsonrpc": "2.0", "id": request_id, "result": result})

    @staticmethod
    def catalog(config_options):
        catalog = {}
        for option in config_options:
            if option.get("id") != "model":
                continue
            for group in option.get("options", []):
                for item in group.get("options", []):
                    try:
                        provider, model = json.loads(item["value"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    catalog[f"{provider}/{model}"] = {
                        "name": item.get("name") or model,
                        "description": item.get("description", ""),
                        "value": item["value"]}
        return catalog

    def discover_models(self, directory):
        probe = self.call("session/new", {"cwd": directory, "mcpServers": []}, timeout=90)
        try:
            return self.catalog(probe.get("configOptions", []))
        finally:
            self.call("session/close", {"sessionId": probe["sessionId"]}, timeout=45)

    def start_chat(self, directory, model_value):
        result = self.call("session/new", {"cwd": directory, "mcpServers": []}, timeout=90)
        session_id = result["sessionId"]
        try:
            self.call("session/set_config_option", {"sessionId": session_id,
                      "configId": "model", "value": model_value}, timeout=60)
        except Exception:
            self.call("session/close", {"sessionId": session_id}, timeout=45)
            raise
        self.active_sessions.add(session_id)
        return session_id

    def resume_chat(self, session_id, directory):
        if session_id not in self.active_sessions:
            self.call("session/resume", {"sessionId": session_id, "cwd": directory,
                                        "mcpServers": []}, timeout=90)
            self.active_sessions.add(session_id)

    def prompt(self, session_id, text, timeout=600):
        return self.call("session/prompt", {"sessionId": session_id,
                         "prompt": [{"type": "text", "text": text}]}, timeout=timeout)

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
