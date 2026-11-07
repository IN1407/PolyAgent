"""Antigravity PreToolUse bridge. Invoked by the native app, not an SDK."""

import json
import sys
from urllib.request import Request, urlopen


def main():
    try:
        payload = json.load(sys.stdin)
        request = Request(sys.argv[1], data=json.dumps(payload).encode(), method="POST",
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=1780) as response:
            result = json.load(response)
        if result.get("decision") not in {"allow", "deny"}:
            raise ValueError("PolyAgent returned an invalid hook decision")
    except Exception as exc:
        result = {"decision": "deny", "reason": f"PolyAgent delegation failed: {exc}"}
    print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
