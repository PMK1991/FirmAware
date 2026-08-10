"""Decide whether this model is allowed to reach the registry.

The gate is the only thing standing between a training run and a production
model, so it is deliberately blunt and deliberately fails closed:

  1. ROC AUC on the untouched test split must be at least the incumbent's AUC
     minus a tolerance. The incumbent is read from the registry, not from a
     hardcoded number, so the bar rises as the model improves and no one has to
     remember to raise it. With an empty registry the configured floor applies.
  2. Recall at the selected threshold must clear the target. This model exists to
     catch risky firmware deployments; a model that is accurate by declining to
     flag anything is worse than useless, and expected-cost tuning alone will not
     always prevent that.
  3. Zero contract warnings from the feature step. A run whose inputs were
     partially unparseable can still produce good-looking metrics.

Any failure exits non-zero, which stops the pipeline before `register` and leaves
the registry untouched. That is acceptance criterion 5, and it is enforced here
rather than by a human reading a report.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _incumbent_auc(model_name: str) -> tuple[float | None, str | None]:
    """Read the best AUC already in the registry, tolerating an empty one."""
    try:
        from mlflow import MlflowClient
    except ImportError:
        return None, None

    try:
        client = MlflowClient()
        versions = client.search_model_versions(f"name='{model_name}'")
    except Exception as error:  # noqa: BLE001 - registry absence must not block
        print(f"[evaluate] registry unavailable, using floor: {error}")
        return None, None

    best_auc: float | None = None
    best_version: str | None = None
    for version in versions:
        raw = (version.tags or {}).get("test_roc_auc")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if best_auc is None or value > best_auc:
            best_auc, best_version = value, str(version.version)
    return best_auc, best_version


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--features", required=True)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--auc-tolerance", type=float, default=0.02)
    parser.add_argument("--auc-floor", type=float, default=0.65)
    parser.add_argument("--min-recall", type=float, default=0.90)
    args = parser.parse_args()

    model_dir = Path(args.model_dir)
    metadata = _load_json(model_dir / "metadata.json")
    champion = metadata["model_name"]
    metrics = metadata["metrics"][champion]
    profile = _load_json(Path(args.features) / "feature_profile.json")

    model_name = os.getenv("FIRMAWARE_REGISTERED_MODEL_NAME", "FirmAwareRiskModel")
    incumbent_auc, incumbent_version = _incumbent_auc(model_name)
    baseline = args.auc_floor if incumbent_auc is None else incumbent_auc
    required_auc = baseline - args.auc_tolerance

    warnings = list(profile.get("contract_warnings", []))
    checks = [
        {
            "name": "roc_auc_not_regressed",
            "passed": bool(metrics["roc_auc"] >= required_auc),
            "observed": float(metrics["roc_auc"]),
            "required": float(required_auc),
            "basis": (
                f"registry version {incumbent_version}"
                if incumbent_version
                else "configured floor (registry empty)"
            ),
        },
        {
            "name": "recall_at_threshold",
            "passed": bool(metrics["recall"] >= args.min_recall),
            "observed": float(metrics["recall"]),
            "required": float(args.min_recall),
            "basis": f"threshold {metadata['threshold']}",
        },
        {
            "name": "zero_contract_warnings",
            "passed": not warnings,
            "observed": warnings,
            "required": [],
            "basis": "feature_profile.json",
        },
    ]

    passed = all(check["passed"] for check in checks)
    gate = {
        "passed": passed,
        "checks": checks,
        "model_name": champion,
        "threshold": metadata["threshold"],
        "cost_ratio_fn_fp": metadata["cost_ratio_fn_fp"],
        "test_metrics": metrics,
        "run_id": metadata["mlflow"]["run_id"],
        "model_uri": metadata["mlflow"]["model_uri"],
        "registered_model_name": model_name,
        "test_size": metadata["test_size"],
        "test_date_range": metadata["test_date_range"],
    }

    evaluation_dir = Path(args.evaluation)
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    # Carry the training run's own evaluation artifacts forward so the gate
    # verdict and the evidence behind it live in one place.
    source = model_dir / "evaluation"
    if source.is_dir():
        shutil.copytree(source, evaluation_dir / "evaluation", dirs_exist_ok=True)
    (evaluation_dir / "gate.json").write_text(
        json.dumps(gate, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    for check in checks:
        state = "PASS" if check["passed"] else "FAIL"
        print(
            f"[evaluate] {state} {check['name']}: "
            f"observed={check['observed']} required={check['required']} "
            f"({check['basis']})"
        )

    if not passed:
        print("[evaluate] gate failed; nothing will be registered")
        return 1
    print(f"[evaluate] gate passed for {champion} run {gate['run_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
