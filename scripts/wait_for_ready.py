"""Poll service readiness with a deadline instead of fixed sleeps.

Used by `make demo` after `docker compose up`: waits until the gateway and
Incident API report /health/ready (or fails loudly with the last error).
This is a minimal entry-point gate, not full-stack health: downstream scripts
(scenario/smoke runners) poll the backends they need themselves, and
`docker compose ps` shows the rest. Every wait in the demo path is a bounded
poll like this one.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.request


def _ready(url: str, timeout_seconds: float) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout_seconds) as response:
            if response.status == 200:
                return True, "ok"
            return False, f"HTTP {response.status}"
    except Exception as error:  # readiness poller never crashes on fetch errors
        return False, type(error).__name__


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--deadline", type=float, default=180.0, help="Total seconds to wait (default: 180)"
    )
    parser.add_argument("--gateway-url", default=os.environ.get("DEMO_GATEWAY_URL", ""))
    parser.add_argument("--incident-api-url", default=os.environ.get("DEMO_INCIDENT_API_URL", ""))
    args = parser.parse_args()

    targets = {
        "gateway": f"{args.gateway_url or 'http://127.0.0.1:8001'}/health/ready",
        "incident-api": f"{args.incident_api_url or 'http://127.0.0.1:8006'}/health/ready",
    }
    deadline = time.monotonic() + args.deadline
    pending = dict(targets)
    last_error = "no response"
    while pending and time.monotonic() < deadline:
        for name, url in list(pending.items()):
            ok, detail = _ready(url, timeout_seconds=5.0)
            if ok:
                print(f"ready: {name} ({url})", flush=True)
                del pending[name]
            else:
                last_error = f"{name}: {detail}"
        if pending:
            time.sleep(2.0)
    if pending:
        print(
            f"DEMO NOT READY: {sorted(pending)} did not report ready: {last_error}",
            file=sys.stderr,
        )
        return 1
    print("demo platform ready", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
