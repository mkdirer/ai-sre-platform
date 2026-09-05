"""Local platform validation: Helm chart + rendered Kubernetes manifests.

Runs everything the local toolchain allows and reports SKIP (never FAIL)
for missing prerequisites:
  - helm lint            (needs: helm)
  - helm template        (needs: helm)
  - kubeconform -strict  (needs: kubeconform; fetches schemas on first run)
  - kubectl dry-run      (needs: kubectl binary only; client-side, no cluster)

Exit 0 only when every applicable check passes. Used by `make k8s-validate`
and CI (which installs the same pinned tool versions).
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CHART = REPO_ROOT / "infrastructure" / "helm" / "ai-sre-platform"


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | FAIL | SKIP
    detail: str = ""


@dataclass
class Report:
    results: list[CheckResult] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.results.append(CheckResult(name, status, detail))
        print(f"[{status}] {name}" + (f" — {detail}" if detail else ""), flush=True)

    def failed(self) -> bool:
        return any(result.status == "FAIL" for result in self.results)


def _run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)  # type: ignore[arg-type]


def check_helm_lint(report: Report) -> Path | None:
    if shutil.which("helm") is None:
        report.add("helm-lint", "SKIP", "helm not on PATH (see runbook prerequisites)")
        return None
    proc = _run(["helm", "lint", str(CHART)])
    if proc.returncode != 0:
        report.add("helm-lint", "FAIL", proc.stdout.strip()[-2000:])
        return None
    report.add("helm-lint", "PASS", "chart passes helm lint (incl. values.schema.json)")
    return CHART


def check_helm_template(report: Report, out_dir: Path) -> Path | None:
    if shutil.which("helm") is None:
        report.add("helm-template", "SKIP", "helm not on PATH")
        return None
    rendered = out_dir / "rendered.yaml"
    proc = _run(
        [
            "helm",
            "template",
            "ai-sre-demo",
            str(CHART),
            "--namespace",
            "ai-sre-demo",
            "--output-dir",
            str(out_dir),
        ]
    )
    if proc.returncode != 0:
        report.add("helm-template", "FAIL", proc.stderr.strip()[-2000:])
        return None
    # Flatten helm's per-template files into one manifest for kubeconform.
    # Skip our own flattened output on reused --out-dir runs (self-ingestion
    # would duplicate every document).
    docs = sorted(p for p in out_dir.rglob("*.yaml") if p.name != rendered.name)
    if not docs:
        report.add("helm-template", "FAIL", "no manifests rendered")
        return None
    with rendered.open("w", encoding="utf-8") as sink:
        for doc in docs:
            text = doc.read_text(encoding="utf-8")
            if text.strip() and "kind:" in text:
                sink.write(text if text.endswith("\n") else text + "\n")
    report.add("helm-template", "PASS", f"{len(docs)} templates rendered")
    return rendered


def check_kubeconform(report: Report, rendered: Path | None) -> None:
    if rendered is None:
        report.add("kubeconform", "SKIP", "nothing rendered to validate")
        return
    if shutil.which("kubeconform") is None:
        report.add("kubeconform", "SKIP", "kubeconform not on PATH")
        return
    proc = _run(["kubeconform", "-strict", "-summary", str(rendered)])
    if proc.returncode != 0:
        report.add("kubeconform", "FAIL", (proc.stdout + proc.stderr).strip()[-2000:])
        return
    summary = proc.stdout.strip().splitlines()
    report.add("kubeconform", "PASS", summary[-1] if summary else "no output")


def check_kubectl_dry_run(report: Report, rendered: Path | None) -> None:
    if rendered is None:
        report.add("kubectl-dry-run", "SKIP", "nothing rendered to validate")
        return
    if shutil.which("kubectl") is None:
        report.add("kubectl-dry-run", "SKIP", "kubectl not on PATH")
        return
    proc = _run(["kubectl", "apply", "--dry-run=client", "-f", str(rendered)])
    combined = (proc.stdout + proc.stderr).strip()
    if proc.returncode != 0:
        if "connect: connection refused" in combined or "no such host" in combined:
            report.add(
                "kubectl-dry-run",
                "SKIP",
                "no cluster context (kind/Minikube); kubeconform already schema-validated",
            )
            return
        report.add("kubectl-dry-run", "FAIL", combined[-2000:])
        return
    report.add("kubectl-dry-run", "PASS", "client-side apply dry-run accepted")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default=None, help="Keep rendered manifests here")
    args = parser.parse_args()

    report = Report()
    check_helm_lint(report)
    with tempfile.TemporaryDirectory(prefix="ai-sre-render-") as tmp:
        out_dir = Path(args.out_dir) if args.out_dir else Path(tmp)
        out_dir.mkdir(parents=True, exist_ok=True)
        rendered = check_helm_template(report, out_dir)
        check_kubeconform(report, rendered)
        check_kubectl_dry_run(report, rendered)

    if report.failed():
        print("platform validation FAILED", flush=True)
        return 1
    print("platform validation OK (skips, if any, are listed above)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
