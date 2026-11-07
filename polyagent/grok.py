"""Grok Build CLI transport. This invokes the harness executable, not an API SDK."""

import json
import re
import subprocess
import threading
import uuid
from .deepseek import DeepSeekClient, DeepSeekError


class GrokError(RuntimeError):
    pass


class GrokClient:
    def __init__(self, executable, on_permission=None):
        self.executable = executable
        self.on_permission = on_permission
        self.active_clients = {}
        self.active_lock = threading.Lock()

    def interrupt(self, session_id):
        with self.active_lock:
            client = self.active_clients.get(session_id)
        if not client:
            raise GrokError("Grok turn is no longer running")
        client.notify("session/cancel", {"sessionId": session_id})

    def connect(self):
        try:
            result = subprocess.run([self.executable, "models"], text=True,
                                    capture_output=True, timeout=45, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GrokError(f"Grok model discovery failed: {exc}") from exc
        if result.returncode:
            raise GrokError((result.stderr or result.stdout).strip()[-500:] or "Grok models failed")
        if "You are not authenticated." in result.stdout:
            raise GrokError("Sign in with `grok login` or configure XAI_API_KEY")
        catalog = {}
        in_models = False
        for line in result.stdout.splitlines():
            if line.strip() == "Available models:":
                in_models = True
                continue
            if not in_models:
                continue
            match = re.fullmatch(r"\s+[*-]\s+(\S+)(?:\s+\(default\))?\s*", line)
            if match:
                model = match.group(1)
                catalog[model] = {"name": model}
        if not catalog:
            raise GrokError("Grok returned no available models")
        return catalog

    def start_chat(self):
        return str(uuid.uuid4())

    def run(self, session_id, directory, prompt, model, *, first=False, orchestrator=False,
            on_event=None, timeout=600, settings=None):
        settings = settings or {}
        command = [self.executable, "--no-auto-update", "--cwd", directory]
        if orchestrator:
            command.append("--trust")
        command.extend(["agent", "-m", model, "stdio"])
        chunks = []

        def update(params):
            item = params.get("update") or {}
            kind = item.get("sessionUpdate")
            if kind == "agent_message_chunk" and item.get("content", {}).get("type") == "text":
                value = item["content"]["text"]
                chunks.append(value)
                if on_event:
                    on_event({"type": "text", "data": value})
            elif kind in {"tool_call", "tool_call_update"} and on_event:
                on_event({**item, "type": kind})

        client = DeepSeekClient(self.executable, update, self.on_permission, command=command, cwd=directory)
        try:
            client.connect()
            with self.active_lock:
                self.active_clients[session_id] = client
            meta = {"sessionId": session_id}
            approval = settings.get("approval", "harness")
            if approval != "harness":
                meta.update({"yoloMode": approval == "always-approve", "autoMode": approval == "auto"})
            params = {"cwd": directory, "mcpServers": [], "_meta": meta}
            if first:
                result = client.call("session/new", params, timeout=90)
                if result.get("sessionId") != session_id:
                    raise GrokError("Grok created a different session identity")
            else:
                client.call("session/resume", {**params, "sessionId": session_id}, timeout=90)
            client.call("session/set_config_option", {"sessionId": session_id,
                        "configId": "model", "value": model})
            if settings.get("execution") not in (None, "harness"):
                client.call("session/set_mode", {"sessionId": session_id, "modeId": settings["execution"]})
            # Historical replay during resume is excluded from the current answer.
            chunks.clear()
            client.prompt(session_id, prompt, timeout)
        except DeepSeekError as exc:
            raise GrokError(str(exc).replace("DeepSeek Harness", "Grok Build")) from exc
        finally:
            with self.active_lock:
                if self.active_clients.get(session_id) is client:
                    self.active_clients.pop(session_id, None)
            client.close()
        answer = "".join(chunks).strip()
        if not answer:
            raise GrokError("Grok completed without a text response")
        return answer
