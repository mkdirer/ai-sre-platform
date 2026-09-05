"""Coverage for secret redaction used by CI failure artifacts (Stage 11)."""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "redact_secrets.py"


def _redact(stdin: str) -> str:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def test_bearer_and_assignments_are_masked() -> None:
    """Secret-shaped values are replaced; structure is preserved."""

    out = _redact(
        "Authorization: Bearer s3cr3t\n"
        "FAULT_CONTROL_TOKEN=local-demo-fault-control\n"
        'postgres_password = "change-me"\n'
    )
    assert "s3cr3t" not in out
    assert "local-demo-fault-control" not in out
    assert "change-me" not in out
    assert out.count("[REDACTED]") == 3
    assert "Authorization: Bearer" in out
    assert "FAULT_CONTROL_TOKEN=" in out


def test_url_credentials_are_masked() -> None:
    """Embedded userinfo never reaches uploaded logs."""

    out = _redact("db=postgres://aisre:pwd123@db:5432/aisre ok\n")
    assert "pwd123" not in out
    assert "postgres://aisre:[REDACTED]@db:5432/aisre ok" in out


def test_json_quoted_keys_are_masked() -> None:
    """JSON-encoded secrets (structured logs) are redacted, not just bare pairs."""

    out = _redact('{"postgres_password": "change-me", "token": "s3cr3t", "ok": true}\n')
    assert "change-me" not in out
    assert "s3cr3t" not in out
    assert '"postgres_password": [REDACTED]' in out
    assert '"token": [REDACTED]' in out
    assert '"ok": true' in out


def test_password_containing_at_sign_is_fully_masked() -> None:
    """Generated passwords with '@' must not leave a suffix exposed."""

    out = _redact("db=postgres://aisre:p@ss@db:5432/aisre ok\n")
    assert "p@ss" not in out
    assert "postgres://aisre:[REDACTED]@db:5432/aisre ok" in out


def test_benign_eval_and_report_text_passes_through() -> None:
    """Grader output and prose must be byte-identical after redaction."""

    benign = (
        '{"evidence_references": ["EVD-ABC"], "cost_usd": 0.0}\n'
        "| SCN-001 | true | true | true | true | true | true | true |\n"
    )
    assert _redact(benign) == benign
