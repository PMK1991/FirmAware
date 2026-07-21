"""Derive signed version and interaction features before removing unsafe columns."""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd

from .schema import (
    DATE_COLUMN,
    ID_COLUMNS,
    LEAKAGE_COLUMNS,
    OUTCOME_COLUMN,
    ContractViolation,
)

LABEL_COLUMN = "deployment_risk"
DERIVED_NUMERIC_COLUMNS = [
    "major_version_jump",
    "major_version_changed",
    "emergency_no_maintenance",
    "core_system_touched",
]


def deployment_risk(outcomes: pd.Series) -> pd.Series:
    """Treat every validated non-success outcome as risky to avoid vocabulary coupling."""
    return outcomes.ne("SUCCESS").astype(int)


def _major(value: object) -> int | None:
    if pd.isna(value):
        return None
    try:
        return int(str(value).split(".", maxsplit=1)[0])
    except (TypeError, ValueError):
        return None


def derive_features(
    df: pd.DataFrame, mode: Literal["training", "scoring"]
) -> pd.DataFrame:
    """Keep version jumps signed because firmware downgrades carry useful signal."""
    if mode not in {"training", "scoring"}:
        raise ValueError(f"Unsupported feature mode: {mode!r}")

    featured = df.copy()
    current_major = featured["current_firmware"].map(_major)
    target_major = featured["target_firmware"].map(_major)
    parse_failure_mask = current_major.isna() | target_major.isna()
    failure_count = int(parse_failure_mask.sum())
    row_count = len(featured)
    print(
        f"[features] firmware major parse failures: {failure_count}/{row_count}"
    )
    if mode == "training" and row_count and failure_count / row_count > 0.01:
        bad_ids = featured.loc[parse_failure_mask, "deployment_id"].astype(str).tolist()
        raise ContractViolation(
            "Firmware major version parsing failed for more than 1% of training "
            f"rows ({failure_count}/{row_count}); deployment_id values: {bad_ids}"
        )

    featured["major_version_jump"] = target_major - current_major
    featured["major_version_changed"] = np.where(
        featured["major_version_jump"].isna(),
        np.nan,
        featured["major_version_jump"].ne(0).astype(int),
    )
    featured["emergency_no_maintenance"] = (
        featured["deployment_type"].eq("EMERGENCY")
        & featured["maintenance_window"].eq(0)
    ).astype(int)
    featured["core_system_touched"] = (
        featured["kernel_touched"].fillna(0).ne(0)
        | featured["bootloader_touched"].fillna(0).ne(0)
    ).astype(int)

    if OUTCOME_COLUMN in featured.columns:
        featured[LABEL_COLUMN] = deployment_risk(featured[OUTCOME_COLUMN])

    deployment_ids = featured["deployment_id"].copy()
    featured = featured.drop(
        columns=ID_COLUMNS + LEAKAGE_COLUMNS + ["current_firmware", "target_firmware"],
        errors="ignore",
    )
    featured.index = pd.Index(deployment_ids, name="deployment_id")
    return featured


def model_inputs(featured: pd.DataFrame) -> pd.DataFrame:
    """Exclude split and label columns from both training and scoring matrices."""
    return featured.drop(
        columns=[DATE_COLUMN, OUTCOME_COLUMN, LABEL_COLUMN], errors="ignore"
    )

