"""Client for the installed Antigravity desktop app's local HTTPS interface.

This uses the app's own transport. Connection uses the running app's interface
and authentication response without requiring a particular app version.
"""

import json
import os
import re
import shlex
import ssl
import subprocess
import threading
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class AntigravityError(RuntimeError):
    pass


class AntigravityClient:
    SERVICE = "exa.language_server_pb.LanguageServerService"

    def __init__(self, app_path):
        self.app_path = Path(app_path).resolve()
        self.port = None
        self.context = None
        self.csrf_token = None
        self.version = None
        self._project_lock = threading.RLock()
        self._initialized_projects = set()

    def connect(self):
        executable = str(self.app_path / "Contents/Resources/bin/language_server")
        listing = subprocess.run(["lsof", "-nP", "-Fpcn", "-iTCP", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=8, check=False)
        if listing.returncode:
            raise AntigravityError("Cannot inspect the Antigravity app's local listener")
        pid = None
        ports = []
        for line in listing.stdout.splitlines():
            if line.startswith("p") and line[1:].isdigit():
                pid = line[1:]
            elif line.startswith("n127.0.0.1:") and pid:
                port_text = line.split(":", 1)[1]
                if port_text.isdigit():
                    ports.append((pid, int(port_text)))
        for pid, port in ports:
            command = subprocess.run(["ps", "-p", pid, "-o", "args="], capture_output=True,
                                     text=True, timeout=3, check=False).stdout
            if not command.startswith(executable + " "):
                continue
            try:
                pem = ssl.get_server_certificate(("localhost", port), timeout=3)
                context = ssl.create_default_context(cadata=pem)
                with urlopen(f"https://localhost:{port}/", context=context, timeout=4) as response:
                    html = response.read(100_000).decode()
                match = re.search(r"window\.__APP_CONFIG__\s*=\s*(\{.*?\})\s*;", html)
                if not match:
                    continue
                config = json.loads(match.group(1))
                if config.get("productName") != "antigravity":
                    continue
                token = config.get("csrfToken")
                if not token:
                    raise AntigravityError("Antigravity did not provide its local request token")
                self.port, self.context, self.csrf_token = port, context, token
                self.version = config.get("appVersion")
                auth = self.call("GetAuthStatus", {})
                if not auth.get("authResult", {}).get("hasValidAuth"):
                    raise AntigravityError("Antigravity app is not signed in")
                return self
            except (OSError, URLError, ValueError, ssl.SSLError):
                continue
        raise AntigravityError("No running Antigravity desktop app with an accessible local interface was found")

    def call(self, method, payload, timeout=30):
        if self.port is None:
            raise AntigravityError("Antigravity app is not connected")
        url = f"https://localhost:{self.port}/{self.SERVICE}/{method}"
        request = Request(url, data=json.dumps(payload).encode(), method="POST",
                          headers={"Content-Type": "application/json", "Connect-Protocol-Version": "1",
                                   "x-codeium-csrf-token": self.csrf_token})
        try:
            with urlopen(request, context=self.context, timeout=timeout) as response:
                return json.load(response)
        except HTTPError as exc:
            try:
                error = json.load(exc)
                detail = error.get("message", str(exc))
            except (ValueError, OSError):
                detail = str(exc)
            raise AntigravityError(f"Antigravity {method}: {detail}") from exc
        except (OSError, URLError, ValueError) as exc:
            raise AntigravityError(f"Antigravity {method}: {exc}") from exc

    def models(self):
        catalog = self.call("GetAvailableModels", {}).get("response", {}).get("models", {})
        return {key: value for key, value in catalog.items()
                if value.get("displayName") and not value.get("isInternal") and value.get("model")}

    def ensure_project(self, project):
        project_id = project["id"]
        expected_uri = Path(project["path"]).resolve().as_uri()
        with self._project_lock:
            if project_id in self._initialized_projects:
                return
            ids = self.call("ListProjects", {}).get("projectIds", [])
            if project_id in ids:
                existing = self.call("ReadProject", {"id": project_id}).get("project", {})
                resources = existing.get("projectResources", {}).get("resources", [])
                if not any(resource.get("folderUri") == expected_uri for resource in resources):
                    raise AntigravityError("Existing Antigravity project has a different working directory")
            else:
                self.call("CreateProject", {"project": {
                    "id": project_id, "name": project["name"], "isWorkspaceOnly": True,
                    "projectResources": {"resources": [{"folderUri": expected_uri}]}}})
            self._initialized_projects.add(project_id)

    @staticmethod
    def install_subagent_hook(project, bridge_script, endpoint):
        root = Path(project["path"]).resolve()
        agents = root / ".agents"
        hooks_file = agents / "hooks.json"
        if agents.is_symlink() or hooks_file.is_symlink():
            raise AntigravityError("Antigravity hook path must not be a symbolic link")
        agents.mkdir(exist_ok=True)
        try:
            hooks = json.loads(hooks_file.read_text()) if hooks_file.exists() else {}
        except (OSError, ValueError) as exc:
            raise AntigravityError("Existing Antigravity hooks.json cannot be read") from exc
        if not isinstance(hooks, dict):
            raise AntigravityError("Existing Antigravity hooks.json is not a JSON object")
        command = f"{shlex.quote(os.sys.executable)} {shlex.quote(str(bridge_script))} {shlex.quote(endpoint)}"
        entry = {"PreToolUse": [{"matcher": "invoke_subagent", "hooks": [
            {"type": "command", "command": command, "timeout": 1800}]}]}
        previous = hooks.get("polyagent-native-subagents")
        if previous and previous != entry:
            try:
                old_command = previous["PreToolUse"][0]["hooks"][0]["command"]
            except (KeyError, IndexError, TypeError):
                old_command = ""
            if str(bridge_script) not in old_command or "/api/hooks/antigravity" not in old_command:
                raise AntigravityError("A conflicting PolyAgent subagent hook already exists")
        if previous == entry:
            return
        hooks["polyagent-native-subagents"] = entry
        temporary = hooks_file.with_name("hooks.json.polyagent-tmp")
        temporary.write_text(json.dumps(hooks, indent=2) + "\n")
        os.replace(temporary, hooks_file)

    def start_chat(self, project, model_info):
        self.ensure_project(project)
        response = self.call("StartCascade", {"source": 1,
                             "projectEnvConfig": {"projectId": project["id"],
                                                  "defaultProjectEnvironment": {}},
                             "requestedModel": model_info["model"]})
        chat_id = response.get("cascadeId")
        if not chat_id:
            raise AntigravityError("Antigravity did not create a conversation")
        return chat_id

    def send(self, chat_id, message, model_info, settings=None):
        planner = {"planModel": model_info["model"]}
        tools = {}
        approval = (settings or {}).get("approval", "harness")
        if approval != "harness":
            command_policy = {"request-review": 1, "auto": 2, "always-proceed": 3,
                              "proceed-in-sandbox": 4}[approval]
            tools["runCommand"] = {"autoCommandConfig": {"autoExecutionPolicy": command_policy}}
        artifact = (settings or {}).get("artifact_review", "harness")
        if artifact != "harness":
            tools["notifyUser"] = {"artifactReviewMode": {
                "request-review": 1, "auto": 3, "always-proceed": 2}[artifact]}
        if tools:
            planner["toolConfig"] = tools
        self.call("SendUserCascadeMessage", {"cascadeId": chat_id,
                  "items": [{"text": message}],
                  "cascadeConfig": {"plannerConfig": planner}})

    def trajectory(self, chat_id):
        return self.call("GetCascadeTrajectory", {"cascadeId": chat_id})

    def interrupt(self, chat_id):
        return self.call("CancelCascadeInvocation", {"cascadeId": chat_id}, timeout=15)

    def answer_permission(self, chat_id, trajectory_id, step_index, kind, value, decision):
        approved = decision == "accept"
        if kind == "permission":
            interaction = {"allow": approved, "scope": 1}
        elif kind == "filePermission":
            interaction = {"allow": approved, "scope": 1,
                           "absolutePathUri": value.get("absolutePathUri", "")}
        else:
            interaction = {"confirm": approved}
        return self.call("HandleCascadeUserInteraction", {"cascadeId": chat_id, "interaction": {
            "trajectoryId": trajectory_id, "stepIndex": step_index, kind: interaction}})

    def wait_idle(self, chat_id, timeout=120):
        return self.call("WaitForConversationFullyIdle", {"conversationId": chat_id,
                         "inactivityTimeoutSeconds": timeout}, timeout=timeout + 10)
