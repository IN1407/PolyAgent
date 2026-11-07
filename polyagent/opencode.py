"""OpenCode CLI/app-bundled CLI transport over its local HTTP server.

No OpenCode SDK is used. V1 and V2 have distinct published HTTP protocols.
"""

import base64
import json
import os
import queue
import re
import secrets
import subprocess
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


class OpenCodeError(RuntimeError):
    pass


class OpenCodeClient:
    def __init__(self, executable):
        self.executable = executable
        self.process = None
        self.base_url = None
        self.password = secrets.token_urlsafe(32)
        self.protocol = None
        self._output = queue.Queue()
        self.session_agents = {}

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _read_output(self):
        for line in self.process.stdout:
            self._output.put(line.strip())

    def connect(self):
        env = dict(os.environ)
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        env["OPENCODE_SERVER_USERNAME"] = "opencode"
        self.process = subprocess.Popen(
            [self.executable, "serve", "--hostname", "127.0.0.1", "--port", "0"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
            env=env,
        )
        threading.Thread(target=self._read_output, daemon=True).start()
        deadline = time.monotonic() + 25
        lines = []
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise OpenCodeError("OpenCode server exited: " + " | ".join(lines[-4:]))
            try:
                line = self._output.get(timeout=0.25)
            except queue.Empty:
                continue
            lines.append(line)
            match = re.search(r"server listening on (http://(?:127\.0\.0\.1|localhost):\d+)", line)
            if match:
                self.base_url = match.group(1)
                break
        if not self.base_url:
            self.close()
            raise OpenCodeError("OpenCode server did not announce a local HTTP address: " + " | ".join(lines[-4:]))
        try:
            try:
                info = self.request("GET", "/api/info", timeout=5)
                if not isinstance(info, dict):
                    raise OpenCodeError("Unexpected OpenCode V2 server response")
                self.protocol = "v2"
            except OpenCodeError:
                info = self.request("GET", "/global/health", timeout=5)
                if not isinstance(info, dict) or not info.get("healthy"):
                    raise OpenCodeError("OpenCode server health check failed")
                self.protocol = "v1"
            return self.models()
        except Exception:
            self.close()
            raise

    def request(self, method, path, body=None, *, directory=None, timeout=30):
        if not self.base_url:
            raise OpenCodeError("OpenCode server is not connected")
        if directory and self.protocol == "v1":
            path += ("&" if "?" in path else "?") + urlencode({"directory": directory})
        token = base64.b64encode(("opencode:" + self.password).encode()).decode()
        headers = {"Authorization": "Basic " + token, "Accept": "application/json"}
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        request = Request(self.base_url + path, data=payload, headers=headers, method=method)
        try:
            with urlopen(request, timeout=timeout) as response:
                raw = response.read()
            return json.loads(raw) if raw else None
        except HTTPError as exc:
            detail = exc.read(1200).decode(errors="replace")
            raise OpenCodeError(f"OpenCode HTTP {exc.code}: {detail}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise OpenCodeError(f"OpenCode request failed: {exc}") from exc

    def models(self):
        if self.protocol == "v2":
            result = self.request("GET", "/api/model")
            models = result.get("data", [])
            return {f"{m['providerID']}/{m['id']}": m for m in models
                    if m.get("providerID") and m.get("id")}
        result = self.request("GET", "/provider")
        connected = set(result.get("connected", []))
        models = {}
        for provider in result.get("all", []):
            if provider.get("id") not in connected:
                continue
            for model_id, info in provider.get("models", {}).items():
                models[f"{provider['id']}/{model_id}"] = info
        return models

    def agents(self):
        result = self.request("GET", "/api/agent" if self.protocol == "v2" else "/agent")
        rows = result.get("data", []) if isinstance(result, dict) else result
        return [{"value": item.get("id") or item["name"], "label": item["name"],
                 "description": item.get("description", "")}
                for item in rows if not item.get("hidden") and item.get("mode") != "subagent"]

    def permission_settings(self, settings):
        approval = (settings or {}).get("approval", "harness")
        if approval == "harness":
            return {}
        if self.protocol == "v2":
            return {"permissions": [{"action": "*", "resource": "*", "effect": approval}]}
        return {"permission": [{"permission": "*", "pattern": "*", "action": approval}]}

    def start_chat(self, directory, model, settings=None):
        extra = self.permission_settings(settings)
        agent = (settings or {}).get("execution", "harness")
        if agent != "harness":
            extra["agent"] = agent
        if self.protocol == "v2":
            provider, native = model.split("/", 1)
            result = self.request("POST", "/api/session",
                                  {"location": {"directory": directory},
                                   "model": {"providerID": provider, "id": native}, **extra})
            session = result.get("data", {})
        else:
            session = self.request("POST", "/session", extra, directory=directory)
        session_id = session.get("id")
        if not session_id:
            raise OpenCodeError("OpenCode did not return a session ID")
        if agent != "harness":
            self.session_agents[session_id] = agent
        return session_id

    def apply_settings(self, session_id, directory, settings):
        session = quote(session_id, safe="")
        body = self.permission_settings(settings)
        if body:
            if self.protocol == "v2":
                self.request("PATCH", "/api/session/" + session, body, directory=directory)
            else:
                native = self.request("GET", "/session/" + session, directory=directory)
                if native.get("permission", []) != body["permission"]:
                    raise OpenCodeError("OpenCode V1 fixes session permission rules at creation. Start a new chat to change them.")
        agent = settings.get("execution", "harness")
        if agent != "harness" and self.protocol == "v2":
            self.request("POST", f"/api/session/{session}/agent", {"agent": agent})
        elif agent != "harness":
            self.session_agents[session_id] = agent

    def permissions(self, session_id, directory):
        if self.protocol == "v2":
            result = self.request("GET", f"/api/session/{quote(session_id, safe='')}/permission")
            return result.get("data", [])
        result = self.request("GET", "/permission", directory=directory)
        return [item for item in result if item.get("sessionID") == session_id]

    def answer_permission(self, session_id, request_id, directory, decision):
        if self.protocol == "v2":
            return self.request("POST", f"/api/session/{quote(session_id, safe='')}/permission/{quote(request_id, safe='')}/reply",
                                {"decision": "once" if decision == "accept" else "reject"})
        return self.request("POST", f"/permission/{quote(request_id, safe='')}/reply",
                            {"reply": "once" if decision == "accept" else "reject"}, directory=directory)

    def send(self, session_id, message, model, directory):
        session = quote(session_id, safe="")
        if self.protocol == "v2":
            return self.request("POST", f"/api/session/{session}/prompt", {"text": message}, timeout=30)
        provider, native = model.split("/", 1)
        return self.request("POST", f"/session/{session}/message",
                            {"model": {"providerID": provider, "modelID": native},
                             **({"agent": self.session_agents[session_id]} if session_id in self.session_agents else {}),
                             "parts": [{"type": "text", "text": message}]},
                            directory=directory, timeout=600)

    def messages(self, session_id, directory):
        session = quote(session_id, safe="")
        if self.protocol == "v2":
            result = self.request("GET", f"/api/session/{session}/message?order=asc&limit=200", timeout=15)
            return result.get("data", [])
        return self.request("GET", f"/session/{session}/message", directory=directory, timeout=15)

    def active(self, session_id):
        if self.protocol == "v2":
            result = self.request("GET", "/api/session/active", timeout=10)
            return session_id in result.get("data", {})
        result = self.request("GET", "/session/status", timeout=10)
        return result.get(session_id, {}).get("type") not in (None, "idle")

    def interrupt(self, session_id, directory):
        session = quote(session_id, safe="")
        path = f"/api/session/{session}/interrupt" if self.protocol == "v2" else f"/session/{session}/abort"
        return self.request("POST", path, {}, directory=directory, timeout=15)

    def slash_command(self, session_id, command, arguments, model, directory):
        session = quote(session_id, safe="")
        if self.protocol == "v2":
            return self.request("POST", f"/api/session/{session}/command",
                                {"name": command, "text": arguments}, timeout=600)
        provider, native = model.split("/", 1)
        return self.request("POST", f"/session/{session}/command",
                            {"command": command, "arguments": arguments,
                             **({"agent": self.session_agents[session_id]} if session_id in self.session_agents else {}),
                             "model": {"providerID": provider, "modelID": native}},
                            directory=directory, timeout=600)
