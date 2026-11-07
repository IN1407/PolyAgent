"""Native DeepSeek CLI Web Host transport, including its agent presets.

Only the CLI and its localhost interfaces are used. websocket-client is a
transport dependency; no model-provider or harness SDK is used.
"""

import http.cookiejar
import json
import os
import re
import subprocess
import threading
import time
import uuid
from queue import Queue, Empty
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPCookieProcessor, Request

from .deepseek import DeepSeekError


class DeepSeekWebClient:
    protocol = "web"

    def __init__(self, executable, on_update=None, on_permission=None, *, source="cli"):
        self.executable = executable
        self.on_update = on_update
        self.on_permission = on_permission
        self.process = None
        self.socket = None
        self.base_url = None
        self.jar = http.cookiejar.CookieJar()
        self.http = build_opener(HTTPCookieProcessor(self.jar))
        self.write_lock = threading.Lock()
        self.active_sessions = set()
        self.streams = {}
        self.waiters = {}
        self.ready = threading.Event()
        self.client_id = None
        self.failure = None
        self.presets = []
        self.permission_catalog = {}
        self.session_settings = {}
        self.source = source
        self.native_defaults = {}

    def connect(self):
        try:
            import websocket
        except ImportError as exc:
            raise DeepSeekError("Install PolyAgent's requirements.txt to enable native DeepSeek modes") from exc
        command = [self.executable, "--profile", "web"]
        if self.source == "app":
            home = Path(os.environ.get("DSH_HOME", str(Path.home() / ".dsh"))).expanduser()
            patch = home / "profiles" / "desktop" / "cordis.patch.yml"
            if patch.is_file():
                command.extend(["--patch", str(patch)])
        command.extend(["--no-open", "--port", "0"])
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        urls = Queue()

        def output():
            for line in self.process.stdout:
                match = re.search(r"dsh web: (http://(?:127\.0\.0\.1|localhost):\d+/\S*)", line)
                if match:
                    urls.put(match.group(1))
            urls.put(None)

        threading.Thread(target=output, daemon=True).start()
        try:
            url = urls.get(timeout=90)
            if not url:
                raise DeepSeekError("DeepSeek Web Host exited before announcing its local connection")
            parsed = urlsplit(url)
            self.base_url = f"{parsed.scheme}://{parsed.netloc}"
            # Exchange the CLI's fresh local token for its native Host cookie.
            # Token and cookies stay in memory and are never exposed in UI/logs.
            with self.http.open(url, timeout=20) as response:
                response.read(1)
            cookies = "; ".join(f"{cookie.name}={cookie.value}" for cookie in self.jar)
            self.socket = websocket.create_connection(
                self.base_url.replace("http://", "ws://") + "/api/remote.mux",
                cookie=cookies, origin=self.base_url, timeout=30)
            self.socket.settimeout(None)
            threading.Thread(target=self._read, daemon=True).start()
            self._open("events", "$events", {})
            if not self.ready.wait(25) or self.failure:
                raise DeepSeekError(self.failure or "DeepSeek event channel did not become ready")
            self.refresh_modes()
            return self
        except (Empty, OSError, URLError) as exc:
            self.close()
            raise DeepSeekError("DeepSeek native Host connection failed") from exc
        except DeepSeekError:
            self.close()
            raise
        except Exception as exc:
            self.close()
            raise DeepSeekError(f"DeepSeek native Host connection failed ({type(exc).__name__})") from exc

    def rpc(self, endpoint, args, timeout=45):
        request_id = str(uuid.uuid4())
        body = {"type": "client-request", "rpcId": request_id,
                "method": endpoint, "payload": {"args": args}}
        request = Request(self.base_url + "/api/" + endpoint,
                          data=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json", "Origin": self.base_url})
        try:
            with self.http.open(request, timeout=timeout) as response:
                result = json.load(response)
        except (HTTPError, URLError, OSError, ValueError) as exc:
            raise DeepSeekError(f"DeepSeek {endpoint} connection failed") from exc
        if result.get("rpcId") != request_id:
            raise DeepSeekError("DeepSeek returned a different request identity")
        outcome = result.get("result", {})
        if not outcome.get("ok"):
            raise DeepSeekError(f"DeepSeek {endpoint}: {outcome.get('error', {}).get('message', 'request failed')}")
        return outcome.get("value")

    def _open(self, stream_id, endpoint, args):
        self._write({"type": "open", "streamId": stream_id,
                     "endpoint": endpoint, "payload": {"args": args}})

    def _write(self, value):
        with self.write_lock:
            self.socket.send(json.dumps(value))

    def _read(self):
        try:
            while True:
                raw = self.socket.recv()
                if not raw:
                    raise DeepSeekError("DeepSeek event connection closed")
                frame = json.loads(raw)
                stream = frame.get("streamId")
                if frame.get("type") == "error":
                    raise DeepSeekError(frame.get("error", {}).get("message", "DeepSeek stream failed"))
                if frame.get("type") != "item":
                    continue
                item = frame.get("value") or {}
                if stream == "events":
                    if item.get("type") == "ready":
                        self.client_id = item["clientId"]
                        self.ready.set()
                    elif item.get("type") == "waterfall":
                        self._interaction(item)
                    elif item.get("type") == "emit" and item.get("event") == "api-session/error":
                        session_id, message = item["args"][:2]
                        waiter = self.waiters.get(session_id)
                        if waiter:
                            waiter.put({"error": message})
                elif stream in self.streams:
                    session_id = self.streams[stream]
                    if item.get("type") == "snapshot":
                        self.active_sessions.add(session_id)
                    elif item.get("type") == "event":
                        self._session_event(session_id, item["event"])
        except Exception as exc:
            self.failure = str(exc)
            self.ready.set()
            for waiter in list(self.waiters.values()):
                waiter.put({"error": self.failure})

    def _interaction(self, item):
        if item.get("event") != "approval/request" or not self.on_permission:
            self.rpc("$events/result", {"clientId": self.client_id, "eventId": item["eventId"],
                                       "outcome": {"kind": "next"}})
            return
        request = item.get("request", {})
        self.on_permission(self, {"id": item["eventId"], "params": {
            "sessionId": item["agentId"], "toolCall": request,
            "options": [{"optionId": "allow-once", "kind": "allow_once"},
                        {"optionId": "reject-once", "kind": "reject_once"}]}})

    def answer(self, request_id, result):
        outcome = result.get("outcome", {})
        value = ("cancelled" if outcome.get("outcome") == "cancelled" else
                 "allowed-once" if outcome.get("optionId") == "allow-once" else "rejected")
        self.rpc("$events/result", {"clientId": self.client_id, "eventId": request_id,
                                   "outcome": {"kind": "result", "value": value}})

    def _session_event(self, session_id, event):
        kind, data = event.get("type"), event.get("data", {})
        update = None
        if kind == "assistant/message":
            for part in data.get("message", {}).get("content", []):
                if part.get("type") == "text" and self.on_update:
                    self.on_update({"sessionId": session_id, "update": {
                        "sessionUpdate": "agent_message_chunk", "content": part}})
        elif kind == "tool/call":
            raw = data.get("arguments", "{}")
            try:
                raw = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                pass
            update = {"sessionUpdate": "tool_call", "title": data.get("name"),
                      "toolCallId": data.get("callId"), "rawInput": raw, "status": "in_progress"}
        elif kind == "tool/result":
            update = {"sessionUpdate": "tool_call_update", "status": "completed",
                      "toolCallId": data.get("message", {}).get("toolCallId")}
        elif kind == "turn/end" and session_id in self.waiters:
            reason = data.get("reason") or {}
            if reason.get("kind") == "completed":
                self.waiters[session_id].put({"stopReason": "end_turn"})
            else:
                self.waiters[session_id].put({"error": reason.get("error", {}).get("message") or
                                            f"DeepSeek turn ended: {reason.get('kind', 'unknown')}"})
        if update and self.on_update:
            self.on_update({"sessionId": session_id, "update": update})

    def refresh_modes(self):
        self.presets = self.rpc("agentPresets/list", {}).get("presets", [])
        self.permission_catalog = self.rpc("permissionPresets/catalog", {})
        self.native_defaults = {
            "execution": next((p["id"] for p in self.presets if p.get("isDefault")), None),
            "approval": self.permission_catalog.get("defaultPreset")}

    def discover_models(self, directory=None):
        groups = self.rpc("session/modelCatalog", {}).get("groups", [])
        return {f"{group['id']}/{model['id']}": {
                    "name": model.get("name") or model["id"],
                    "description": model.get("description", ""),
                    "value": json.dumps([group["id"], model["id"]])}
                for group in groups for model in group.get("models", [])}

    def start_chat(self, directory, model_value, settings=None):
        settings = settings or {}
        self.refresh_modes()
        request = {"cwd": directory, "sessionId": "polyagent-" + str(uuid.uuid4())}
        if settings.get("execution") not in (None, "harness"):
            request["agentPreset"] = settings["execution"]
        result = self.rpc("session/create", {"request": request})
        session_id = result["sessionId"]
        provider, model = json.loads(model_value)
        self.rpc("session/selectModel", {"request": {"sessionId": session_id,
                 "provider": provider, "model": model}})
        self.session_settings[session_id] = {"execution": result.get("agentPreset")}
        self.resume_chat(session_id, directory)
        self.apply_settings(session_id, settings)
        return session_id

    def resume_chat(self, session_id, directory):
        stream_id = "session-" + session_id
        if stream_id not in self.streams:
            self.streams[stream_id] = session_id
            self._open(stream_id, "session/follow", {"request": {
                "address": {"kind": "session", "sessionId": session_id}, "maxMessages": 1}})
            deadline = time.monotonic() + 20
            while session_id not in self.active_sessions:
                if self.failure or time.monotonic() > deadline:
                    raise DeepSeekError(self.failure or "DeepSeek session did not become ready")
                time.sleep(.05)

    def apply_settings(self, session_id, settings):
        approval = settings.get("approval", "harness")
        if approval != "harness":
            if self.command(session_id, "/permission " + approval) is None:
                raise DeepSeekError("DeepSeek did not expose its permission command in this session")

    def command(self, session_id, line):
        result = self.rpc("commands/execute", {"agentId": session_id,
                          "line": line, "submittedAttachments": []})
        if result is not None and result.get("result", {}).get("kind") != "success":
            raise DeepSeekError(result.get("result", {}).get("text") or "DeepSeek command failed")
        return result

    def prompt(self, session_id, text, timeout=600):
        if text.startswith("/") and not text.startswith("//"):
            result = self.command(session_id, text)
            if result is not None:
                content = result.get("result", {}).get("text") or "Command completed."
                if self.on_update:
                    self.on_update({"sessionId": session_id, "update": {
                        "sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": content}}})
                return {"stopReason": "end_turn"}
        waiter = Queue()
        self.waiters[session_id] = waiter
        try:
            self.rpc("session/prompt", {"request": {"sessionId": session_id,
                     "requestId": str(uuid.uuid4()), "mode": "steer",
                     "content": [{"type": "text", "text": text}]}})
            result = waiter.get(timeout=timeout)
            if result.get("error"):
                raise DeepSeekError(result["error"])
            return result
        except Empty as exc:
            try:
                self.rpc("session/cancel", {"request": {"sessionId": session_id}})
            except DeepSeekError:
                pass
            raise DeepSeekError("DeepSeek turn timed out") from exc
        finally:
            self.waiters.pop(session_id, None)

    def interrupt(self, session_id):
        return self.rpc("session/cancel", {"request": {"sessionId": session_id}})

    def call(self, method, params, timeout=60):
        if method == "session/close":
            session_id = params["sessionId"]
            stream_id = "session-" + session_id
            if stream_id in self.streams:
                self._write({"type": "cancel", "streamId": stream_id})
                self.streams.pop(stream_id, None)
            self.active_sessions.discard(session_id)
            return {}
        raise DeepSeekError("This operation requires an ACP session")

    def close(self):
        if self.socket:
            self.socket.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
