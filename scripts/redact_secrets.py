"""Redact secret-shaped values from logs/artifacts before upload.

Reads files (or stdin) and masks bearer tokens, password/token/key
assignments, and embedded URL credentials. Everything else passes through
byte-identical, so eval JSON and Markdown reports are unaffected. Used by
CI failure-artifact steps; safe to run even when nothing matches.
"""

from __future__ import annotations

import argparse
import re
import sys

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Bearer <token>
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9\-._~+/=]+"), r"\1 [REDACTED]"),
    # password=... / "token": "..." / api-key: ... (quoted or bare keys and
    # values), including SNAKE_CASE env names such as FAULT_CONTROL_TOKEN
    # and JSON object keys such as {"db_password": "change-me"}. Deliberately
    # overlaps packages/telemetry/logging.py without sharing the pattern: the
    # hot path bounds values and normalizes separators, this script preserves
    # artifact text verbatim apart from secrets.
    (
        re.compile(
            r"""(?ix)
            ("?)\b([A-Za-z_]*(?:authorization|password|passwd|pwd|token|api[-_]?key|secret|client[-_]?secret))\b("?)
            (\s*[:=]\s*)("[^"]*"|'[^']*'|\S+)
            """
        ),
        r"\1\2\3\4[REDACTED]",
    ),
    # scheme://user:password@host credentials in URLs. The userinfo match is
    # greedy up to the last "@" before whitespace so generated passwords
    # containing "@" or "/" are fully masked (over-masking a malformed line
    # is safer than leaking a secret).
    (re.compile(r"(://[^:/\s]+:)[^\s]*(@)"), r"\1[REDACTED]\2"),
)


def redact_text(text: str) -> str:
    """Mask secret-shaped values, leaving all other text untouched."""

    redacted = text
    for pattern, replacement in _PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", help="Files to redact in place (default: stdin)")
    parser.add_argument("--in-place", action="store_true", help="Rewrite files instead of stdout")
    args = parser.parse_args()

    if not args.paths:
        sys.stdout.write(redact_text(sys.stdin.read()))
        return 0

    for raw in args.paths:
        with open(raw, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        redacted = redact_text(text)
        if args.in_place:
            if redacted != text:
                with open(raw, "w", encoding="utf-8") as handle:
                    handle.write(redacted)
        else:
            sys.stdout.write(redacted)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
