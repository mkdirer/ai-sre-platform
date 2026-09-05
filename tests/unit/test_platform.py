"""Structural coverage for the Helm chart without requiring helm (Stage 11).

Pure-Python checks always run: values.yaml and values.schema.json stay in
lockstep, every image is pinned (never :latest), and HPA targets exist.
Rendering checks run only when the helm binary is present, otherwise skip
with the exact missing prerequisite.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
CHART = REPO_ROOT / "infrastructure" / "helm" / "ai-sre-platform"


def _values() -> dict[str, object]:
    with open(CHART / "values.yaml", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _schema() -> dict[str, object]:
    with open(CHART / "values.schema.json", encoding="utf-8") as handle:
        return json.load(handle)


def test_values_and_schema_cover_the_same_top_level_keys() -> None:
    """A knob added to one file but not the other fails here, not in CI."""

    values_keys = set(_values())
    schema_keys = set(_schema()["properties"])
    assert values_keys == schema_keys, (
        f"values-only={sorted(values_keys - schema_keys)} "
        f"schema-only={sorted(schema_keys - values_keys)}"
    )


def test_every_image_is_pinned_and_never_latest() -> None:
    """Image fields must carry an explicit non-latest tag."""

    values = _values()
    images: list[str] = [
        f"{values['global']['appImage']['repository']}:{values['global']['appImage']['tag']}",
        f"{values['global']['frontendImage']['repository']}:"
        f"{values['global']['frontendImage']['tag']}",
        values["postgres"]["image"],
        values["redis"]["image"],
    ]
    for component in ("prometheus", "alertmanager", "loki", "tempo", "otelCollector", "grafana"):
        images.append(values["observability"][component]["image"])
    assert images
    for image in images:
        assert ":" in image, f"unpinned image reference: {image}"
        assert not image.endswith(":latest"), f"floating latest tag: {image}"


def test_hpa_targets_are_defined_apps() -> None:
    """Autoscaling targets must name entries in .Values.apps."""

    values = _values()
    apps = set(values["apps"])
    for target in values["autoscaling"]["targets"]:
        assert target in apps, f"HPA target {target!r} has no .Values.apps entry"


def test_chart_metadata_matches_repo_conventions() -> None:
    """Chart name/version track the demo; kube floor stays documented."""

    with open(CHART / "Chart.yaml", encoding="utf-8") as handle:
        chart = yaml.safe_load(handle)
    assert chart["name"] == "ai-sre-platform"
    assert chart["apiVersion"] == "v2"
    assert chart["appVersion"] == "0.1.0"


def _compose_default(compose_text: str, name: str) -> str:
    """Extract the `default` from a compose `${NAME:-default}` entry.

    Fails loudly when the entry is missing or carries no default: these
    tunables must stay explicitly defaulted so local runs are reproducible.
    """

    match = re.search(
        rf"^\s*{re.escape(name)}:\s*\$\{{{re.escape(name)}:-([^}}]+)\}}", compose_text, re.MULTILINE
    )
    assert match, f"compose has no defaulted entry for {name}"
    return match.group(1)


# Compose env key -> path of the same knob in values.yaml. Service endpoints
# are deliberately absent: compose uses localhost while the chart must use
# in-cluster DNS (documented divergence in values.yaml comments).
_COMPOSE_TO_VALUES_PARITY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("OTEL_EXPORT_TIMEOUT_SECONDS", ("appDefaults", "otelExportTimeoutSeconds")),
    (
        "OTEL_BATCH_SCHEDULE_DELAY_MILLISECONDS",
        ("appDefaults", "otelBatchScheduleDelayMilliseconds"),
    ),
    ("EVIDENCE_SOURCE_TIMEOUT_SECONDS", ("worker", "evidence", "sourceTimeoutSeconds")),
    ("EVIDENCE_MAX_RESPONSE_BYTES", ("worker", "evidence", "maxResponseBytes")),
    ("EVIDENCE_SLOW_TRACE_THRESHOLD_MS", ("worker", "evidence", "slowTraceThresholdMs")),
    ("EVIDENCE_HTTP_TIMEOUT_SECONDS", ("worker", "evidence", "httpTimeoutSeconds")),
    ("EVIDENCE_MAX_WINDOW_SECONDS", ("worker", "evidence", "maxWindowSeconds")),
    ("EVIDENCE_MAX_LOOKBACK_SECONDS", ("worker", "evidence", "maxLookbackSeconds")),
    ("EVIDENCE_LOG_LIMIT", ("worker", "evidence", "logLimit")),
    ("EVIDENCE_TRACE_LIMIT", ("worker", "evidence", "traceLimit")),
    ("EVIDENCE_CORRELATION_LIMIT", ("worker", "evidence", "correlationLimit")),
    ("INVESTIGATOR_MODEL_TIMEOUT_SECONDS", ("worker", "investigator", "modelTimeoutSeconds")),
    (
        "INVESTIGATOR_MAX_OUTPUT_TOKENS_PER_CALL",
        ("worker", "investigator", "maxOutputTokensPerCall"),
    ),
    ("INVESTIGATOR_MAX_CONTEXT_CHARS", ("worker", "investigator", "maxContextChars")),
    ("INVESTIGATOR_MAX_TOTAL_TOKENS", ("worker", "investigator", "maxTotalTokens")),
    (
        "INVESTIGATOR_INPUT_COST_PER_MILLION_USD",
        ("worker", "investigator", "inputCostPerMillionUsd"),
    ),
    (
        "INVESTIGATOR_OUTPUT_COST_PER_MILLION_USD",
        ("worker", "investigator", "outputCostPerMillionUsd"),
    ),
    (
        "INVESTIGATOR_ROOT_CONFIDENCE_THRESHOLD",
        ("worker", "investigator", "rootConfidenceThreshold"),
    ),
    ("INVESTIGATOR_MIN_COMPETING_HYPOTHESES", ("worker", "investigator", "minCompetingHypotheses")),
    ("KNOWLEDGE_EMBEDDING_MODEL", ("worker", "knowledge", "embeddingModel")),
    ("KNOWLEDGE_EMBEDDING_DIMENSIONS", ("worker", "knowledge", "embeddingDimensions")),
    ("KNOWLEDGE_CHUNK_TOKENS", ("worker", "knowledge", "chunkTokens")),
    ("KNOWLEDGE_CHUNK_OVERLAP_TOKENS", ("worker", "knowledge", "chunkOverlapTokens")),
    ("KNOWLEDGE_TOP_K", ("worker", "knowledge", "topK")),
    ("KNOWLEDGE_MAX_TOP_K", ("worker", "knowledge", "maxTopK")),
    ("KNOWLEDGE_MAX_CONTEXT_CHARS", ("worker", "knowledge", "maxContextChars")),
    ("KNOWLEDGE_EMBEDDING_TIMEOUT_SECONDS", ("worker", "knowledge", "embeddingTimeoutSeconds")),
)


def test_worker_values_keep_compose_parity() -> None:
    """Worker/investigator/knowledge knob *values* must mirror compose defaults.

    Prevents silent drift where the cluster worker runs on code defaults
    while compose runs on tuned values (Stage 11 single-source rule).
    """

    values = _values()
    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert _COMPOSE_TO_VALUES_PARITY, "parity table must not be empty"
    for compose_key, path in _COMPOSE_TO_VALUES_PARITY:
        node: object = values
        for part in path:
            assert isinstance(node, dict) and part in node, f"values missing {'.'.join(path)}"
            node = node[part]
        assert node == _compose_default(compose_text, compose_key), (
            f"{compose_key} default {node!r} != compose"
        )


def test_compose_and_terraform_track_pinned_baseline() -> None:
    """Compose image tag and Terraform PG default must match the chart baseline."""

    compose_text = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "ai-sre-platform:local" in compose_text
    assert "ai-sre-platform:stage05" not in compose_text

    variables_text = (REPO_ROOT / "infrastructure" / "terraform" / "variables.tf").read_text(
        encoding="utf-8"
    )
    assert re.search(r'default\s*=\s*"POSTGRES_17"', variables_text), (
        "cloudsql default must stay on a pgvector-capable POSTGRES_17 baseline"
    )


def test_helm_template_renders_expected_kinds() -> None:
    """Full render smoke test; skips honestly without the helm binary."""

    if shutil.which("helm") is None:
        pytest.skip("helm not on PATH (runbook lists it as a prerequisite)")
    with tempfile.TemporaryDirectory(prefix="ai-sre-test-render-") as tmp:
        proc = subprocess.run(
            [
                "helm",
                "template",
                "ai-sre-test",
                str(CHART),
                "--namespace",
                "ai-sre-test",
                "--output-dir",
                tmp,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr[-2000:]
        kinds: set[str] = set()
        images: set[str] = set()
        for path in Path(tmp).rglob("*.yaml"):
            for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                if not isinstance(doc, dict) or "kind" not in doc:
                    continue
                kinds.add(doc["kind"])
                for container in (
                    doc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
                    or []
                ):
                    if isinstance(container, dict) and "image" in container:
                        images.add(str(container["image"]))
        for expected in (
            "Deployment",
            "StatefulSet",
            "Service",
            "ConfigMap",
            "Secret",
            "ServiceAccount",
            "PersistentVolumeClaim",
            "Job",
            "HorizontalPodAutoscaler",
            "NetworkPolicy",
        ):
            assert expected in kinds, f"rendered chart lacks {expected}"
        assert images
        assert not any(image.endswith(":latest") for image in images)
