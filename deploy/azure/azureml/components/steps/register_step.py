"""Register the gated model and tag the version with its full provenance.

This step only ever runs when the gate passed, because a failed gate exits
non-zero and the pipeline is configured with `continue_on_step_failure: false`.

Registration is where provenance stops being implicit. A registry version on its
own tells you a model exists; these tags tell you which commit produced it, which
image ran it, which version of the data it saw, what decision threshold it was
tuned to, what the cost asymmetry behind that threshold was, and every metric it
scored on the untouched test split. The rollback and audit paths in the
architecture all read these tags -- without them, "promote what was tested" is a
claim rather than a check.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import mlflow
from mlflow import MlflowClient


def _tag_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--evaluation", required=True)
    parser.add_argument("--registration", required=True)
    args = parser.parse_args()

    gate = json.loads(
        (Path(args.evaluation) / "gate.json").read_text(encoding="utf-8")
    )
    if not gate["passed"]:
        # Defence in depth. The pipeline should never route here on a failed
        # gate, but a registry write is irreversible enough to check twice.
        print("[register] gate.json reports failure; refusing to register")
        return 1

    metadata = json.loads(
        (Path(args.model_dir) / "metadata.json").read_text(encoding="utf-8")
    )
    model_name = gate["registered_model_name"]
    model_uri = gate["model_uri"]

    version = mlflow.register_model(model_uri=model_uri, name=model_name)
    client = MlflowClient()

    tags = {
        "git_sha": os.getenv("GIT_SHA", "unknown"),
        "image_digest": os.getenv("IMAGE_DIGEST", "unknown"),
        "data_asset_version": os.getenv("DATA_ASSET_VERSION", "unknown"),
        "pipeline_run_id": os.getenv("AZUREML_ROOT_RUN_ID", "unknown"),
        "training_run_id": metadata["mlflow"]["run_id"],
        "threshold": _tag_value(metadata["threshold"]),
        "cost_ratio_fn_fp": _tag_value(metadata["cost_ratio_fn_fp"]),
        "model_family": metadata["model_name"],
        "seed": _tag_value(metadata["seed"]),
        "train_size": _tag_value(metadata["train_size"]),
        "test_size": _tag_value(metadata["test_size"]),
        "test_start": metadata["test_date_range"]["start"],
        "test_end": metadata["test_date_range"]["end"],
        "spec_version": metadata["spec_version"],
    }
    # Every test metric, not a chosen few: the evaluate gate reads test_roc_auc
    # back off the incumbent version, so the tag set has to be complete enough to
    # compare any future run against this one.
    for key, value in gate["test_metrics"].items():
        tags[f"test_{key}"] = _tag_value(value)

    for key, value in tags.items():
        client.set_model_version_tag(model_name, version.version, key, value)

    client.update_model_version(
        name=model_name,
        version=version.version,
        description=(
            f"{metadata['model_name']} at threshold {metadata['threshold']}, "
            f"ROC AUC {gate['test_metrics']['roc_auc']:.4f}, "
            f"recall {gate['test_metrics']['recall']:.4f} "
            f"on {metadata['test_size']} untouched test rows "
            f"({metadata['test_date_range']['start']} to "
            f"{metadata['test_date_range']['end']})."
        ),
    )

    registration_dir = Path(args.registration)
    registration_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "registered_model_name": model_name,
        "registered_model_version": str(version.version),
        "registered_model_uri": f"models:/{model_name}/{version.version}",
        "source_model_uri": model_uri,
        "tags": tags,
    }
    (registration_dir / "registration.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    print(f"[register] {record['registered_model_uri']}")
    print(f"[register] git_sha={tags['git_sha']} image_digest={tags['image_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
