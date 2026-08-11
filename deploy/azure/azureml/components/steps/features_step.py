"""Materialise the derived feature table and pass the validated rows through.

Two jobs, and the split between them matters.

The *inspectable* job: `derive_features` is run here and its output written as a
parquet artifact, so the feature table that training will build is visible in the
run, diffable between runs, and usable for drift comparison. It also surfaces the
firmware-parse failure rate, which is a contract signal the schema check cannot
see because a parse failure is not a schema violation.

The *pass-through* job: the raw validated CSV is copied to the output unchanged
and that copy is what the train step consumes. Training deliberately re-derives
its own features from raw rows -- that is the same code path serving uses, and
the parity test in the pipeline spec depends on it. Handing training a
pre-computed matrix would create a second feature path that could drift from the
one the endpoint runs. So this step proves the features are derivable and what
they look like; it does not hand them to the model.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import pandas as pd

from firmaware.features import derive_features, model_inputs
from firmaware.io import read_csv
from firmaware.schema import OUTCOME_COLUMN, validate


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--features", required=True)
    args = parser.parse_args()

    frame = validate(read_csv(args.data), mode="training")
    featured = derive_features(frame, mode="training")
    matrix = model_inputs(featured)

    features_dir = Path(args.features)
    features_dir.mkdir(parents=True, exist_ok=True)
    matrix.to_parquet(features_dir / "features.parquet")

    # A firmware string the parser cannot read is not a schema violation -- the
    # column is a free-text version and any string is well-formed -- so validate
    # cannot catch it. It still degrades the two version features to null, which
    # is exactly the kind of silent quality loss the evaluate gate exists to
    # refuse. Named here so the gate has something concrete to assert on.
    parse_failures = int(matrix["major_version_jump"].isna().sum())
    contract_warnings = []
    if parse_failures:
        contract_warnings.append(
            f"firmware major version unparseable for {parse_failures} of "
            f"{len(matrix)} rows"
        )

    # Counted, not sampled: a reviewer reading the run should be able to see
    # every category the corpus contains without opening the data.
    profile = {
        "row_count": len(featured),
        "feature_columns": sorted(matrix.columns),
        "firmware_parse_failures": parse_failures,
        "contract_warnings": contract_warnings,
        "null_counts": {
            column: int(matrix[column].isna().sum()) for column in matrix.columns
        },
        "outcome_counts": {
            str(key): int(value)
            for key, value in frame[OUTCOME_COLUMN].value_counts().items()
        },
        "label_rate": float(featured["deployment_risk"].mean()),
        "categorical_vocabulary": {
            column: sorted(str(value) for value in matrix[column].dropna().unique())
            for column in matrix.columns
            if not pd.api.types.is_numeric_dtype(matrix[column])
        },
    }
    (features_dir / "feature_profile.json").write_text(
        json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    dataset_path = Path(args.dataset)
    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.data, dataset_path)

    print(f"[features] {profile['row_count']} rows, {len(matrix.columns)} columns")
    print(f"[features] label rate: {profile['label_rate']:.4f}")
    for warning in contract_warnings:
        print(f"[features] contract warning: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
