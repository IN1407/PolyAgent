"""Detect installed coding harness entry points without starting a session.

An installed desktop app is evidence of installation, not evidence that its
private conversations can be controlled by PolyAgent.
"""

import os
import plistlib
import shutil
import subprocess
from pathlib import Path


# CLI names and desktop bundle names here refer to distinct, published products.
# A model provider alone (for example DeepSeek) is not a coding harness.
HARNESSES = (
    {"id": "codex", "name": "OpenAI Codex", "commands": ("codex",),
     "apps": ("ChatGPT", "Codex"), "bundle_ids": ("com.openai.codex",),
     "app_cli": ("Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex",
                 "Contents/Resources/codex-cli/bin/codex"), "connectable": True},
    {"id": "antigravity", "name": "Google Antigravity", "commands": ("agy",),
     "apps": ("Antigravity",), "bundle_ids": ("com.google.antigravity",),
     "app_connectable": True},
    {"id": "claude_code", "name": "Claude Code", "commands": ("claude",),
     "apps": (), "bundle_ids": ()},
    {"id": "deepseek_harness", "name": "DeepSeek Harness", "commands": ("dsh",),
     "apps": ("DeepSeek Harness",), "bundle_ids": ("com.deepseek.harness",),
     "app_cli": ("Contents/Resources/runtime/cli/bin/dsh",), "connectable": True},
    {"id": "grok_build", "name": "Grok Build", "commands": ("grok", "grok-build"),
     "apps": (), "bundle_ids": (), "connectable": True},
    {"id": "kimi_code", "name": "Kimi Code", "commands": ("kimi",),
     "apps": ("Kimi Code", "Kimi"), "bundle_ids": ()},
    {"id": "qwen_code", "name": "Qwen Code", "commands": ("qwen",),
     "apps": ("Qwen Code",), "bundle_ids": ()},
    {"id": "mistral_vibe", "name": "Mistral Vibe", "commands": ("vibe",),
     "apps": ("Vibe", "Mistral Vibe"), "bundle_ids": ()},
    {"id": "opencode", "name": "OpenCode", "commands": ("opencode",),
     "apps": ("OpenCode",), "bundle_ids": ("ai.opencode.desktop",),
     "app_cli": ("Contents/Resources/opencode",), "connectable": True},
    {"id": "hermes", "name": "Hermes Agent", "commands": ("hermes",),
     "apps": ("Hermes", "Hermes Agent"), "bundle_ids": ()},
    {"id": "pi", "name": "Pi", "commands": ("pi",), "apps": (), "bundle_ids": ()},
    {"id": "oh_my_pi", "name": "Oh My Pi", "commands": ("omp",),
     "apps": (), "bundle_ids": ()},
    {"id": "openclaw", "name": "OpenClaw", "commands": ("openclaw",),
     "apps": ("OpenClaw",), "bundle_ids": ()},
    {"id": "aider", "name": "Aider", "commands": ("aider",),
     "apps": (), "bundle_ids": ()},
    {"id": "zcode", "name": "Zcode", "commands": ("zcode",),
     "apps": ("Zcode",), "bundle_ids": ()},
    {"id": "sarvam_code", "name": "Sarvam Code", "commands": ("sarvam-code",),
     "apps": ("Sarvam Code",), "bundle_ids": ()},
)


def _app_bundles(roots):
    """Inspect only app metadata. Never execute a desktop app while scanning."""
    bundles = []
    for root in roots:
        root = Path(root).expanduser()
        if not root.is_dir():
            continue
        for current, dirs, _ in os.walk(root, followlinks=False):
            depth = len(Path(current).relative_to(root).parts)
            if depth > 2:
                dirs[:] = []
                continue
            for dirname in list(dirs):
                if not dirname.endswith(".app"):
                    continue
                dirs.remove(dirname)
                bundle = Path(current) / dirname
                if bundle.is_symlink():
                    continue
                try:
                    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
                except (OSError, ValueError, TypeError):
                    continue
                executable = bundle / "Contents/MacOS" / info.get("CFBundleExecutable", "")
                if executable.is_file() and os.access(executable, os.X_OK):
                    bundles.append({"path": str(bundle),
                                    "name": info.get("CFBundleName") or bundle.stem,
                                    "bundle_id": info.get("CFBundleIdentifier", ""),
                                    "version": info.get("CFBundleShortVersionString", "")})
    return bundles


def _cli_probe(path):
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True,
                                timeout=4, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode:
        return False, (result.stderr or result.stdout).strip()[:180] or f"exit {result.returncode}"
    return True, (result.stdout or result.stderr).strip().splitlines()[0][:120] if (result.stdout or result.stderr).strip() else "ready"


def discover(*, app_roots=None, which=None, probe=None, specs=HARNESSES):
    """Prefer a working PATH CLI; when absent or failing, detect a desktop app."""
    roots = app_roots if app_roots is not None else ("/Applications", Path.home() / "Applications")
    bundles = _app_bundles(roots)
    which = which or shutil.which
    probe = probe or _cli_probe
    found = []
    for spec in specs:
        cli = None
        cli_error = None
        for command in spec["commands"]:
            path = which(command)
            if not path:
                continue
            ready, detail = probe(path)
            if ready:
                cli = {"path": path, "version": detail}
                break
            cli_error = f"{command}: {detail}"
        app = next((b for b in bundles if b["bundle_id"] in spec["bundle_ids"] or
                    b["name"].casefold() in {name.casefold() for name in spec["apps"]} or
                    Path(b["path"]).stem.casefold() in {name.casefold() for name in spec["apps"]}), None)
        selected = "cli" if cli else "app" if app else None
        connectable = (spec.get("connectable") and (cli or bundled_cli(app, spec)) or
                       spec.get("app_connectable") and (cli or app))
        found.append({"id": spec["id"], "name": spec["name"], "cli": cli,
                      "cli_error": cli_error, "app": app, "selected": selected,
                      "connectable": bool(connectable),
                      "integration": "codex_app_server" if spec["id"] == "codex" else
                      "opencode_server" if spec["id"] == "opencode" else
                      "deepseek_native_host" if spec["id"] == "deepseek_harness" else
                      "grok_cli" if spec["id"] == "grok_build" else
                      "antigravity_cli_or_app" if spec.get("app_connectable") and (cli or app) else None})
    return found


def codex_app_cli(app):
    return bundled_cli(app, HARNESSES[0])


def bundled_cli(app, spec):
    if not app:
        return None
    for relative in spec.get("app_cli", ()):
        path = Path(app["path"]) / relative
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None
