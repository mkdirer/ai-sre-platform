"""Deterministic eval entrypoint (Stage 09).

Fake suite (CI, offline, no cost):

    uv run python scripts/run_evals.py --dataset v1
    uv run python scripts/run_evals.py --dataset v1-extended --output evals/results

Live suite (gated, bounded, no paid models without approval):

    RUN_LIVE_EVALS=1 EVAL_LIVE_CONFIRM=1 uv run python scripts/run_evals.py --mode live ...

Live mode only activates allowlisted faults, generates bounded traffic,
always attempts cleanup, and refuses to run when the cost budget is exceeded.
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from packages.evals.artifacts import git_commit
from packages.evals.runner import (
    LiveEvalConfig,
    run_fake_dataset_sync,
    run_live_scenario,
    write_fake_dataset_artifacts,
)
from packages.evals.scenario import load_enabled_scenarios


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["fake", "live"], default="fake")
    parser.add_argument("--dataset", default=os.getenv("EVAL_DATASET_VERSION", "v1"))
    parser.add_argument("--scenarios-dir", default="evals/scenarios")
    parser.add_argument("--output", default=os.getenv("EVAL_OUTPUT_DIR", "evals/results"))
    parser.add_argument("--model-config", default="fake")
    parser.add_argument("--live-scenarios", default=os.getenv("EVAL_LIVE_SCENARIOS", "SCN-001"))
    return parser.parse_args()


def _run_fake(args: argparse.Namespace) -> int:
    scenarios_dir = Path(args.scenarios_dir)
    result = run_fake_dataset_sync(scenarios_dir, dataset_version=args.dataset)
    json_path, markdown_path = write_fake_dataset_artifacts(
        result, output_dir=Path(args.output), model_config=args.model_config
    )
    print(
        f"eval dataset={result.summary.dataset_version} "
        f"scenarios={result.summary.scenario_count} "
        f"passed={result.summary.passed_count} "
        f"rca_accuracy={result.summary.root_cause_accuracy:.3f} "
        f"service_accuracy={result.summary.service_accuracy:.3f} "
        f"grounding={result.summary.evidence_grounding_rate:.3f} "
        f"cost_usd={result.summary.total_estimated_cost_usd:.4f}"
    )
    for grade in result.grades:
        print(f"scenario {grade.scenario_id} passed={str(grade.passed).lower()}")
        for note in grade.notes:
            print(f"  note: {note}")
    print(f"artifacts json={json_path} markdown={markdown_path}")
    return 0 if all(grade.passed for grade in result.grades) else 1


def _run_live(args: argparse.Namespace) -> int:
    import asyncio

    if os.getenv("RUN_LIVE_EVALS") != "1" or os.getenv("EVAL_LIVE_CONFIRM") != "1":
        print(
            "live evals require RUN_LIVE_EVALS=1 and EVAL_LIVE_CONFIRM=1; refusing to run",
            file=sys.stderr,
        )
        return 2
    try:
        max_cost = float(os.getenv("EVAL_MAX_COST_USD", "0.0"))
    except ValueError:
        print(
            "live evals require EVAL_MAX_COST_USD to be a number; refusing to run",
            file=sys.stderr,
        )
        return 2
    if max_cost <= 0:
        print(
            "live evals require EVAL_MAX_COST_USD>0 as an explicit cost budget; refusing to run",
            file=sys.stderr,
        )
        return 2
    wanted = {item.strip() for item in args.live_scenarios.split(",") if item.strip()}
    scenarios = [
        item
        for item in load_enabled_scenarios(Path(args.scenarios_dir))
        if item.scenario_id in wanted
    ]
    if not scenarios:
        print(f"no live scenarios selected from {args.live_scenarios}", file=sys.stderr)
        return 2
    config = LiveEvalConfig(
        gateway_url=os.getenv("SMOKE_GATEWAY_URL", "http://127.0.0.1:8001"),
        payment_url=os.getenv("SMOKE_PAYMENT_URL", "http://127.0.0.1:8004"),
        inventory_url=os.getenv("SMOKE_INVENTORY_URL", "http://127.0.0.1:8003"),
        incident_api_url=os.getenv("SCENARIO_INCIDENT_API_URL", "http://127.0.0.1:8006"),
        prometheus_url=os.getenv("SMOKE_PROMETHEUS_URL", "http://127.0.0.1:9090"),
        fault_control_token=os.getenv(
            "SCENARIO_FAULT_CONTROL_TOKEN",
            os.getenv("FAULT_CONTROL_TOKEN", "local-demo-fault-control"),
        ),
        max_cost_usd=max_cost,
    )

    async def _run_all() -> int:
        failures = 0
        artifacts: list[dict[str, object]] = []
        for scenario in scenarios:
            try:
                artifact = await run_live_scenario(scenario, config)
                artifacts.append(artifact)
                traffic = artifact["traffic"]
                print(
                    f"live scenario {scenario.scenario_id} "
                    f"traffic={len(traffic) if isinstance(traffic, list) else '?'} "
                    f"incident={artifact['incident_id']} "
                    f"report={artifact['report_present']} "
                    f"grade={artifact['grade_passed']} "
                    f"cleaned_up={artifact['cleaned_up']}"
                )
                notes = artifact.get("notes", [])
                if isinstance(notes, list):
                    for note in notes:
                        print(f"  note: {note}")
                if artifact.get("grade_passed") is False:
                    failures += 1
            except Exception as error:
                print(f"live scenario {scenario.scenario_id} failed: {error}", file=sys.stderr)
                failures += 1
        output_dir = Path(args.output)
        output_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "dataset_version": args.dataset,
            "schema_version": "1.0",
            "git_commit": git_commit(),
            "model_config": "live",
            "timestamp": datetime.now(UTC).isoformat(),
            "scenario_count": len(artifacts),
            "failures": failures,
            "artifacts": artifacts,
        }
        live_path = output_dir / f"eval-live-{args.dataset}.json"
        live_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        print(f"live artifacts json={live_path}")
        return 1 if failures else 0

    return asyncio.run(_run_all())


def main() -> int:
    """Run the requested eval mode and return a shell-friendly status."""

    args = _parse_args()
    try:
        if args.mode == "live":
            return _run_live(args)
        return _run_fake(args)
    except Exception as error:
        print(f"eval run failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
