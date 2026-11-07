"""Native harness mode vocabulary, sourced in HARNESS_RESEARCH.md."""


def options(*entries):
    return [{"value": entry[0], "label": entry[1],
             **({"description": entry[2]} if len(entry) > 2 else {})} for entry in entries]


def field(key, label, description, *, creation_only=False):
    return {"key": key, "label": label, "description": description, "creation_only": creation_only}


INHERIT = ("harness", "Use harness settings")
STATIC = {
    "codex": {
        "fields": [field("approval", "Approval policy", "When Codex asks permission to run an action."),
                   field("sandbox", "Sandbox mode", "File and command access allowed to Codex."),
                   field("execution", "Collaboration mode", "Choose Codex's Default or Plan behavior.")],
        "approval": options(INHERIT, ("untrusted", "Ask for untrusted actions"),
                            ("on-request", "Ask when requested"),
                            ("never", "Never ask")),
        "sandbox": options(INHERIT, ("read-only", "Read only"), ("workspace-write", "Workspace write"),
                           ("danger-full-access", "Full access")),
        "execution": options(INHERIT, ("default", "Default"), ("plan", "Plan")),
    },
    "antigravity": {
        "fields": [field("approval", "Terminal execution policy", "Controls review of terminal commands in the Antigravity app."),
                   field("artifact_review", "Artifact review policy", "Controls review of plans and artifacts independently of terminal commands.")],
        "approval": options(INHERIT, ("request-review", "Request review"),
                            ("proceed-in-sandbox", "Proceed in sandbox"), ("auto", "Auto"),
                            ("always-proceed", "Always proceed")),
        "artifact_review": options(INHERIT, ("request-review", "Request review"),
                                   ("auto", "Auto"), ("always-proceed", "Always proceed")),
    },
    "opencode": {
        "fields": [field("approval", "Tool permission rules", "Applies Ask, Allow, or Deny to native tools; use harness settings to retain your per-tool rules."),
                   field("execution", "Primary agent", "Choose from the primary agents advertised by your OpenCode installation.")],
        "approval": options(INHERIT, ("ask", "Ask"), ("allow", "Allow"), ("deny", "Deny")),
        "execution": [],
    },
    "grok_build": {
        "fields": [field("approval", "Permission mode", "Grok's Ask, Auto, or Always approve permission policy. Auto requires native Auto support."),
                   field("execution", "Session mode", "Choose Grok's Normal, Plan, or Ask session behavior.")],
        "approval": options(INHERIT, ("ask", "Ask"), ("auto", "Auto"),
                            ("always-approve", "Always approve")),
        "execution": options(INHERIT, ("default", "Normal"), ("plan", "Plan"), ("ask", "Ask")),
    },
    "deepseek_harness": {
        "fields": [field("approval", "Permission preset", "Permission presets supplied by your DeepSeek Harness installation."),
                   field("execution", "Agent preset", "Choose the native agent preset for a new DeepSeek chat.", creation_only=True)],
        "approval": options(INHERIT), "execution": options(INHERIT)},
}

ANTIGRAVITY_CLI_FIELDS = [
    field("approval", "Tool permission policy", "The CLI's toolPermission setting. Native permission rules take precedence over execution mode."),
    field("execution", "Execution mode", "Default, Accept edits, Auto, or Plan, as supported by the native CLI."),
]


def compatible(values, capability):
    """Keep saved choices only where the current native transport supports them."""
    return {key: value for key, value in values.items()
            if any(entry["value"] == value for entry in capability.get(key, [])
                   if isinstance(entry, dict) and "value" in entry)}


def validate(values, capability):
    if not isinstance(values, dict):
        raise ValueError("Harness settings must be an object")
    result = {}
    for key, value in values.items():
        if key not in {item["key"] for item in capability.get("fields", [])}:
            raise ValueError("Unknown harness setting")
        if value not in {entry["value"] for entry in capability.get(key, [])}:
            raise ValueError(f"Unsupported {key} mode: {value}")
        result[key] = value
    return result
