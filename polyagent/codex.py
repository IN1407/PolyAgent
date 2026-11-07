"""Small JSON-RPC client for the installed Codex app-server."""

import json
import os
import queue
import shutil
import subprocess
import threading
from typing import Callable


class CodexError(RuntimeError):
    pass


class CodexClient:
    def __init__(self, on_event: Callable[[dict], None], executable: str | None = None):
        self.executable = executable or shutil.which("codex")
        self.on_event = on_event
        self.process = None
        self.pending = {}
        self.lock = threading.Lock()
        self.serial = 0
        self.stderr_lines = []

    def connect(self):
        if not self.executable:
            raise CodexError("Codex CLI was not found on PATH")
        if self.process and self.process.poll() is None:
            return
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=os.environ.copy(),
        )
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        try:
            self.call("initialize", {"clientInfo": {"name": "polyagent", "title": "PolyAgent", "version": "1.0.0"},
                                     "capabilities": {"experimentalApi": True}})
            self.notify("initialized")
        except Exception:
            self.process.terminate()
            raise

    def _read_stderr(self):
        for line in self.process.stderr:
            self.stderr_lines.append(line.strip())
            self.stderr_lines = self.stderr_lines[-20:]

    def _read(self):
        for line in self.process.stdout:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in message and ("result" in message or "error" in message):
                pending = self.pending.pop(message["id"], None)
                if pending:
                    pending.put(message)
            elif "method" in message:
                try:
                    self.on_event(message)
                except Exception as exc:
                    self.stderr_lines.append(f"event handling failed: {exc}")
                    if "id" in message:
                        try:
                            self.answer_error(message["id"], str(exc))
                        except CodexError:
                            pass
        for pending in list(self.pending.values()):
            pending.put({"error": {"message": "Codex app-server exited"}})
        self.pending.clear()

    def _write(self, payload):
        if not self.process or self.process.poll() is not None:
            detail = self.stderr_lines[-1] if self.stderr_lines else "process unavailable"
            raise CodexError(f"Codex app-server is not running: {detail}")
        with self.lock:
            self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.process.stdin.flush()

    def call(self, method, params=None, timeout=30):
        with self.lock:
            self.serial += 1
            request_id = self.serial
            pending = queue.Queue(maxsize=1)
            self.pending[request_id] = pending
        try:
            self._write({"id": request_id, "method": method, "params": params or {}})
            response = pending.get(timeout=timeout)
        except queue.Empty as exc:
            raise CodexError(f"Codex timed out answering {method}") from exc
        finally:
            self.pending.pop(request_id, None)
        if "error" in response:
            raise CodexError(f"{method}: {response['error']}")
        return response.get("result", {})

    def notify(self, method, params=None):
        payload = {"method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def answer(self, request_id, result):
        self._write({"id": request_id, "result": result})

    def answer_error(self, request_id, message):
        self._write({"id": request_id, "error": {"code": -32000, "message": message}})

    def models(self):
        result = []
        cursor = None
        while True:
            page = self.call("model/list", {"limit": 100, "cursor": cursor})
            result.extend(page.get("data", []))
            cursor = page.get("nextCursor")
            if not cursor:
                return result

    @staticmethod
    def mode_params(settings, *, turn=False):
        settings = settings or {}
        params = {}
        if settings.get("approval") not in (None, "harness"):
            params["approvalPolicy"] = settings["approval"]
        if settings.get("sandbox") not in (None, "harness"):
            if turn:
                names = {"read-only": "readOnly", "workspace-write": "workspaceWrite", "danger-full-access": "dangerFullAccess"}
                params["sandboxPolicy"] = {"type": names[settings["sandbox"]]}
            else:
                params["sandbox"] = settings["sandbox"]
        return params

    def start_thread(self, cwd, model, *, ephemeral=False, readonly=False, settings=None):
        params = {"cwd": cwd, "model": model, "ephemeral": ephemeral, **self.mode_params(settings)}
        if readonly:
            params.update({"sandbox": "read-only", "approvalPolicy": "never"})
        return self.call("thread/start", params)["thread"]["id"]

    def resume_thread(self, thread_id, cwd, settings=None):
        return self.call("thread/resume", {"threadId": thread_id, "cwd": cwd, "excludeTurns": True,
                                            **self.mode_params(settings)})

    def start_turn(self, thread_id, text, model=None, output_schema=None, settings=None):
        params = {"threadId": thread_id, "input": [{"type": "text", "text": text}],
                  **self.mode_params(settings, turn=True)}
        if model:
            params["model"] = model
        if output_schema:
            params["outputSchema"] = output_schema
        if settings and settings.get("execution") not in (None, "harness"):
            params["collaborationMode"] = {"mode": settings["execution"],
                "settings": {"model": model, "reasoning_effort": None, "developer_instructions": None}}
        return self.call("turn/start", params)["turn"]["id"]

    def interrupt_turn(self, thread_id, turn_id):
        return self.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
