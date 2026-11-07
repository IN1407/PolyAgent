"""Local PolyAgent backend. Coding tools remain inside connected harnesses."""

import json
import copy
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from queue import Empty, Queue

from .antigravity import AntigravityClient, AntigravityError
from .antigravity_cli import AntigravityCliClient, AntigravityCliError
from .codex import CodexClient, CodexError
from .deepseek import DeepSeekClient, DeepSeekError
from .deepseek_web import DeepSeekWebClient
from .discovery import bundled_cli, codex_app_cli, discover, HARNESSES
from .grok import GrokClient, GrokError
from .opencode import OpenCodeClient, OpenCodeError
from .modes import (STATIC as HARNESS_MODES, INHERIT, ANTIGRAVITY_CLI_FIELDS,
                    options as mode_options, validate as validate_modes, compatible as compatible_modes)


FIRST_MESSAGE = "you are a subagent working under a ai orchestrator. all messages excluding these sentences are from orchestrator. orchestrator says: '[prompt]'"
MODES = {"full_auto", "manual", "hybrid", "ask_every_time"}
PREFERENCES = {"neutral", "prefer", "strongly_prefer", "force", "exclude"}


def first_subagent_message(prompt):
    return FIRST_MESSAGE.replace("[prompt]", prompt)


def distinct_model_labels(names, fallback):
    """Give different native model IDs different labels within one harness."""
    counts = Counter(name.casefold() for name in names.values())
    labels = {model_id: (fallback(model_id) if counts[name.casefold()] > 1 else name)
              for model_id, name in names.items()}
    collisions = Counter(name.casefold() for name in labels.values())
    for model_id, name in list(labels.items()):
        if collisions[name.casefold()] > 1:
            labels[model_id] = f"{name} [{model_id}]"
    used = set()
    for model_id in sorted(labels):
        name = labels[model_id]
        while name.casefold() in used:
            name = f"{name} [{model_id}]"
        labels[model_id] = name
        used.add(name.casefold())
    return labels


class PolyAgent:
    def __init__(self, root: Path, codex_home: str | None = None):
        self.root = Path(root).resolve()
        self.data = self.root / "data"
        self.data.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.data / "polyagent.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self._init_db()
        self.catalog = {}
        self.account = None
        self.connected = False
        self.waiters = {}
        self.completed_turns = {}
        self.turn_messages = {}
        self.approvals = {}
        self.native_models = {}
        self.codex_source = None
        self.antigravity = None
        self.antigravity_cli = None
        self.antigravity_source = None
        self.antigravity_catalog = {}
        self.opencode = None
        self.opencode_catalog = {}
        self.opencode_source = None
        self.deepseek = None
        self.deepseek_catalog = {}
        self.deepseek_source = None
        self.grok = None
        self.grok_catalog = {}
        self.grok_source = None
        self.grok_followups = {}
        self.grok_followups_lock = threading.Lock()
        self.acp_turn_text = {}
        self.acp_permissions = {}
        self.opencode_permissions = {}
        self.antigravity_permissions = {}
        self.cli_waiters = {}
        self.cli_results = {}
        self.hook_base_url = None
        self.pending_model_choices = {}
        self.conversation_locks = {}
        self.active_turns = {}
        if codex_home:
            os.environ["CODEX_HOME"] = codex_home
        self.codex = CodexClient(self._on_codex_event)
        self._recover_connections()

    def _recover_connections(self):
        # Runtime requests belong to one server process; durable histories remain.
        for chat in self._all("SELECT id,project_id,harness,model FROM chats WHERE status='running'"):
            self._execute("UPDATE chats SET status='interrupted' WHERE id=?", (chat["id"],))
            self.event(chat["project_id"], chat["id"], "turn", "interrupted",
                       {"harness": chat["harness"], "model": chat["model"],
                        "reason": "PolyAgent restarted; send a message to resume the native session."})
        resolved = {json.loads(row["metadata"]).get("request_id")
                    for row in self._all("SELECT metadata FROM events WHERE kind='request_resolved'")}
        for row in self._all("SELECT project_id,chat_id,metadata FROM events WHERE kind IN ('approval','user_input','model_choice_required')"):
            request_id = json.loads(row["metadata"]).get("request_id")
            if request_id and request_id not in resolved:
                self.event(row["project_id"], row["chat_id"], "request_resolved", "Expired after server restart",
                           {"request_id": request_id})
                resolved.add(request_id)

    def close(self):
        for client in (self.deepseek, self.opencode):
            if client:
                client.close()
        if self.antigravity_cli:
            for process in list(self.antigravity_cli.sessions.values()):
                if process.poll() is None:
                    process.terminate()
        if self.codex.process and self.codex.process.poll() is None:
            self.codex.process.terminate()

    def _init_db(self):
        with self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS projects (
                  id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL UNIQUE,
                  mode TEXT NOT NULL DEFAULT 'manual', orchestrator_model TEXT,
                  chooser_model TEXT, subagent_model TEXT,
                  locked_orchestrator INTEGER NOT NULL DEFAULT 0,
                  locked_subagent INTEGER NOT NULL DEFAULT 0,
                  created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS chats (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, role TEXT NOT NULL,
                  harness TEXT NOT NULL, model TEXT NOT NULL, thread_id TEXT NOT NULL UNIQUE,
                  status TEXT NOT NULL, source TEXT, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                  id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                  chat_id TEXT, kind TEXT NOT NULL, text TEXT NOT NULL,
                  metadata TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS models (
                  id TEXT PRIMARY KEY, harness TEXT NOT NULL, display_name TEXT NOT NULL,
                  metadata TEXT NOT NULL, discovered_at REAL NOT NULL,
                  enabled INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS research (
                  model_id TEXT PRIMARY KEY, strengths TEXT NOT NULL DEFAULT '[]',
                  weaknesses TEXT NOT NULL DEFAULT '[]', evidence TEXT NOT NULL DEFAULT '[]',
                  conclusion TEXT NOT NULL DEFAULT '', updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS overrides (
                  model_id TEXT PRIMARY KEY, strengths TEXT NOT NULL DEFAULT '',
                  weaknesses TEXT NOT NULL DEFAULT '', assessment TEXT NOT NULL DEFAULT '',
                  weakness_policy TEXT NOT NULL DEFAULT 'normal',
                  preference TEXT NOT NULL DEFAULT 'neutral');
                CREATE TABLE IF NOT EXISTS subagent_links (
                  external_id TEXT PRIMARY KEY, parent_chat_id TEXT NOT NULL,
                  child_chat_id TEXT NOT NULL, created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS conversations (
                  id TEXT PRIMARY KEY, project_id TEXT NOT NULL, title TEXT NOT NULL,
                  settings TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS harness_preferences (
                  harness TEXT PRIMARY KEY, settings TEXT NOT NULL);
            """)
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(projects)")}
            if "subagent_model" not in columns:
                self.db.execute("ALTER TABLE projects ADD COLUMN subagent_model TEXT")
            if "locked_subagent" not in columns:
                self.db.execute("ALTER TABLE projects ADD COLUMN locked_subagent INTEGER NOT NULL DEFAULT 0")
            chat_columns = {row[1] for row in self.db.execute("PRAGMA table_info(chats)")}
            if "source" not in chat_columns:
                self.db.execute("ALTER TABLE chats ADD COLUMN source TEXT")
            if "conversation_id" not in chat_columns:
                self.db.execute("ALTER TABLE chats ADD COLUMN conversation_id TEXT")
            event_columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
            if "conversation_id" not in event_columns:
                self.db.execute("ALTER TABLE events ADD COLUMN conversation_id TEXT")
            for row in self.db.execute("SELECT DISTINCT project_id FROM chats WHERE conversation_id IS NULL").fetchall():
                conversation_id = str(uuid.uuid4())
                self.db.execute("INSERT INTO conversations(id,project_id,title,created_at) VALUES(?,?,?,?)",
                                (conversation_id, row[0], "Chat 1", time.time()))
                self.db.execute("UPDATE chats SET conversation_id=? WHERE project_id=? AND conversation_id IS NULL",
                                (conversation_id, row[0]))
                self.db.execute("UPDATE events SET conversation_id=? WHERE project_id=? AND conversation_id IS NULL",
                                (conversation_id, row[0]))

    def _execute(self, sql, args=()):
        with self.lock, self.db:
            return self.db.execute(sql, args)

    def _one(self, sql, args=()):
        with self.lock:
            return self.db.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        with self.lock:
            return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def event(self, project_id, chat_id, kind, text, metadata=None, *, conversation_id=None):
        if chat_id:
            chat = self._one("SELECT conversation_id FROM chats WHERE id=?", (chat_id,))
            conversation_id = chat["conversation_id"] if chat else conversation_id
        self._execute("INSERT INTO events(project_id,chat_id,kind,text,metadata,created_at,conversation_id) VALUES(?,?,?,?,?,?,?)",
                      (project_id, chat_id, kind, text, json.dumps(metadata or {}), time.time(), conversation_id))

    def conversations(self, project_id):
        self.project(project_id)
        rows = self._all("SELECT * FROM conversations WHERE project_id=? ORDER BY created_at", (project_id,))
        capabilities = self.harness_modes()
        for row in rows:
            row["settings"] = {harness: self._compatible_execution_settings(harness, values, capabilities.get(harness, {}))
                               for harness, values in json.loads(row["settings"]).items()}
        return rows

    def create_conversation(self, project_id):
        self.project(project_id)
        conversation_id = str(uuid.uuid4())
        self._execute("INSERT INTO conversations(id,project_id,title,created_at) VALUES(?,?,?,?)",
                      (conversation_id, project_id, "New chat", time.time()))
        return next(row for row in self.conversations(project_id) if row["id"] == conversation_id)

    def conversation(self, project_id, conversation_id):
        row = self._one("SELECT * FROM conversations WHERE id=? AND project_id=?", (conversation_id, project_id))
        if not row:
            raise ValueError("Chat does not belong to this project")
        result = dict(row)
        result["settings"] = json.loads(result["settings"])
        return result

    def harness_modes(self):
        result = copy.deepcopy(HARNESS_MODES)
        if self.deepseek_status()["running"]:
            # Discover native catalogs and native defaults without a probe chat.
            result["deepseek_harness"]["execution"] = mode_options(INHERIT) + [
                {"value": item["id"], "label": item.get("name") or {"ptc": "PTC", "cordis": "Create"}.get(item["id"], item["id"].title()),
                 "description": item.get("description", "")}
                for item in self.deepseek.presets if not item.get("broken")]
            result["deepseek_harness"]["approval"] = mode_options(INHERIT) + [
                {"value": item["value"], "label": item.get("name") or item["value"],
                 "description": item.get("description", "")}
                for item in self.deepseek.permission_catalog.get("options", [])
                if item["value"] != "custom"]
            result["deepseek_harness"]["native_defaults"] = self.deepseek.native_defaults
        if self.opencode_status()["running"]:
            result["opencode"]["approval_creation_only"] = self.opencode.protocol == "v1"
            result["opencode"]["fields"][0]["creation_only"] = self.opencode.protocol == "v1"
            try:
                result["opencode"]["execution"] = mode_options(INHERIT) + self.opencode.agents()
            except OpenCodeError:
                result["opencode"]["execution"] = mode_options(INHERIT)
        if self.antigravity_source == "cli":
            result["antigravity"]["fields"] = copy.deepcopy(ANTIGRAVITY_CLI_FIELDS)
            result["antigravity"].pop("artifact_review", None)
            result["antigravity"]["approval_note"] = "The native CLI applies its permission policy; it denies actions that would require an interactive prompt."
            result["antigravity"]["approval"] = mode_options(INHERIT, ("request-review", "Request review"),
                ("proceed-in-sandbox", "Proceed in sandbox"), ("always-proceed", "Always proceed"), ("strict", "Strict"))
            result["antigravity"]["execution"] = mode_options(INHERIT, ("default", "Default"),
                ("accept-edits", "Accept edits"), ("auto", "Auto"), ("plan", "Plan"))
        for harness, value in result.items():
            row = self._one("SELECT settings FROM harness_preferences WHERE harness=?", (harness,))
            value["last_used"] = self._compatible_execution_settings(harness, json.loads(row["settings"]) if row else {}, value)
        return result

    def _compatible_execution_settings(self, harness, values, capability):
        values = dict(values)
        if harness == "antigravity" and "artifact_review" in capability and "artifact_review" not in values:
            # Preserve the artifact behavior of the older combined app selector.
            legacy = {"request-review": "request-review", "proceed-in-sandbox": "request-review",
                      "auto": "auto", "always-proceed": "always-proceed"}
            if values.get("approval") in legacy:
                values["artifact_review"] = legacy[values["approval"]]
        return compatible_modes(values, capability)

    def execution_settings(self, project, harness):
        capability = self.harness_modes().get(harness, {})
        conversation_id = project.get("_conversation_id")
        if conversation_id:
            conversation = self.conversation(project["id"], conversation_id)
            if harness in conversation["settings"]:
                return self._compatible_execution_settings(harness, conversation["settings"][harness], capability)
        row = self._one("SELECT settings FROM harness_preferences WHERE harness=?", (harness,))
        return self._compatible_execution_settings(harness, json.loads(row["settings"]) if row else {}, capability)

    def chat_execution_settings(self, chat):
        project = {"id": chat["project_id"], "_conversation_id": chat["conversation_id"]}
        return self.execution_settings(project, chat["harness"])

    def save_execution_settings(self, project, harness, values):
        if harness not in HARNESS_MODES:
            raise ValueError("Unknown harness settings")
        capability = self.harness_modes().get(harness, {})
        values = validate_modes(values, capability)
        conversation_id = project.get("_conversation_id")
        if conversation_id:
            conversation = self.conversation(project["id"], conversation_id)
            previous = self._compatible_execution_settings(harness, conversation["settings"].get(harness, {}), capability)
            existing = self._one("SELECT id FROM chats WHERE conversation_id=? AND harness=? LIMIT 1",
                                 (conversation_id, harness))
            if harness == "deepseek_harness" and existing and values.get("execution", "harness") != previous.get("execution", "harness"):
                raise ValueError("DeepSeek fixes its agent preset when a chat starts. Create a new chat to change the preset.")
            if harness == "opencode" and existing and self.opencode and self.opencode.protocol == "v1" and values.get("approval", "harness") != previous.get("approval", "harness"):
                raise ValueError("OpenCode V1 fixes its permission rules when a chat starts. Create a new chat to change them.")
            conversation["settings"][harness] = {**previous, **values}
            self._execute("UPDATE conversations SET settings=? WHERE id=?",
                          (json.dumps(conversation["settings"]), conversation_id))
            values = conversation["settings"][harness]
        self._execute("INSERT INTO harness_preferences(harness,settings) VALUES(?,?) ON CONFLICT(harness) DO UPDATE SET settings=excluded.settings",
                      (harness, json.dumps(values)))
        return values

    def projects(self):
        return self._all("SELECT * FROM projects ORDER BY created_at DESC")

    def project(self, project_id):
        row = self._one("SELECT * FROM projects WHERE id=?", (project_id,))
        if not row:
            raise ValueError("Project not found")
        return dict(row)

    def create_project(self, name, path=None):
        name = str(name).strip()
        if not name or len(name) > 100:
            raise ValueError("Project name must be 1–100 characters")
        if path:
            target = Path(path).expanduser().resolve()
        else:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
            target = (self.root / "projects" / slug).resolve()
        projects_root = self.root / "projects"
        if target == self.root or (self.root in target.parents and target != projects_root and projects_root not in target.parents):
            raise ValueError("Choose a project directory separate from PolyAgent's application files")
        if target.exists() and not target.is_dir():
            raise ValueError("Project path is not a directory")
        if self._one("SELECT id FROM projects WHERE path=?", (str(target),)):
            raise ValueError("This directory already belongs to a PolyAgent project")
        target.mkdir(parents=True, exist_ok=True)
        project_id = str(uuid.uuid4())
        self._execute("INSERT INTO projects(id,name,path,created_at) VALUES(?,?,?,?)",
                      (project_id, name, str(target), time.time()))
        return self.project(project_id)

    def configure_project(self, project_id, values):
        self.project(project_id)
        mode = values.get("mode", "manual")
        if mode not in MODES:
            raise ValueError("Invalid selection mode")
        orch = values.get("orchestrator_model") or None
        chooser = values.get("chooser_model") or None
        subagent = values.get("subagent_model") or None
        for model in (orch, chooser, subagent):
            if model and not self._model_selectable(model):
                raise ValueError(f"Model is unavailable or excluded: {model}")
        self._execute("UPDATE projects SET mode=?,orchestrator_model=?,chooser_model=?,subagent_model=?,locked_orchestrator=?,locked_subagent=? WHERE id=?",
                      (mode, orch, chooser, subagent, int(bool(values.get("locked_orchestrator"))),
                       int(bool(values.get("locked_subagent"))), project_id))
        return self.project(project_id)

    def connect_codex(self):
        detected = next(item for item in self.harnesses() if item["id"] == "codex")
        candidates = []
        if detected["cli"]:
            candidates.append(("cli", detected["cli"]["path"]))
        app_executable = codex_app_cli(detected["app"])
        if app_executable and all(path != app_executable for _, path in candidates):
            candidates.append(("app", app_executable))
        if not candidates:
            reason = detected["cli_error"] or "No working Codex CLI or app-bundled Codex runtime was found"
            raise CodexError(reason)
        errors = []
        for source, executable in candidates:
            client = CodexClient(self._on_codex_event, executable=executable)
            try:
                client.connect()
                account = client.call("account/read")
                models = client.models()
                break
            except (OSError, CodexError) as exc:
                errors.append(f"{source}: {exc}")
                if client.process and client.process.poll() is None:
                    client.process.terminate()
        else:
            raise CodexError("Codex connection failed: " + "; ".join(errors))
        previous = self.codex
        self.codex = client
        self.codex_source = source
        self.account = account.get("account")
        if previous.process and previous.process.poll() is None:
            previous.process.terminate()
        now = time.time()
        labels = distinct_model_labels(
            {model.get("model") or model.get("id"): model.get("displayName") or model.get("model") or model.get("id")
             for model in models if model.get("model") or model.get("id")},
            lambda model_id: model_id)
        for model in models:
            model_id = model.get("model") or model.get("id")
            if not model_id:
                continue
            self._execute("""INSERT INTO models(id,harness,display_name,metadata,discovered_at)
                      VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,
                      metadata=excluded.metadata,discovered_at=excluded.discovered_at""",
                      (model_id, "codex", labels[model_id], json.dumps(model), now))
        self.catalog = {m.get("model") or m.get("id"): m for m in models}
        self._remove_stale_models("codex", self.catalog)
        self.connected = True
        return self.connection_status()

    def _remove_stale_models(self, harness, current):
        """Keep research and overrides, but stop listing departed catalog entries."""
        for row in self._all("SELECT id FROM models WHERE harness=?", (harness,)):
            if row["id"] not in current:
                self._execute("DELETE FROM models WHERE id=?", (row["id"],))

    def connect_antigravity(self):
        detected = next(item for item in self.harnesses() if item["id"] == "antigravity")
        errors = []
        catalog = None
        source = None
        if detected["cli"]:
            try:
                cli = AntigravityCliClient(detected["cli"]["path"], self._on_antigravity_cli_event)
                catalog = cli.connect()
                self.antigravity_cli = cli
                source = "cli"
            except AntigravityCliError as exc:
                errors.append(f"CLI: {exc}")
        if not catalog and detected["app"]:
            try:
                client = AntigravityClient(detected["app"]["path"]).connect()
                catalog = client.models()
                self.antigravity = client
                source = "app"
            except AntigravityError as exc:
                errors.append(f"app: {exc}")
        if not catalog:
            raise AntigravityError("Antigravity connection failed: " + "; ".join(errors or ["No usable CLI or app found"]))
        if not catalog:
            raise AntigravityError("Antigravity returned no usable agent models")
        now = time.time()
        labels = distinct_model_labels(
            {native_id: info["displayName"] for native_id, info in catalog.items()},
            lambda native_id: native_id.replace("-", " ").title())
        for native_id, info in catalog.items():
            model_id = "antigravity:" + native_id
            self._execute("""INSERT INTO models(id,harness,display_name,metadata,discovered_at)
                      VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,
                      metadata=excluded.metadata,discovered_at=excluded.discovered_at""",
                      (model_id, "antigravity", labels[native_id],
                       json.dumps({"native_id": native_id, "model": info["model"],
                                   "provider": info.get("modelProvider"),
                                   "remaining_fraction": info.get("quotaInfo", {}).get("remainingFraction")}), now))
        self.antigravity_catalog = catalog
        self._remove_stale_models("antigravity", {"antigravity:" + key for key in catalog})
        self.antigravity_source = source
        return self.antigravity_status()

    def antigravity_status(self):
        connected = self.antigravity_source is not None
        return {"harness": "antigravity", "running": connected,
                "authenticated": connected,
                "model_count": len(self.antigravity_catalog),
                "source": self.antigravity_source,
                "note": "Connected through the Antigravity CLI." if self.antigravity_source == "cli" else
                "Connected through the running Antigravity desktop app." if connected else
                "Antigravity is not connected."}

    def connect_opencode(self):
        detected = next(item for item in self.harnesses() if item["id"] == "opencode")
        spec = next(item for item in HARNESSES if item["id"] == "opencode")
        candidates = []
        if detected["cli"]:
            candidates.append(("cli", detected["cli"]["path"]))
        app_binary = bundled_cli(detected["app"], spec)
        if app_binary and all(path != app_binary for _, path in candidates):
            candidates.append(("app", app_binary))
        if not candidates:
            raise OpenCodeError(detected["cli_error"] or "No working OpenCode CLI or app-bundled CLI found")
        errors = []
        for source, path in candidates:
            client = OpenCodeClient(path)
            try:
                catalog = client.connect()
                if not catalog:
                    raise OpenCodeError("No models are available from connected OpenCode providers")
                break
            except (OSError, OpenCodeError) as exc:
                errors.append(f"{source}: {exc}")
                client.close()
        else:
            raise OpenCodeError("OpenCode connection failed: " + "; ".join(errors))
        previous = self.opencode
        self.opencode = client
        self.opencode_catalog = catalog
        self.opencode_source = source
        if previous:
            previous.close()
        now = time.time()
        labels = distinct_model_labels(
            {native_id: info.get("name") or info.get("displayName") or native_id
             for native_id, info in catalog.items()},
            lambda native_id: native_id)
        for native_id, info in catalog.items():
            self._execute("""INSERT INTO models(id,harness,display_name,metadata,discovered_at)
                      VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,
                      metadata=excluded.metadata,discovered_at=excluded.discovered_at""",
                      ("opencode:" + native_id, "opencode", labels[native_id],
                       json.dumps({"native_id": native_id, "description": info.get("description", "")}), now))
        self._remove_stale_models("opencode", {"opencode:" + key for key in catalog})
        return self.opencode_status()

    def opencode_status(self):
        running = bool(self.opencode and self.opencode.process and self.opencode.process.poll() is None)
        return {"harness": "opencode", "running": running, "authenticated": running,
                "model_count": len(self.opencode_catalog) if running else 0,
                "source": self.opencode_source if running else None,
                "note": "OpenCode server connected." if running else "OpenCode is not connected."}

    def connect_deepseek(self):
        detected = next(item for item in self.harnesses() if item["id"] == "deepseek_harness")
        spec = next(item for item in HARNESSES if item["id"] == "deepseek_harness")
        candidates = []
        if detected["cli"]:
            candidates.append(("cli", detected["cli"]["path"]))
        app_binary = bundled_cli(detected["app"], spec)
        if app_binary and all(path != app_binary for _, path in candidates):
            candidates.append(("app", app_binary))
        errors = []
        for source, path in candidates:
            client = DeepSeekWebClient(path, self._on_deepseek_update, self._on_deepseek_permission, source=source)
            try:
                client.connect()
                catalog = client.discover_models(str(self.root))
                if not catalog:
                    raise DeepSeekError("DeepSeek Harness returned no models")
                break
            except (DeepSeekError, OSError) as exc:
                errors.append(f"{source}: {exc}")
                client.close()
        else:
            raise DeepSeekError("DeepSeek Harness connection failed: " + "; ".join(errors or ["No CLI or app runtime found"]))
        previous = self.deepseek
        self.deepseek, self.deepseek_catalog, self.deepseek_source = client, catalog, source
        if previous:
            previous.close()
        labels = distinct_model_labels({key: info["name"] for key, info in catalog.items()}, lambda key: key)
        now = time.time()
        for native_id, info in catalog.items():
            self._execute("""INSERT INTO models(id,harness,display_name,metadata,discovered_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,
                         metadata=excluded.metadata,discovered_at=excluded.discovered_at""",
                          ("deepseek_harness:" + native_id, "deepseek_harness", labels[native_id],
                           json.dumps(info), now))
        self._remove_stale_models("deepseek_harness", {"deepseek_harness:" + key for key in catalog})
        return self.deepseek_status()

    def deepseek_status(self):
        running = bool(self.deepseek and self.deepseek.process and self.deepseek.process.poll() is None
                       and self.deepseek.socket and self.deepseek.socket.connected and not self.deepseek.failure)
        return {"harness": "deepseek_harness", "running": running, "authenticated": running,
                "model_count": len(self.deepseek_catalog) if running else 0,
                "source": self.deepseek_source if running else None,
                "note": "DeepSeek Harness native Host connected." if running else "DeepSeek Harness is not connected."}

    def connect_grok(self):
        detected = next(item for item in self.harnesses() if item["id"] == "grok_build")
        if not detected["cli"]:
            raise GrokError(detected["cli_error"] or "Grok Build CLI not found")
        client = GrokClient(detected["cli"]["path"], self._on_deepseek_permission)
        catalog = client.connect()
        self.grok, self.grok_catalog, self.grok_source = client, catalog, "cli"
        labels = distinct_model_labels({key: info["name"] for key, info in catalog.items()}, lambda key: key)
        now = time.time()
        for native_id, info in catalog.items():
            self._execute("""INSERT INTO models(id,harness,display_name,metadata,discovered_at)
                         VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET display_name=excluded.display_name,
                         metadata=excluded.metadata,discovered_at=excluded.discovered_at""",
                          ("grok_build:" + native_id, "grok_build", labels[native_id], json.dumps(info), now))
        self._remove_stale_models("grok_build", {"grok_build:" + key for key in catalog})
        return self.grok_status()

    def grok_status(self):
        return {"harness": "grok_build", "running": self.grok is not None,
                "authenticated": self.grok is not None, "model_count": len(self.grok_catalog),
                "source": self.grok_source, "note": "Grok Build CLI connected." if self.grok else "Grok Build is not connected."}

    def connections(self):
        return {"codex": self.connection_status(), "antigravity": self.antigravity_status(),
                "opencode": self.opencode_status(), "deepseek_harness": self.deepseek_status(),
                "grok_build": self.grok_status()}

    def connection_status(self):
        running = bool(self.codex.process and self.codex.process.poll() is None)
        return {"harness": "codex", "running": running,
                "authenticated": bool(self.account), "model_count": len(self.catalog),
                "source": self.codex_source if running else None,
                "note": "Model catalog entries are verified for access when a turn succeeds." if running else "Codex is not connected."}

    def harnesses(self):
        return discover()

    def models(self):
        rows = self._all("SELECT * FROM models ORDER BY display_name")
        for row in rows:
            row["available"] = self._model_selectable(row["id"])
            row["metadata"] = json.loads(row["metadata"])
            research = self._one("SELECT * FROM research WHERE model_id=?", (row["id"],))
            override = self._one("SELECT * FROM overrides WHERE model_id=?", (row["id"],))
            row["research"] = dict(research) if research else None
            if row["research"]:
                for field in ("strengths", "weaknesses", "evidence"):
                    row["research"][field] = json.loads(row["research"][field])
            row["override"] = dict(override) if override else None
        return rows

    def _model_selectable(self, model_id):
        row = self._one("""SELECT m.enabled,m.harness,o.preference FROM models m LEFT JOIN overrides o ON m.id=o.model_id
                           WHERE m.id=?""", (model_id,))
        if not row or not row["enabled"] or row["preference"] == "exclude":
            return False
        return (row["harness"] == "codex" and self.connected and model_id in self.catalog or
                row["harness"] == "antigravity" and self.antigravity_source is not None and
                model_id.removeprefix("antigravity:") in self.antigravity_catalog or
                row["harness"] == "opencode" and self.opencode_status()["running"] and
                model_id.removeprefix("opencode:") in self.opencode_catalog or
                row["harness"] == "deepseek_harness" and self.deepseek_status()["running"] and
                model_id.removeprefix("deepseek_harness:") in self.deepseek_catalog or
                row["harness"] == "grok_build" and self.grok is not None and
                model_id.removeprefix("grok_build:") in self.grok_catalog)

    def set_override(self, model_id, values):
        if not self._one("SELECT id FROM models WHERE id=?", (model_id,)):
            raise ValueError("Unknown model")
        preference = values.get("preference", "neutral")
        if preference not in PREFERENCES:
            raise ValueError("Invalid preference")
        policy = values.get("weakness_policy", "normal")
        if policy not in {"normal", "downweight", "ignore"}:
            raise ValueError("Invalid weakness policy")
        self._execute("""INSERT INTO overrides(model_id,strengths,weaknesses,assessment,weakness_policy,preference)
                      VALUES(?,?,?,?,?,?) ON CONFLICT(model_id) DO UPDATE SET
                      strengths=excluded.strengths,weaknesses=excluded.weaknesses,
                      assessment=excluded.assessment,weakness_policy=excluded.weakness_policy,
                      preference=excluded.preference""",
                      (model_id, str(values.get("strengths", "")), str(values.get("weaknesses", "")),
                       str(values.get("assessment", "")), policy, preference))

    def set_model_enabled(self, model_id, enabled):
        if not self._one("SELECT id FROM models WHERE id=?", (model_id,)):
            raise ValueError("Unknown model")
        self._execute("UPDATE models SET enabled=? WHERE id=?", (int(bool(enabled)), model_id))

    def chats(self, project_id):
        self.project(project_id)
        return self._all("SELECT * FROM chats WHERE project_id=? ORDER BY created_at", (project_id,))

    def interrupt_chat(self, project_id, chat_id):
        """Ask the connected harness to stop the current turn of one chat."""
        chat = self._one("SELECT * FROM chats WHERE id=? AND project_id=?", (chat_id, project_id))
        if not chat:
            raise ValueError("Chat not found in this project")
        if chat["status"] != "running":
            return {"interrupted": False, "status": chat["status"]}
        harness, thread_id = chat["harness"], chat["thread_id"]
        if harness == "codex":
            turn_id = self.active_turns.get(chat_id)
            if not turn_id:
                raise ValueError("Codex has not reported an active turn yet")
            self.codex.interrupt_turn(thread_id, turn_id)
        elif harness == "antigravity":
            if chat["source"] == "cli":
                if not self.antigravity_cli:
                    raise AntigravityCliError("Antigravity CLI is disconnected")
                self.antigravity_cli.interrupt(thread_id)
            else:
                if not self.antigravity:
                    raise AntigravityError("Antigravity app is disconnected")
                self.antigravity.interrupt(thread_id)
        elif harness == "opencode":
            if not self.opencode:
                raise OpenCodeError("OpenCode is disconnected")
            self.opencode.interrupt(thread_id, self.project(project_id)["path"])
        elif harness == "deepseek_harness":
            if not self.deepseek:
                raise DeepSeekError("DeepSeek Harness is disconnected")
            self.deepseek.interrupt(thread_id)
        elif harness == "grok_build":
            if not self.grok:
                raise GrokError("Grok Build is disconnected")
            self.grok.interrupt(thread_id)
        else:
            raise ValueError("This harness has no interrupt action")
        changed = self._execute("UPDATE chats SET status='interrupted' WHERE id=? AND status='running'", (chat_id,)).rowcount
        if changed:
            self.active_turns.pop(chat_id, None)
            for choice_id, pending in list(self.pending_model_choices.items()):
                if pending["chat_id"] == chat_id:
                    pending["result"].put(None)
                    self.pending_model_choices.pop(choice_id, None)
                    self.event(project_id, chat_id, "request_resolved", "interrupted", {"request_id": choice_id})
            self.event(project_id, chat_id, "turn", "interrupted",
                       {"model": chat["model"], "harness": harness, "reason": "Stopped by user"})
        return {"interrupted": bool(changed), "chat_id": chat_id}

    def interrupt_all(self, project_id, conversation_id):
        self.conversation(project_id, conversation_id)
        running = self._all("SELECT id FROM chats WHERE project_id=? AND conversation_id=? AND status='running' ORDER BY CASE role WHEN 'subagent' THEN 0 ELSE 1 END, created_at DESC",
                            (project_id, conversation_id))
        results, errors = [], []
        for chat in running:
            try:
                results.append(self.interrupt_chat(project_id, chat["id"]))
            except Exception as exc:
                errors.append({"chat_id": chat["id"], "error": str(exc)})
        return {"results": results, "errors": errors}

    def _finish_chat(self, chat, status, *, answer=None, error=None):
        """Ignore late harness output after a user interrupt."""
        with self.lock:
            changed = self._execute("UPDATE chats SET status=? WHERE id=? AND status='running'",
                                    (status, chat["id"])).rowcount
            if not changed:
                return
            self.active_turns.pop(chat["id"], None)
            metadata = {"model": chat["model"], "harness": chat["harness"]}
            if answer is not None:
                self.event(chat["project_id"], chat["id"], "assistant", answer, metadata)
            if error is not None:
                self.event(chat["project_id"], chat["id"], "error", str(error), metadata)
            else:
                self.event(chat["project_id"], chat["id"], "turn", status, metadata)

    def events(self, project_id, since=0):
        self.project(project_id)
        rows = self._all("SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT 500", (project_id, since))
        for row in rows:
            row["metadata"] = json.loads(row["metadata"])
        return rows

    def _require_codex(self):
        if not self.connected or not self.codex.process or self.codex.process.poll() is not None:
            raise ValueError("Connect Codex before starting a chat")
        if not self.account:
            raise ValueError("Codex is running but not signed in. Sign in with the Codex CLI first.")

    def _new_chat(self, project, role, model, *, ephemeral=False, readonly=False):
        if not self._model_selectable(model):
            raise ValueError("Selected model is unavailable or excluded")
        harness = self._one("SELECT harness FROM models WHERE id=?", (model,))["harness"]
        settings = self.execution_settings(project, harness)
        if project.get("_conversation_id"):
            settings = self.save_execution_settings(project, harness, settings)
        if harness == "codex":
            self._require_codex()
            thread_id = self.codex.start_thread(project["path"], model, ephemeral=ephemeral, readonly=readonly, settings=settings)
            source = self.codex_source
        elif harness == "antigravity":
            if role == "orchestrator" and self.hook_base_url:
                AntigravityClient.install_subagent_hook(
                    project, Path(__file__).with_name("hook.py"),
                    self.hook_base_url + "/api/hooks/antigravity")
            source = self.antigravity_source
            info = self.antigravity_catalog[model.split(":", 1)[1]]
            if source == "cli":
                thread_id = self.antigravity_cli.start_chat(project, info, settings=settings)
            else:
                thread_id = self.antigravity.start_chat(project, info)
        elif harness == "opencode":
            if not self.opencode_status()["running"]:
                raise OpenCodeError("Connect OpenCode before starting a chat")
            if role == "orchestrator" and self.opencode.protocol == "v2" and self.hook_base_url:
                self._install_opencode_plugin(project)
            thread_id = self.opencode.start_chat(project["path"], model.removeprefix("opencode:"), settings=settings)
            source = self.opencode_source
        elif harness == "deepseek_harness":
            if not self.deepseek_status()["running"]:
                raise DeepSeekError("Connect DeepSeek Harness before starting a chat")
            native_id = model.removeprefix("deepseek_harness:")
            thread_id = self.deepseek.start_chat(project["path"], self.deepseek_catalog[native_id]["value"], settings=settings)
            source = self.deepseek_source
        elif harness == "grok_build":
            if not self.grok:
                raise GrokError("Connect Grok Build before starting a chat")
            if role == "orchestrator" and self.hook_base_url:
                self._install_grok_hook(project)
            thread_id = self.grok.start_chat()
            source = self.grok_source
        else:
            raise ValueError("No working chat integration for this harness")
        chat_id = str(uuid.uuid4())
        if not ephemeral:
            self._execute("INSERT INTO chats(id,project_id,role,harness,model,thread_id,status,source,created_at,conversation_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                          (chat_id, project["id"], role, harness, model, thread_id, "idle", source, time.time(), project.get("_conversation_id")))
        return chat_id, thread_id

    def _install_opencode_plugin(self, project):
        path = Path(project["path"]) / ".opencode" / "plugins" / "polyagent-bridge" / "index.js"
        source = Path(__file__).with_name("opencode_plugin.js").read_text()
        source = source.replace("__POLYAGENT_ENDPOINT__",
                                json.dumps(self.hook_base_url + "/api/hooks/opencode"))
        if path.exists() and "polyagent.native-subagent" not in path.read_text():
            raise OpenCodeError("An unrelated OpenCode plugin already occupies PolyAgent's plugin path")
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text() != source:
            path.write_text(source)

    def _install_grok_hook(self, project):
        root = Path(project["path"]) / ".grok"
        path = root / "hooks" / "polyagent.json"
        if root.is_symlink() or path.parent.is_symlink() or path.is_symlink():
            raise GrokError("Grok hook path must not be a symbolic link")
        if path.exists():
            existing = json.loads(path.read_text())
            if "/api/hooks/grok" not in json.dumps(existing):
                raise GrokError("An unrelated Grok hook occupies PolyAgent's hook path")
        config = {"hooks": {"PreToolUse": [{
            "matcher": "^(task|Task|spawn_subagent)$",
            "hooks": [{"type": "http", "url": self.hook_base_url + "/api/hooks/grok",
                       "timeout": 1800}]}]}}
        path.parent.mkdir(parents=True, exist_ok=True)
        source = json.dumps(config, indent=2)
        if not path.exists() or path.read_text() != source:
            path.write_text(source)

    def _run_ai_json(self, project, model, prompt, schema, timeout=180):
        model_row = self._one("SELECT harness FROM models WHERE id=?", (model,))
        if model_row and model_row["harness"] == "grok_build":
            if not self._model_selectable(model):
                raise GrokError("Auto Chooser model is unavailable")
            raw = self.grok.run(self.grok.start_chat(), project["path"],
                                prompt + "\nReturn only a JSON object matching this schema: " + json.dumps(schema),
                                model.removeprefix("grok_build:"), first=True, timeout=timeout)
            try:
                return json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
            except json.JSONDecodeError as exc:
                raise GrokError("Auto Chooser did not return valid JSON") from exc
        if model_row and model_row["harness"] == "deepseek_harness":
            if not self._model_selectable(model):
                raise DeepSeekError("Auto Chooser model is unavailable")
            native_id = model.removeprefix("deepseek_harness:")
            session_id = self.deepseek.start_chat(project["path"], self.deepseek_catalog[native_id]["value"])
            try:
                self.acp_turn_text[session_id] = []
                self.deepseek.prompt(session_id, prompt + "\nReturn only a JSON object matching this schema: " + json.dumps(schema), timeout)
                raw = "".join(self.acp_turn_text.pop(session_id, [])).strip()
            finally:
                self.acp_turn_text.pop(session_id, None)
                self.deepseek.call("session/close", {"sessionId": session_id}, timeout=45)
                self.deepseek.active_sessions.discard(session_id)
            try:
                return json.loads(raw.removeprefix("```json").removesuffix("```").strip())
            except json.JSONDecodeError as exc:
                raise DeepSeekError("Auto Chooser did not return valid JSON") from exc
        if model_row and model_row["harness"] == "opencode":
            if not self._model_selectable(model):
                raise OpenCodeError("Auto Chooser model is unavailable")
            session_id = self.opencode.start_chat(project["path"], model.removeprefix("opencode:"))
            response = self._opencode_run(session_id, project["path"],
                                           prompt + "\nReturn only a JSON object matching this schema: " + json.dumps(schema),
                                           model.removeprefix("opencode:"), timeout=timeout)
            try:
                return json.loads(response.strip().removeprefix("```json").removesuffix("```").strip())
            except json.JSONDecodeError as exc:
                raise OpenCodeError("Auto Chooser did not return valid JSON") from exc
        if model_row and model_row["harness"] == "antigravity":
            if not self._model_selectable(model):
                raise AntigravityError("Auto Chooser model is unavailable")
            info = self.antigravity_catalog[model.split(":", 1)[1]]
            full_prompt = prompt + "\nReturn only a JSON object matching this schema: " + json.dumps(schema)
            if self.antigravity_source == "cli":
                thread_id = self.antigravity_cli.start_chat(project, info)
                waiter = Queue(maxsize=1)
                self.cli_waiters[thread_id] = waiter
                try:
                    self.antigravity_cli.send(thread_id, full_prompt, project, info)
                    result = waiter.get(timeout=timeout)
                except Empty as exc:
                    raise AntigravityCliError("Auto Chooser timed out") from exc
                finally:
                    self.cli_waiters.pop(thread_id, None)
                if result.get("status") != "SUCCESS":
                    raise AntigravityCliError(result.get("error") or "Auto Chooser failed")
                raw = result.get("response", "")
            else:
                thread_id = self.antigravity.start_chat(project, info)
                self.antigravity.send(thread_id, full_prompt, info)
                self.antigravity.wait_idle(thread_id, timeout)
                raw = self._antigravity_result(self.antigravity.trajectory(thread_id))
            try:
                return json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
            except json.JSONDecodeError as exc:
                raise AntigravityError("Auto Chooser did not return valid JSON") from exc
        self._require_codex()
        thread_id = self.codex.start_thread(project["path"], model, ephemeral=True, readonly=True)
        turn_id = self.codex.start_turn(thread_id, prompt, output_schema=schema)
        waiter = Queue(maxsize=1)
        self.waiters[(thread_id, turn_id)] = waiter
        try:
            result = self.completed_turns.pop((thread_id, turn_id), None)
            if result is None:
                result = waiter.get(timeout=timeout)
        except Empty as exc:
            try:
                self.codex.call("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=10)
            except CodexError:
                pass
            raise CodexError("AI evaluation timed out") from exc
        finally:
            self.waiters.pop((thread_id, turn_id), None)
        if result != "completed":
            raise CodexError(f"AI evaluation ended with status {result}")
        raw = self.turn_messages.pop((thread_id, turn_id), "")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CodexError("AI evaluation did not return valid JSON") from exc

    @staticmethod
    def _antigravity_result(trajectory):
        steps = trajectory.get("trajectory", {}).get("steps", [])
        latest_input = max((index for index, step in enumerate(steps)
                            if step.get("type") == "CORTEX_STEP_TYPE_USER_INPUT"), default=-1)
        steps = steps[latest_input + 1:]
        errors = [step.get("errorMessage", {}).get("error", {}).get("shortError") or
                  step.get("errorMessage", {}).get("error", {}).get("userErrorMessage")
                  for step in steps if step.get("type") == "CORTEX_STEP_TYPE_ERROR_MESSAGE"]
        if errors:
            raise AntigravityError(errors[-1] or "Antigravity agent execution failed")
        responses = [step.get("plannerResponse", {}).get("modifiedResponse") or
                     step.get("plannerResponse", {}).get("response")
                     for step in steps if step.get("type") == "CORTEX_STEP_TYPE_PLANNER_RESPONSE"]
        responses = [item for item in responses if item]
        if not responses:
            raise AntigravityError("Antigravity completed without a response")
        return responses[-1]

    def _watch_antigravity(self, project_id, chat_id, thread_id, model, start_index=0):
        seen_steps = set()
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                if self._one("SELECT status FROM chats WHERE id=?", (chat_id,))["status"] != "running":
                    return
                trajectory = self.antigravity.trajectory(thread_id)
                steps = trajectory.get("trajectory", {}).get("steps", [])
                for index, step in enumerate(steps):
                    if index < start_index:
                        continue
                    step_type = step.get("type", "")
                    step_status = step.get("status", "")
                    signature = (index, step_type, step_status)
                    if signature in seen_steps:
                        continue
                    seen_steps.add(signature)
                    requested = step.get("requestedInteraction") or {}
                    supported = {"permission", "runCommand", "mcp", "filePermission", "approvalInteraction",
                                 "openBrowserUrl", "executeBrowserJavascript", "readUrlContent",
                                 "captureBrowserScreenshot", "clickBrowserPixel", "runExtensionCode"}
                    pending_kind = next((key for key in requested if key in supported), None)
                    if pending_kind and step_status == "CORTEX_STEP_STATUS_WAITING":
                        request_id = "agy-" + uuid.uuid4().hex
                        trajectory_id = trajectory.get("trajectory", {}).get("trajectoryId") or trajectory.get("trajectory", {}).get("id")
                        if trajectory_id:
                            value = requested[pending_kind]
                            self.antigravity_permissions[request_id] = (thread_id, trajectory_id, index, pending_kind, value, project_id, chat_id)
                            self.event(project_id, chat_id, "approval", value.get("actionDescription") or pending_kind,
                                       {"request_id": request_id, "params": value, "harness": "antigravity"})
                    if "SUBAGENT" in step_type:
                        if not self.hook_base_url:
                            self.event(project_id, chat_id, "native_subagent", "",
                                       {"harness": "antigravity", "status": step_status,
                                        "phase": "trajectory", "redirected": False,
                                        "prompt_available": False, "model": model})
                    elif step_type not in {"CORTEX_STEP_TYPE_USER_INPUT", "CORTEX_STEP_TYPE_PLANNER_RESPONSE",
                                            "CORTEX_STEP_TYPE_ERROR_MESSAGE"}:
                        details = {}
                        command = step.get("runCommand") or {}
                        if command.get("commandLine"):
                            details["command"] = command["commandLine"]
                            if "exitCode" in command:
                                details["exit_code"] = command["exitCode"]
                        action = (step.get("codeAction") or {}).get("actionSpec") or {}
                        for operation, value in action.items():
                            if isinstance(value, dict):
                                path = value.get("path") or {}
                                if isinstance(path, dict) and path.get("absoluteUri"):
                                    details.update({"file": path["absoluteUri"], "operation": operation})
                                    break
                        self.event(project_id, chat_id, "tool", step_type,
                                   {"phase": step_status, "model": model, "harness": "antigravity", "detail": details})
                status = trajectory.get("status", "")
                finished_step = any(step.get("type") in {"CORTEX_STEP_TYPE_PLANNER_RESPONSE", "CORTEX_STEP_TYPE_ERROR_MESSAGE"}
                                    and step.get("status") == "CORTEX_STEP_STATUS_DONE" for step in steps[start_index:])
                if status in {"CASCADE_RUN_STATUS_IDLE", "CASCADE_RUN_STATUS_FAILED", "CASCADE_RUN_STATUS_CANCELLED"} and finished_step:
                    response = self._antigravity_result(trajectory)
                    self._finish_chat(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)),
                                      "completed", answer=response)
                    return
                time.sleep(0.8)
            raise AntigravityError("Antigravity turn did not finish within 10 minutes")
        except Exception as exc:
            self._finish_chat(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)),
                              "failed", error=exc)

    def _on_antigravity_cli_event(self, thread_id, event):
        kind = event.get("event")
        if kind == "result" and thread_id in self.cli_waiters:
            self.cli_waiters[thread_id].put(event.get("result") or {})
            return
        chat = self._one("SELECT * FROM chats WHERE thread_id=?", (thread_id,))
        if not chat:
            return
        project_id, chat_id, model = chat["project_id"], chat["id"], chat["model"]
        if kind == "step_update":
            step = event.get("step_update") or {}
            if step.get("text_delta"):
                self.event(project_id, chat_id, "delta", step["text_delta"],
                           {"model": model, "harness": "antigravity"})
            if step.get("tool_name"):
                self.event(project_id, chat_id, "tool", step["tool_name"],
                           {"phase": step.get("state"), "model": model, "harness": "antigravity"})
        elif kind == "result":
            result = event.get("result") or {}
            if result.get("status") == "SUCCESS":
                self._finish_chat(chat, "completed", answer=result.get("response", ""))
            else:
                self._finish_chat(chat, "failed", error=result.get("error") or
                                  f"Antigravity CLI ended with {result.get('status', 'unknown status')}")
        elif kind == "process_exit" and chat["status"] == "running":
            self._finish_chat(chat, "failed", error="Antigravity CLI session exited before completing the turn")

    def _send_antigravity_turn(self, chat, project, message, model):
        source = chat["source"] or "app"
        native_id = model.split(":", 1)[1]
        if chat["role"] == "orchestrator" and self.hook_base_url:
            AntigravityClient.install_subagent_hook(
                project, Path(__file__).with_name("hook.py"),
                self.hook_base_url + "/api/hooks/antigravity")
        if source == "cli":
            if not self.antigravity_cli:
                detected = next(item for item in self.harnesses() if item["id"] == "antigravity")
                if not detected["cli"]:
                    raise AntigravityCliError("The Antigravity CLI for this chat is unavailable")
                self.antigravity_cli = AntigravityCliClient(detected["cli"]["path"], self._on_antigravity_cli_event)
            self.antigravity_cli.send(chat["thread_id"], message, project, {"model": native_id},
                                      settings=self.chat_execution_settings(chat))
        elif source == "app":
            if not self.antigravity:
                detected = next(item for item in self.harnesses() if item["id"] == "antigravity")
                if not detected["app"]:
                    raise AntigravityError("The Antigravity app for this chat is unavailable")
                self.antigravity = AntigravityClient(detected["app"]["path"]).connect()
            info = self.antigravity.models().get(native_id)
            if not info:
                raise AntigravityError("This app chat's model is no longer available")
            start_index = len(self.antigravity.trajectory(chat["thread_id"]).get("trajectory", {}).get("steps", []))
            self.antigravity.send(chat["thread_id"], message, info, settings=self.chat_execution_settings(chat))
            threading.Thread(target=self._watch_antigravity,
                             args=(project["id"], chat["id"], chat["thread_id"], model, start_index), daemon=True).start()
        else:
            raise ValueError("Unknown Antigravity chat source")
        return str(uuid.uuid4())

    @staticmethod
    def _opencode_message_parts(message, protocol):
        if protocol == "v2":
            return message.get("content", []) if message.get("type") == "assistant" else []
        return message.get("parts", []) if message.get("info", {}).get("role") == "assistant" else []

    def _opencode_run(self, session_id, directory, message, model, *, timeout=600,
                      project_id=None, chat_id=None):
        client = self.opencode
        if not client or not self.opencode_status()["running"]:
            raise OpenCodeError("OpenCode is disconnected")
        before = {item.get("id") or item.get("info", {}).get("id")
                  for item in client.messages(session_id, directory)}
        output = Queue(maxsize=1)

        def submit():
            try:
                if message.startswith("/") and not message.startswith("//"):
                    command, _, arguments = message[1:].partition(" ")
                    result = client.slash_command(session_id, command, arguments, model, directory)
                else:
                    result = client.send(session_id, message, model, directory)
                output.put((True, result))
            except Exception as exc:
                output.put((False, exc))

        threading.Thread(target=submit, daemon=True).start()
        seen_tools = set()
        seen_permissions = set()
        deadline = time.monotonic() + timeout
        result = None
        while time.monotonic() < deadline:
            if chat_id and self._one("SELECT status FROM chats WHERE id=?", (chat_id,))["status"] == "interrupted":
                raise OpenCodeError("Turn interrupted")
            if chat_id:
                for permission in client.permissions(session_id, directory):
                    request_id = permission["id"]
                    if request_id not in seen_permissions:
                        seen_permissions.add(request_id)
                        key = "oc-" + request_id
                        self.opencode_permissions[key] = (client, session_id, request_id, directory, project_id, chat_id)
                        self.event(project_id, chat_id, "approval", permission.get("action") or permission.get("permission") or "Tool permission",
                                   {"request_id": key, "params": permission, "harness": "opencode"})
            try:
                ok, value = output.get_nowait()
                if not ok:
                    raise value
                result = value
            except Empty:
                pass
            messages = client.messages(session_id, directory)
            latest = ""
            idle = False
            error = None
            for item in messages:
                item_id = item.get("id") or item.get("info", {}).get("id")
                if item_id in before:
                    continue
                if client.protocol == "v2" and item.get("type") == "idle":
                    idle = True
                if client.protocol == "v2" and item.get("type") == "assistant" and item.get("error"):
                    error = str(item["error"])
                for part in self._opencode_message_parts(item, client.protocol):
                    if part.get("type") == "text" and part.get("text"):
                        latest = part["text"]
                    if part.get("type") == "tool":
                        name = part.get("name") or part.get("tool", "tool")
                        phase = part.get("state", {}).get("status")
                        key = (item_id, part.get("id") or part.get("callID"), phase)
                        if project_id and key not in seen_tools:
                            seen_tools.add(key)
                            if client.protocol == "v1" and name == "task":
                                native_input = part.get("state", {}).get("input") or {}
                                delegated = native_input.get("prompt", "") if isinstance(native_input, dict) else ""
                                self.event(project_id, chat_id, "native_subagent", delegated,
                                           {"harness": "opencode", "status": phase,
                                            "redirected": False, "prompt_available": bool(delegated)})
                            else:
                                self.event(project_id, chat_id, "tool", name,
                                           {"phase": phase, "model": "opencode:" + model,
                                            "harness": "opencode"})
            if error:
                raise OpenCodeError(error)
            if client.protocol == "v1" and result is not None:
                for part in result.get("parts", []):
                    if part.get("type") == "text" and part.get("text"):
                        latest = part["text"]
                if not latest:
                    raise OpenCodeError("OpenCode completed without a text response")
                return latest
            if client.protocol == "v2" and idle and result is not None:
                if not latest:
                    raise OpenCodeError("OpenCode completed without a text response")
                return latest
            time.sleep(0.7)
        raise OpenCodeError("OpenCode turn timed out")

    def _send_opencode_turn(self, chat, project, message, model):
        turn_id = str(uuid.uuid4())

        def work():
            try:
                self.opencode.apply_settings(chat["thread_id"], project["path"], self.chat_execution_settings(chat))
                answer = self._opencode_run(chat["thread_id"], project["path"], message,
                                            model.removeprefix("opencode:"),
                                            project_id=project["id"], chat_id=chat["id"])
                self._finish_chat(chat, "completed", answer=answer)
            except Exception as exc:
                self._finish_chat(chat, "failed", error=exc)

        threading.Thread(target=work, daemon=True).start()
        return turn_id

    def _on_deepseek_update(self, params):
        session_id = params.get("sessionId")
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk" and update.get("content", {}).get("type") == "text":
            text = update["content"].get("text", "")
            self.acp_turn_text.setdefault(session_id, []).append(text)
            chat = self._one("SELECT * FROM chats WHERE thread_id=?", (session_id,))
            if chat and text:
                self.event(chat["project_id"], chat["id"], "delta", text,
                           {"model": chat["model"], "harness": "deepseek_harness"})
        elif kind in {"tool_call", "tool_call_update"}:
            chat = self._one("SELECT * FROM chats WHERE thread_id=?", (session_id,))
            if chat:
                if kind == "tool_call" and update.get("title") == "subagent":
                    raw = update.get("rawInput") or {}
                    prompt = raw.get("prompt", "") if isinstance(raw, dict) else ""
                    self.event(chat["project_id"], chat["id"], "native_subagent", prompt,
                               {"harness": "deepseek_harness", "status": "observed",
                                "redirected": False, "prompt_available": bool(prompt)})
                self.event(chat["project_id"], chat["id"], "tool", update.get("title") or kind,
                           {"phase": update.get("status"), "item": update,
                            "model": chat["model"], "harness": "deepseek_harness"})

    def _on_deepseek_permission(self, client, request):
        params = request.get("params") or {}
        chat = self._one("SELECT * FROM chats WHERE thread_id=?", (params.get("sessionId"),))
        if not chat:
            client.answer(request["id"], {"outcome": {"outcome": "cancelled"}})
            return
        request_id = "dsh-" + uuid.uuid4().hex
        self.acp_permissions[request_id] = (client, request)
        self.event(chat["project_id"], chat["id"], "approval", "Tool permission",
                   {"request_id": request_id, "params": params, "harness": chat["harness"]})

    def _send_deepseek_turn(self, chat, project, message, model):
        turn_id = str(uuid.uuid4())

        def work():
            session_id = chat["thread_id"]
            try:
                self.acp_turn_text[session_id] = []
                self.deepseek.apply_settings(session_id, self.chat_execution_settings(chat))
                self.deepseek.prompt(session_id, message)
                answer = "".join(self.acp_turn_text.pop(session_id, [])).strip()
                if not answer:
                    raise DeepSeekError("DeepSeek Harness completed without a text response")
                self._finish_chat(chat, "completed", answer=answer)
            except Exception as exc:
                self._finish_chat(chat, "failed", error=exc)
            finally:
                self.acp_turn_text.pop(session_id, None)

        threading.Thread(target=work, daemon=True).start()
        return turn_id

    def _send_grok_turn(self, chat, project, message, model):
        turn_id = str(uuid.uuid4())

        def work():
            try:
                first = not self._one("SELECT id FROM events WHERE chat_id=? AND kind='turn' LIMIT 1", (chat["id"],))

                def observe(item):
                    kind = item.get("type")
                    if kind == "text" and item.get("data"):
                        self.event(project["id"], chat["id"], "delta", item["data"],
                                   {"model": model, "harness": "grok_build"})
                    elif kind in {"tool_call", "tool_call_update"}:
                        self.event(project["id"], chat["id"], "tool", item.get("title") or kind,
                                   {"phase": item.get("status"), "item": item,
                                    "model": model, "harness": "grok_build"})

                answer = self.grok.run(chat["thread_id"], project["path"], message,
                                       model.removeprefix("grok_build:"), first=first,
                                       orchestrator=chat["role"] == "orchestrator",
                                       on_event=observe, timeout=1860 if chat["role"] == "orchestrator" else 600,
                                       settings=self.chat_execution_settings(chat))
                if self._one("SELECT status FROM chats WHERE id=?", (chat["id"],))["status"] != "running":
                    return
                self.event(project["id"], chat["id"], "assistant", answer,
                           {"model": model, "harness": "grok_build"})
                if chat["role"] == "orchestrator":
                    while True:
                        if self._one("SELECT status FROM chats WHERE id=?", (chat["id"],))["status"] != "running":
                            return
                        with self.grok_followups_lock:
                            queued = self.grok_followups.pop(chat["id"], [])
                        if not queued:
                            break
                        for delegated_answer in queued:
                            answer = self.grok.run(
                                chat["thread_id"], project["path"],
                                "PolyAgent subagent response to your native task call:\n\n" + delegated_answer,
                                model.removeprefix("grok_build:"), first=False,
                                orchestrator=True, on_event=observe, timeout=1860,
                                settings=self.chat_execution_settings(chat))
                            self.event(project["id"], chat["id"], "assistant", answer,
                                       {"model": model, "harness": "grok_build"})
                self._finish_chat(chat, "completed")
            except Exception as exc:
                self._finish_chat(chat, "failed", error=exc)

        threading.Thread(target=work, daemon=True).start()
        return turn_id

    def _selection_context(self):
        candidates = []
        for model in self.models():
            if model["available"]:
                candidates.append({"id": model["id"], "harness": model["harness"],
                                   "description": model["metadata"].get("description", ""),
                                   "research": model["research"], "user": model["override"]})
        return candidates

    def choose_model(self, project, task, role, exclude=()):
        candidates = [m for m in self._selection_context() if m["id"] not in exclude]
        forced = [m for m in candidates if m["user"] and m["user"]["preference"] == "force"]
        if len(forced) == 1:
            return forced[0]["id"]
        if len(forced) > 1:
            raise ValueError("Multiple models are forced; choose one")
        chooser = project["chooser_model"]
        if not chooser or not self._model_selectable(chooser):
            raise ValueError("Choose an available Auto Chooser model")
        if not candidates:
            raise ValueError("No authorized models are available")
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"model": {"type": "string"}, "reason": {"type": "string"}},
                  "required": ["model", "reason"]}
        prompt = ("Select one available coding model for this delegated role. Reason about the actual task, "
                  "coding performance, reasoning, planning, debugging, tool use, strengths, weaknesses, "
                  "research quality, and the user's preferences. Use cached source-backed research; when it is "
                  "missing or stale, check current primary or independent evidence with your native web tool if available. "
                  "Distinguish tested evidence from marketing and anecdotes. Do not use a fixed numerical scoring formula. "
                  "Do not invent benchmark evidence. Return JSON only.\n"
                  f"Role: {role}\nTask: {task}\nCandidates: {json.dumps(candidates)}")
        choice = self._run_ai_json(project, chooser, prompt, schema)
        selected = choice.get("model")
        if selected not in {m["id"] for m in candidates}:
            raise CodexError("Auto Chooser selected an unavailable model")
        self.event(project["id"], None, "choice", choice.get("reason", ""),
                   {"model": selected, "role": role, "chooser": chooser},
                   conversation_id=project.get("_conversation_id"))
        return selected

    def research_model(self, project_id, model_id):
        project = self.project(project_id)
        if not self._one("SELECT id FROM models WHERE id=?", (model_id,)):
            raise ValueError("Unknown model")
        chooser = project["chooser_model"]
        if not chooser or not self._model_selectable(chooser):
            raise ValueError("Choose an available Auto Chooser model")
        schema = {"type": "object", "additionalProperties": False,
                  "properties": {"strengths": {"type": "array", "items": {"type": "string"}},
                                 "weaknesses": {"type": "array", "items": {"type": "string"}},
                                 "evidence": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                             "properties": {"source": {"type": "string"}, "url": {"type": "string"},
                                                            "finding": {"type": "string"}, "type": {"type": "string"}},
                                             "required": ["source", "url", "finding", "type"]}},
                                 "conclusion": {"type": "string"}},
                  "required": ["strengths", "weaknesses", "evidence", "conclusion"]}
        prompt = (f"Research current coding-agent evidence for the underlying AI model identified by {model_id}. "
                  "The identifier may contain a harness prefix, provider or gateway path, and routing suffixes "
                  "such as :free; use these only to identify the actual model name and version, then research "
                  "that model itself. Evaluate model capabilities and limitations only. Do not research or "
                  "evaluate the endpoint, hosting provider, gateway, subscription or free tier, pricing, rate "
                  "limits, serving latency, routing reliability, or the harness running it. Do not list a lack "
                  "of benchmarks for the exact endpoint, provider, or model-and-harness combination as a weakness "
                  "or qualification. Apply this model-only scope to strengths, weaknesses, evidence, and conclusion. "
                  "Use live web search if available. "
                  "Check at most three relevant primary or independent sources, then finish. Good candidates include "
                  "Terminal-Bench, SWE-bench, LiveCodeBench, Aider Polyglot, METR, Artificial Analysis, HELM, "
                  "Epoch AI, arXiv, and primary GitHub issue reports. Do not search every site. Assess coding, "
                  "reasoning, planning, debugging, agentic completion and tool use only where evidence exists. "
                  "Classify each source as independent, vendor, or anecdote. Give exact source URLs "
                  "and concise findings; omit unsupported scores and conclusions. If browsing is unavailable, "
                  "return empty evidence, strengths and weaknesses, and explain why. Return JSON only.")
        result = self._run_ai_json(project, chooser, prompt, schema, timeout=180)
        for item in result.get("evidence", []):
            if not str(item.get("url", "")).startswith("https://"):
                raise CodexError("Research contained a source without an HTTPS URL")
        if not result["evidence"]:
            result["strengths"] = []
            result["weaknesses"] = []
            result["conclusion"] = "No source-backed research was available. " + result["conclusion"]
        self._execute("""INSERT INTO research(model_id,strengths,weaknesses,evidence,conclusion,updated_at)
                      VALUES(?,?,?,?,?,?) ON CONFLICT(model_id) DO UPDATE SET strengths=excluded.strengths,
                      weaknesses=excluded.weaknesses,evidence=excluded.evidence,
                      conclusion=excluded.conclusion,updated_at=excluded.updated_at""",
                      (model_id, json.dumps(result["strengths"]), json.dumps(result["weaknesses"]),
                       json.dumps(result["evidence"]), result["conclusion"], time.time()))
        return result

    def send(self, project_id, message, conversation_id=None, settings=None, settings_harness=None):
        if conversation_id is None:
            existing = self.conversations(project_id)
            conversation_id = existing[-1]["id"] if existing else self.create_conversation(project_id)["id"]
        self.conversation(project_id, conversation_id)
        with self.lock:
            gate = self.conversation_locks.setdefault(conversation_id, threading.Lock())
        if not gate.acquire(blocking=False):
            raise ValueError("This chat is already starting a turn")
        try:
            if settings_harness and settings:
                # Full Auto may select a different harness than the one whose
                # controls are currently visible. Never send its modes elsewhere.
                scoped_project = {"id": project_id, "_conversation_id": conversation_id}
                effective = self.execution_settings(scoped_project, settings_harness)
                self.save_execution_settings(scoped_project, settings_harness, {**effective, **settings})
                settings = None
            return self._send(project_id, message, conversation_id, settings)
        finally:
            gate.release()

    def _send(self, project_id, message, conversation_id=None, settings=None):
        project = self.project(project_id)
        message = str(message).strip()
        if not message:
            raise ValueError("Message is empty")
        if conversation_id is None:
            existing = self.conversations(project_id)
            conversation_id = existing[-1]["id"] if existing else self.create_conversation(project_id)["id"]
        conversation = self.conversation(project_id, conversation_id)
        project["_conversation_id"] = conversation_id
        row = self._one("SELECT * FROM chats WHERE conversation_id=? AND role='orchestrator' ORDER BY created_at DESC LIMIT 1", (conversation_id,))
        if row and row["status"] == "running":
            raise ValueError("The orchestrator is still working")
        if row:
            chat_id, thread_id, model = row["id"], row["thread_id"], row["model"]
            user_assigned = project["mode"] in {"manual", "ask_every_time"} or (project["mode"] == "hybrid" and project["locked_orchestrator"])
            if user_assigned and project["orchestrator_model"]:
                if not self._model_selectable(project["orchestrator_model"]):
                    raise ValueError("Selected orchestrator model is unavailable or excluded")
                model = project["orchestrator_model"]
            assigned_harness = self._one("SELECT harness FROM models WHERE id=?", (model,))["harness"]
            effective = self.execution_settings(project, assigned_harness)
            self.save_execution_settings(project, assigned_harness, {**effective, **(settings or {})})
            requires_new_chat = (
                row["harness"] == "antigravity" and row["source"] != self.antigravity_source or
                row["harness"] == "opencode" and
                (row["source"] != self.opencode_source or model != row["model"]) or
                row["harness"] == "deepseek_harness" and model != row["model"] or
                row["harness"] == "grok_build" and model != row["model"])
            if assigned_harness != row["harness"] or requires_new_chat or (row["harness"] == "grok_build" and row["status"] == "failed"):
                chat_id, thread_id = self._new_chat(project, "orchestrator", model)
            elif row["harness"] == "codex":
                self.codex.resume_thread(thread_id, project["path"], self.execution_settings(project, "codex"))
            elif row["harness"] == "deepseek_harness":
                self.deepseek.resume_chat(thread_id, project["path"])
            elif row["harness"] not in {"antigravity", "opencode", "grok_build"}:
                raise ValueError("No working chat integration for this harness")
        else:
            if project["mode"] == "full_auto" or project["mode"] == "hybrid" and not project["locked_orchestrator"]:
                model = self.choose_model(project, message, "orchestrator")
            else:
                model = project["orchestrator_model"]
                if not model:
                    raise ValueError("Select an orchestrator model")
            assigned_harness = self._one("SELECT harness FROM models WHERE id=?", (model,))["harness"]
            effective = self.execution_settings(project, assigned_harness)
            self.save_execution_settings(project, assigned_harness, {**effective, **(settings or {})})
            chat_id, thread_id = self._new_chat(project, "orchestrator", model)
        if conversation["title"] == "New chat":
            self._execute("UPDATE conversations SET title=? WHERE id=?", (message[:70], conversation_id))
        self._execute("UPDATE chats SET status='running',model=? WHERE id=?", (model, chat_id))
        harness = self._one("SELECT harness FROM chats WHERE id=?", (chat_id,))["harness"]
        self.event(project_id, chat_id, "user", message, {"model": model, "harness": harness})
        try:
            if harness == "codex":
                turn_id = self.codex.start_turn(thread_id, message, model=model,
                                                settings=self.execution_settings(project, "codex"))
                self.active_turns[chat_id] = turn_id
            elif harness == "antigravity":
                turn_id = self._send_antigravity_turn(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)),
                                                      project, message, model)
            elif harness == "opencode":
                turn_id = self._send_opencode_turn(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)),
                                                   project, message, model)
            elif harness == "deepseek_harness":
                turn_id = self._send_deepseek_turn(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)),
                                                   project, message, model)
            elif harness == "grok_build":
                turn_id = self._send_grok_turn(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)),
                                               project, message, model)
            else:
                raise ValueError("No working chat integration for this harness")
        except Exception as exc:
            self._finish_chat(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)), "failed", error=exc)
            raise
        return {"chat_id": chat_id, "conversation_id": conversation_id, "turn_id": turn_id, "model": model, "harness": harness}

    def create_subagent(self, project_id, prompt, model, *, replacement_for=None, parent_chat_id=None):
        """Create a fresh harness chat with the required first message."""
        project = self.project(project_id)
        parent = self._one("SELECT conversation_id FROM chats WHERE id=? AND project_id=?", (parent_chat_id, project_id))
        project["_conversation_id"] = parent["conversation_id"] if parent else None
        chat_id, thread_id = self._new_chat(project, "subagent", model)
        first = first_subagent_message(prompt)
        self.event(project_id, chat_id, "subagent_started", prompt,
                   {"model": model, "harness": self._one("SELECT harness FROM chats WHERE id=?", (chat_id,))["harness"],
                    "replacement_for": replacement_for})
        self._execute("UPDATE chats SET status='running' WHERE id=?", (chat_id,))
        try:
            chat = self._one("SELECT * FROM chats WHERE id=?", (chat_id,))
            if chat["harness"] == "codex":
                turn_id = self.codex.start_turn(thread_id, first, model=model,
                                                settings=self.execution_settings(project, "codex"))
                self.active_turns[chat_id] = turn_id
            elif chat["harness"] == "opencode":
                turn_id = self._send_opencode_turn(chat, project, first, model)
            elif chat["harness"] == "deepseek_harness":
                turn_id = self._send_deepseek_turn(chat, project, first, model)
            elif chat["harness"] == "grok_build":
                turn_id = self._send_grok_turn(chat, project, first, model)
            else:
                turn_id = self._send_antigravity_turn(chat, project, first, model)
        except Exception as exc:
            self._finish_chat(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)), "failed", error=exc)
            raise
        return {"chat_id": chat_id, "thread_id": thread_id, "turn_id": turn_id}

    def continue_subagent(self, chat_id, message):
        chat = self._one("SELECT * FROM chats WHERE id=? AND role='subagent'", (chat_id,))
        if not chat:
            raise ValueError("Subagent chat not found")
        project = self.project(chat["project_id"])
        project["_conversation_id"] = chat["conversation_id"]
        if chat["harness"] == "codex":
            self.codex.resume_thread(chat["thread_id"], project["path"], self.chat_execution_settings(chat))
        elif chat["harness"] == "deepseek_harness":
            self.deepseek.resume_chat(chat["thread_id"], project["path"])
        self._execute("UPDATE chats SET status='running' WHERE id=?", (chat_id,))
        try:
            if chat["harness"] == "codex":
                turn_id = self.codex.start_turn(chat["thread_id"], message, model=chat["model"],
                                                settings=self.chat_execution_settings(chat))
                self.active_turns[chat_id] = turn_id
            elif chat["harness"] == "opencode":
                turn_id = self._send_opencode_turn(chat, project, message, chat["model"])
            elif chat["harness"] == "deepseek_harness":
                turn_id = self._send_deepseek_turn(chat, project, message, chat["model"])
            elif chat["harness"] == "grok_build":
                turn_id = self._send_grok_turn(chat, project, message, chat["model"])
            else:
                turn_id = self._send_antigravity_turn(chat, project, message, chat["model"])
        except Exception as exc:
            self._finish_chat(self._one("SELECT * FROM chats WHERE id=?", (chat_id,)), "failed", error=exc)
            raise
        return turn_id

    def _wait_subagent_response(self, chat_id, timeout=240):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chat = self._one("SELECT status FROM chats WHERE id=?", (chat_id,))
            if chat and chat["status"] != "running":
                if chat["status"] != "completed":
                    error = self._one("SELECT text FROM events WHERE chat_id=? AND kind='error' ORDER BY id DESC LIMIT 1", (chat_id,))
                    raise ValueError(error["text"] if error else f"Subagent ended: {chat['status']}")
                answer = self._one("SELECT text FROM events WHERE chat_id=? AND kind='assistant' ORDER BY id DESC LIMIT 1", (chat_id,))
                if answer:
                    return answer["text"]
                raise ValueError("Subagent completed without a response")
            time.sleep(0.5)
        raise ValueError("Subagent did not finish within four minutes")

    def answer_model_choice(self, choice_id, model):
        pending = self.pending_model_choices.get(choice_id)
        if not pending:
            raise ValueError("Model choice is no longer pending")
        if model not in pending["options"] or not self._model_selectable(model):
            raise ValueError("Selected model is unavailable")
        pending["result"].put(model)
        self.pending_model_choices.pop(choice_id, None)
        self.event(pending["project_id"], pending["chat_id"], "request_resolved", "selected",
                   {"request_id": choice_id})
        return {"selected": model}

    def _select_subagent_model(self, project, chat_id, prompt, exclude=()):
        parent = self._one("SELECT conversation_id FROM chats WHERE id=?", (chat_id,))
        project["_conversation_id"] = parent["conversation_id"] if parent else None
        mode = project["mode"]
        forced = [model["id"] for model in self.models()
                  if model["available"] and model["override"] and model["override"]["preference"] == "force"]
        if len(forced) > 1:
            raise ValueError("Multiple models are forced; choose one")
        if forced:
            if forced[0] in exclude:
                raise ValueError("The forced model failed; automatic replacement would violate the forced choice")
            if mode == "manual" or mode == "hybrid" and project["locked_subagent"]:
                if project["subagent_model"] != forced[0]:
                    raise ValueError("Forced model conflicts with the assigned subagent model")
            if mode != "ask_every_time":
                return forced[0]
        if mode == "manual" or mode == "hybrid" and project["locked_subagent"]:
            selected = project["subagent_model"]
            if not selected or selected in exclude or not self._model_selectable(selected):
                raise ValueError("Select an available subagent model in project settings")
            return selected
        if mode == "ask_every_time":
            options = [model["id"] for model in self.models() if model["available"] and model["id"] not in exclude]
            if forced:
                options = forced
            if not options:
                raise ValueError("No available models for this subagent")
            choice_id = str(uuid.uuid4())
            pending = {"project_id": project["id"], "chat_id": chat_id,
                       "options": options, "result": Queue(maxsize=1)}
            self.pending_model_choices[choice_id] = pending
            self.event(project["id"], chat_id, "model_choice_required", prompt,
                       {"request_id": choice_id, "options": options})
            try:
                choice = pending["result"].get(timeout=270)
                if choice is None:
                    raise ValueError("Subagent model selection interrupted")
                return choice
            except Empty as exc:
                raise ValueError("Subagent model selection timed out") from exc
            finally:
                self.pending_model_choices.pop(choice_id, None)
        return self.choose_model(project, prompt, "subagent", exclude=exclude)

    def handle_antigravity_hook(self, payload):
        tool = payload.get("toolCall") or {}
        if tool.get("name") != "invoke_subagent":
            return {"decision": "allow"}
        conversation_id = payload.get("conversationId")
        chat = self._one("SELECT * FROM chats WHERE thread_id=? AND harness='antigravity'", (conversation_id,))
        if not chat:
            return {"decision": "allow"}
        project = self.project(chat["project_id"])
        if project["path"] not in payload.get("workspacePaths", []):
            return {"decision": "deny", "reason": "PolyAgent project working directory does not match the app conversation"}
        args = tool.get("args") or {}
        specs = args.get("Subagents")
        if isinstance(specs, list) and specs:
            prompts = [item.get("Prompt") for item in specs if isinstance(item, dict)]
        else:
            prompts = [args.get("Prompt")]
        if not prompts or any(not isinstance(prompt, str) or not prompt.strip() for prompt in prompts):
            return {"decision": "deny", "reason": "PolyAgent could not extract the native subagent prompt"}
        answers = []
        for prompt in prompts:
            try:
                self.event(project["id"], chat["id"], "native_subagent", prompt,
                           {"harness": "antigravity", "status": "intercepted", "redirected": True,
                            "prompt_available": True, "role": args.get("Role"), "type_name": args.get("TypeName")})
                selected = self._select_subagent_model(project, chat["id"], prompt)
                replacement_for = None
                attempted = set()
                while True:
                    attempted.add(selected)
                    child = None
                    try:
                        child = self.create_subagent(project["id"], prompt, selected,
                                                     replacement_for=replacement_for, parent_chat_id=chat["id"])
                        answer = self._wait_subagent_response(child["chat_id"])
                        answers.append(answer)
                        self.event(project["id"], chat["id"], "subagent_response", answer,
                                   {"model": selected, "harness": self._one("SELECT harness FROM models WHERE id=?", (selected,))["harness"],
                                    "subagent_chat_id": child["chat_id"]})
                        break
                    except Exception:
                        if child and self._one("SELECT status FROM chats WHERE id=?", (child["chat_id"],))["status"] == "interrupted":
                            raise
                        if self._one("SELECT status FROM chats WHERE id=?", (chat["id"],))["status"] == "interrupted":
                            raise
                        if project["mode"] in {"manual", "hybrid"} and project["locked_subagent"] or project["mode"] == "manual":
                            raise
                        replacement_for = child["chat_id"] if child else None
                        selected = self._select_subagent_model(project, chat["id"], prompt, exclude=attempted)
            except Exception as exc:
                self.event(project["id"], chat["id"], "error", str(exc), {})
                return {"decision": "deny", "reason": f"PolyAgent delegation failed: {exc}"}
        return {"decision": "deny", "reason": "PolyAgent completed the delegated task in a separate chat. "
                "Use the following subagent response as the tool result:\n" + "\n\n".join(answers)}

    def handle_opencode_hook(self, payload):
        session_id = payload.get("sessionID")
        chat = self._one("SELECT * FROM chats WHERE thread_id=? AND harness='opencode'", (session_id,))
        if not chat:
            return {"handled": False}
        project = self.project(chat["project_id"])
        call = payload.get("input") or {}
        prompt = call.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("OpenCode native subagent call has no delegated prompt")
        if call.get("background") is True:
            self.event(project["id"], chat["id"], "native_subagent", prompt,
                       {"harness": "opencode", "status": "observed", "redirected": False,
                        "prompt_available": True, "reason": "background call"})
            return {"handled": False}
        prior_id = call.get("sessionID")
        if prior_id and not str(prior_id).startswith("ses_polyagent_"):
            return {"handled": False}
        if prior_id:
            link = self._one("SELECT * FROM subagent_links WHERE external_id=? AND parent_chat_id=?",
                             (prior_id, chat["id"]))
            if not link:
                raise ValueError("PolyAgent subagent session was not found")
            child_id = link["child_chat_id"]
            child = self._one("SELECT * FROM chats WHERE id=? AND role='subagent'", (child_id,))
            if not child:
                raise ValueError("PolyAgent subagent chat was not found")
            self.event(project["id"], chat["id"], "native_subagent", prompt,
                       {"harness": "opencode", "status": "continued", "redirected": True,
                        "prompt_available": True})
            self.continue_subagent(child_id, prompt)
            answer = self._wait_subagent_response(child_id)
            self.event(project["id"], chat["id"], "subagent_response", answer,
                       {"model": child["model"], "harness": child["harness"],
                        "subagent_chat_id": child_id})
            return {"handled": True, "sessionID": prior_id, "output": answer}
        self.event(project["id"], chat["id"], "native_subagent", prompt,
                   {"harness": "opencode", "status": "intercepted", "redirected": True,
                    "prompt_available": True, "agent": call.get("agent")})
        selected = self._select_subagent_model(project, chat["id"], prompt)
        attempted = set()
        replacement_for = None
        while True:
            attempted.add(selected)
            child = None
            try:
                child = self.create_subagent(project["id"], prompt, selected,
                                             replacement_for=replacement_for, parent_chat_id=chat["id"])
                answer = self._wait_subagent_response(child["chat_id"])
                external_id = "ses_polyagent_" + uuid.uuid4().hex
                self._execute("INSERT INTO subagent_links(external_id,parent_chat_id,child_chat_id,created_at) VALUES(?,?,?,?)",
                              (external_id, chat["id"], child["chat_id"], time.time()))
                self.event(project["id"], chat["id"], "subagent_response", answer,
                           {"model": selected,
                            "harness": self._one("SELECT harness FROM models WHERE id=?", (selected,))["harness"],
                            "subagent_chat_id": child["chat_id"]})
                return {"handled": True, "sessionID": external_id, "output": answer}
            except Exception:
                if child and self._one("SELECT status FROM chats WHERE id=?", (child["chat_id"],))["status"] == "interrupted":
                    raise
                if self._one("SELECT status FROM chats WHERE id=?", (chat["id"],))["status"] == "interrupted":
                    raise
                if project["mode"] == "manual" or project["mode"] == "hybrid" and project["locked_subagent"]:
                    raise
                replacement_for = child["chat_id"] if child else None
                selected = self._select_subagent_model(project, chat["id"], prompt, exclude=attempted)

    def handle_grok_hook(self, payload):
        if payload.get("hookEventName") not in {"PreToolUse", "pre_tool_use"} or \
                payload.get("toolName") not in {"task", "Task", "spawn_subagent"}:
            return {"decision": "allow"}
        chat = self._one("SELECT * FROM chats WHERE thread_id=? AND harness='grok_build' AND role='orchestrator'",
                         (payload.get("sessionId"),))
        if not chat:
            return {"decision": "allow"}
        project = self.project(chat["project_id"])
        if Path(payload.get("cwd") or "").resolve() != Path(project["path"]).resolve():
            return {"decision": "allow"}
        call = payload.get("toolInput") or {}
        if not isinstance(call, dict) or call.get("isolation") == "worktree" or call.get("resume_from"):
            return {"decision": "allow"}
        prompt = call.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            return {"decision": "allow"}
        self.event(project["id"], chat["id"], "native_subagent", prompt,
                   {"harness": "grok_build", "status": "intercepted", "redirected": True,
                    "prompt_available": True})
        selected = self._select_subagent_model(project, chat["id"], prompt)
        attempted = set()
        replacement_for = None
        while True:
            attempted.add(selected)
            child = None
            try:
                child = self.create_subagent(project["id"], prompt, selected,
                                             replacement_for=replacement_for, parent_chat_id=chat["id"])
                answer = self._wait_subagent_response(child["chat_id"])
                self.event(project["id"], chat["id"], "subagent_response", answer,
                           {"model": selected,
                            "harness": self._one("SELECT harness FROM models WHERE id=?", (selected,))["harness"],
                            "subagent_chat_id": child["chat_id"]})
                with self.grok_followups_lock:
                    self.grok_followups.setdefault(chat["id"], []).append(answer)
                return {"decision": "deny", "reason": "PolyAgent completed the delegated task in a separate chat. "
                        "Its full response will be delivered in this Grok session's next message."}
            except Exception:
                if child and self._one("SELECT status FROM chats WHERE id=?", (child["chat_id"],))["status"] == "interrupted":
                    raise
                if self._one("SELECT status FROM chats WHERE id=?", (chat["id"],))["status"] == "interrupted":
                    raise
                if project["mode"] == "manual" or project["mode"] == "hybrid" and project["locked_subagent"]:
                    raise
                replacement_for = child["chat_id"] if child else None
                selected = self._select_subagent_model(project, chat["id"], prompt, exclude=attempted)

    def _on_codex_event(self, event):
        if "id" in event and "method" in event:
            params = event.get("params", {})
            thread_id = params.get("threadId")
            chat = self._one("SELECT * FROM chats WHERE thread_id=?", (thread_id,)) if thread_id else None
            method = event["method"]
            interactive = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/tool/requestUserInput"}
            if chat and method in interactive:
                self.approvals[str(event["id"])] = event
                self.event(chat["project_id"], chat["id"], "user_input" if method == "item/tool/requestUserInput" else "approval", method,
                           {"request_id": str(event["id"]), "params": params})
            elif method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                self.codex.answer(event["id"], {"decision": "decline"})
            else:
                self.codex.answer_error(event["id"], "This Codex request type is not supported by PolyAgent")
                if chat:
                    self.event(chat["project_id"], chat["id"], "error", f"Unsupported Codex request: {method}", {})
            return
        method = event.get("method")
        params = event.get("params") or {}
        thread_id = params.get("threadId")
        if not thread_id:
            return
        chat = self._one("SELECT * FROM chats WHERE thread_id=?", (thread_id,))
        if not chat:
            if method == "turn/completed":
                turn = params.get("turn") or {}
                waiter = self.waiters.get((thread_id, turn.get("id")))
                if waiter:
                    waiter.put(turn.get("status", "unknown"))
                else:
                    self.completed_turns[(thread_id, turn.get("id"))] = turn.get("status", "unknown")
            elif method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage":
                    self.turn_messages[(thread_id, params.get("turnId"))] = item.get("text", "")
            return
        project_id, chat_id = chat["project_id"], chat["id"]
        if method == "turn/started":
            turn_id = (params.get("turn") or {}).get("id")
            if turn_id:
                self.active_turns[chat_id] = turn_id
        elif method == "item/agentMessage/delta":
            self.event(project_id, chat_id, "delta", params.get("delta", ""),
                       {"model": chat["model"], "harness": chat["harness"]})
        elif method in {"item/started", "item/completed"}:
            item = params.get("item") or {}
            item_type = item.get("type")
            if item_type == "subAgentActivity" and method == "item/completed":
                child_id = item.get("agentThreadId")
                if item.get("model") and child_id:
                    self.native_models[child_id] = item["model"]
                self.event(project_id, chat_id, "native_subagent", "",
                           {"status": item.get("kind"), "agent_path": item.get("agentPath"),
                            "receiver_thread_ids": [child_id],
                            "model": item.get("model") or self.native_models.get(child_id), "harness": "codex", "phase": method,
                            "prompt_available": False, "redirected": False})
            elif item_type == "collabAgentToolCall":
                tool = item.get("tool")
                if tool == "spawnAgent":
                    self.event(project_id, chat_id, "native_subagent", item.get("prompt") or "",
                               {"status": item.get("status"), "receiver_thread_ids": item.get("receiverThreadIds", []),
                                "model": item.get("model"), "harness": "codex", "phase": method,
                                "prompt_available": bool(item.get("prompt")), "redirected": False})
            elif method == "item/completed" and item_type == "agentMessage":
                self.turn_messages[(thread_id, params.get("turnId"))] = item.get("text", "")
                if self._one("SELECT status FROM chats WHERE id=?", (chat_id,))["status"] == "running":
                    self.event(project_id, chat_id, "assistant", item.get("text", ""),
                               {"model": chat["model"], "harness": chat["harness"]})
            elif item_type in {"commandExecution", "fileChange", "mcpToolCall", "webSearch"}:
                self.event(project_id, chat_id, "tool", item_type,
                           {"phase": method, "item": item, "model": chat["model"], "harness": chat["harness"]})
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            status = turn.get("status", "unknown")
            self._finish_chat(chat, status)
            waiter = self.waiters.get((thread_id, turn.get("id")))
            if waiter:
                waiter.put(status)
        elif method == "error":
            self.event(project_id, chat_id, "error", str(params.get("error") or params), {})

    def answer_approval(self, request_id, decision):
        if decision not in {"accept", "decline"}:
            raise ValueError("Invalid approval decision")
        antigravity_request = self.antigravity_permissions.get(str(request_id))
        if antigravity_request:
            thread_id, trajectory_id, index, kind, value, project_id, chat_id = antigravity_request
            self.antigravity.answer_permission(thread_id, trajectory_id, index, kind, value, decision)
            self.antigravity_permissions.pop(str(request_id), None)
            self.event(project_id, chat_id, "request_resolved", decision, {"request_id": str(request_id)})
            return {"answered": True}
        opencode_request = self.opencode_permissions.get(str(request_id))
        if opencode_request:
            client, session_id, native_id, directory, project_id, chat_id = opencode_request
            client.answer_permission(session_id, native_id, directory, decision)
            self.opencode_permissions.pop(str(request_id), None)
            self.event(project_id, chat_id, "request_resolved", decision, {"request_id": str(request_id)})
            return {"answered": True}
        acp_request = self.acp_permissions.get(str(request_id))
        if acp_request:
            if decision not in {"accept", "decline"}:
                raise ValueError("Invalid approval decision")
            client, request = acp_request
            wanted = "allow_once" if decision == "accept" else "reject_once"
            option = next((item for item in request.get("params", {}).get("options", []) if item.get("kind") == wanted), None)
            if not option:
                raise ValueError("The harness did not offer this permission choice")
            client.answer(request["id"], {"outcome": {"outcome": "selected",
                          "optionId": option["optionId"]}})
            self.acp_permissions.pop(str(request_id), None)
            chat = self._one("SELECT * FROM chats WHERE thread_id=?", (request.get("params") or {}).get("sessionId",))
            if chat:
                self.event(chat["project_id"], chat["id"], "request_resolved", decision,
                           {"request_id": str(request_id)})
            return {"answered": True}
        request = self.approvals.get(str(request_id))
        if not request:
            raise ValueError("Approval request not found")
        method = request["method"]
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            if decision not in {"accept", "decline"}:
                raise ValueError("Invalid approval decision")
            self.codex.answer(request["id"], {"decision": decision})
        else:
            raise ValueError("This approval type is not supported in PolyAgent")
        self.approvals.pop(str(request_id), None)
        chat = self._one("SELECT * FROM chats WHERE thread_id=?", (request.get("params", {}).get("threadId"),))
        if chat:
            self.event(chat["project_id"], chat["id"], "request_resolved", decision,
                       {"request_id": str(request_id)})
        return {"answered": True}

    def answer_user_input(self, request_id, answers):
        request = self.approvals.get(str(request_id))
        if not request or request["method"] != "item/tool/requestUserInput":
            raise ValueError("Codex question request not found")
        questions = request.get("params", {}).get("questions", [])
        result = {}
        for question in questions:
            question_id = question["id"]
            answer = str(answers.get(question_id, "")).strip()
            if not answer:
                raise ValueError(f"Answer required: {question.get('header') or question_id}")
            result[question_id] = {"answers": [answer]}
        self.codex.answer(request["id"], {"answers": result})
        self.approvals.pop(str(request_id), None)
        chat = self._one("SELECT * FROM chats WHERE thread_id=?", (request.get("params", {}).get("threadId"),))
        if chat:
            self.event(chat["project_id"], chat["id"], "request_resolved", "answered",
                       {"request_id": str(request_id)})
        return {"answered": True}
