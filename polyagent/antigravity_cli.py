"""Official Antigravity CLI stream-json session transport."""

import json
import select
import signal
import subprocess
import threading
from pathlib import Path


class AntigravityCliError(RuntimeError):
    pass


class AntigravityCliClient:
    def __init__(self, executable, on_event):
        self.executable = executable
        self.on_event = on_event
        self.sessions = {}
        self.lock = threading.RLock()
        self.stderr_lines = []
        self.session_modes = {}

    def connect(self):
        try:
            result = subprocess.run([self.executable, "models"], capture_output=True,
                                    text=True, timeout=25, check=False)
            if result.returncode:
                raise AntigravityCliError((result.stderr or result.stdout).strip()[:300] or "model discovery failed")
            catalog = {}
            for line in result.stdout.splitlines():
                if "\t" not in line:
                    continue
                slug, name = line.split("\t", 1)
                if slug and name:
                    catalog[slug] = {"displayName": name.strip(), "model": slug}
            if not catalog:
                raise AntigravityCliError("CLI returned no usable models")
            auth = subprocess.run([self.executable, "-p", "/model", "--output-format", "json"],
                                  capture_output=True, text=True, timeout=25, check=False)
            if auth.returncode:
                raise AntigravityCliError((auth.stderr or auth.stdout).strip()[:300] or "CLI authentication failed")
            status = json.loads(auth.stdout)
            if status.get("status") != "SUCCESS":
                raise AntigravityCliError(status.get("error") or "CLI authentication failed")
            return catalog
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            raise AntigravityCliError(f"Antigravity CLI connection failed: {exc}") from exc

    def _start(self, project, model_info, resume_id=None, settings=None):
        args = [self.executable, "--input-format", "stream-json", "--output-format", "stream-json",
                "--model", model_info["model"]]
        settings = settings or {}
        approval = settings.get("approval", "harness")
        if approval != "harness":
            path = Path.home() / ".gemini" / "antigravity-cli" / "settings.json"
            current = json.loads(path.read_text()) if path.exists() else {}
            if current.get("toolPermission") != approval:
                current["toolPermission"] = approval
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name("settings.json.polyagent-tmp")
                temporary.write_text(json.dumps(current, indent=2) + "\n")
                temporary.replace(path)
        execution = settings.get("execution", "harness")
        if execution != "harness":
            args.extend(["--mode", "" if execution == "default" else execution])
        if resume_id:
            args.extend(["--conversation", resume_id])
        try:
            process = subprocess.Popen(args, cwd=project["path"], stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       text=True, bufsize=1)
            ready, _, _ = select.select([process.stdout], [], [], 25)
            if not ready:
                process.terminate()
                raise AntigravityCliError("Antigravity CLI did not start a session")
            first = process.stdout.readline()
            message = json.loads(first)
            if message.get("event") != "init" or not message.get("conversation_id"):
                process.terminate()
                raise AntigravityCliError("Antigravity CLI did not return a session ID")
            thread_id = message["conversation_id"]
            if resume_id and thread_id != resume_id:
                process.terminate()
                raise AntigravityCliError("Antigravity CLI resumed a different conversation")
            with self.lock:
                self.sessions[thread_id] = process
                self.session_modes[thread_id] = settings
            threading.Thread(target=self._read, args=(thread_id, process), daemon=True).start()
            threading.Thread(target=self._read_stderr, args=(process,), daemon=True).start()
            return thread_id
        except (OSError, ValueError) as exc:
            raise AntigravityCliError(f"Antigravity CLI could not start a chat: {exc}") from exc

    def start_chat(self, project, model_info, settings=None):
        return self._start(project, model_info, settings=settings)

    def send(self, thread_id, message, project, model_info, settings=None):
        with self.lock:
            process = self.sessions.get(thread_id)
        if process and process.poll() is None and self.session_modes.get(thread_id, {}) != (settings or {}):
            with self.lock:
                self.sessions.pop(thread_id, None)
            process.terminate()
            process.wait(timeout=5)
            process = None
        if not process or process.poll() is not None:
            self._start(project, model_info, resume_id=thread_id, settings=settings)
            with self.lock:
                process = self.sessions[thread_id]
        try:
            process.stdin.write(json.dumps({"event": "user", "message": {"content": message}}) + "\n")
            process.stdin.flush()
        except OSError as exc:
            raise AntigravityCliError(f"Antigravity CLI could not send the message: {exc}") from exc

    def interrupt(self, thread_id):
        with self.lock:
            process = self.sessions.get(thread_id)
        if not process or process.poll() is not None:
            raise AntigravityCliError("Antigravity CLI session is no longer running")
        process.send_signal(signal.SIGINT)

    def _read(self, thread_id, process):
        for line in process.stdout:
            try:
                event = json.loads(line)
                self.on_event(thread_id, event)
            except (ValueError, TypeError) as exc:
                self.stderr_lines.append(f"Invalid CLI event: {exc}")
        with self.lock:
            if self.sessions.get(thread_id) is process:
                self.sessions.pop(thread_id, None)
        self.on_event(thread_id, {"event": "process_exit", "exit_code": process.wait()})

    def _read_stderr(self, process):
        for line in process.stderr:
            self.stderr_lines.append(line.strip())
            self.stderr_lines = self.stderr_lines[-20:]
