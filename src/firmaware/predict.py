"""Score through the fitted path and append decisions without erasing history."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import pandas as pd

from .model import SCORE_COLUMNS, score_dataframe
from .io import materialize_artifacts, read_csv, write_scores
from .schema import ContractViolation
from .train import SPEC_VERSION
from .transform import Preprocessor

OUTPUT_COLUMNS = SCORE_COLUMNS + ["scored_at"]


def _load_artifacts(artifacts_dir: Path) -> tuple[Any, Preprocessor, dict[str, Any]]:
    required = [
        artifacts_dir / "model.joblib",
        artifacts_dir / "preprocessor.joblib",
        artifacts_dir / "metadata.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise ContractViolation(
            f"Required artifacts are missing; train first. Missing: {missing}"
        )
    metadata = json.loads(required[2].read_text(encoding="utf-8"))
    if metadata.get("spec_version") != SPEC_VERSION:
        raise ContractViolation(
            "Artifact spec_version mismatch: "
            f"expected {SPEC_VERSION!r}, found {metadata.get('spec_version')!r}"
        )
    model = joblib.load(required[0])
    preprocessor = Preprocessor.load(required[1])
    return model, preprocessor, metadata


def predict(
    input_path: str | Path,
    artifacts_dir: str | Path = "artifacts",
    output_path: str | Path = "outputs/scores.csv",
) -> pd.DataFrame:
    """Use the persisted threshold as the sole source of decisions and bands."""
    with materialize_artifacts(artifacts_dir) as local_artifacts:
        model, preprocessor, metadata = _load_artifacts(local_artifacts)
        raw = read_csv(input_path)
        scores = score_dataframe(raw, model, preprocessor, metadata)
        scored_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        scores["scored_at"] = scored_at
        scores = scores[OUTPUT_COLUMNS]
        run_id = str(
            metadata.get("artifact_run_id")
            or metadata.get("mlflow", {}).get("run_id")
            or metadata["timestamp"]
        )
        written_uri = write_scores(
            scores,
            output_path,
            scored_at,
            run_id,
            OUTPUT_COLUMNS,
        )
        print(f"[predict] scores: {written_uri}")

    decision_counts = scores["risk_prediction"].value_counts().to_dict()
    band_counts = scores["risk_band"].value_counts().to_dict()
    print(
        f"[predict] decisions: GO={decision_counts.get('GO', 0)}, "
        f"NO_GO={decision_counts.get('NO_GO', 0)}"
    )
    print(
        f"[predict] bands: HIGH={band_counts.get('HIGH', 0)}, "
        f"MEDIUM={band_counts.get('MEDIUM', 0)}, LOW={band_counts.get('LOW', 0)}"
    )
    unseen_rows = scores.loc[scores["unseen_categories"].ne("{}")]
    if not unseen_rows.empty:
        details = "; ".join(
            f"{row.deployment_id}: "
            f"{row.unseen_categories}"
            for row in unseen_rows.itertuples(index=False)
        )
        print(f"[predict] WARNING: UNSEEN CATEGORIES: {details}")
    return scores
