"""Enforce input vocabulary and types before they can affect the model."""

from __future__ import annotations

from typing import Literal

import pandas as pd

ID_COLUMNS = [
    "deployment_id",
    "device_id",
    "site_id",
    "firmware_fingerprint",
]
LEAKAGE_COLUMNS = ["time_to_failure_hours", "rollback_required"]
CATEGORICAL_COLUMNS = [
    "vendor_name",
    "device_type",
    "hardware_series",
    "fleet_tier",
    "site_criticality",
    "deployment_type",
]
NUMERIC_COLUMNS = [
    "version_jump_magnitude",
    "kernel_touched",
    "bootloader_touched",
    "protocol_mismatch_flag",
    "maintenance_window",
    "network_stress_score",
    "error_rate_predeploy",
    "uptime_days",
    "past_failure_count",
    "firmware_release_age_days",
    "cve_count",
    "max_cvss_score",
    "cross_vendor_dependency_count",
    "dependent_device_count",
]
DATE_COLUMN = "deployment_date"
OUTCOME_COLUMN = "deployment_outcome"
ALLOWED_OUTCOMES = {"SUCCESS", "DEGRADED", "ROLLBACK", "FAILED"}
VERSION_COLUMNS = ["current_firmware", "target_firmware"]

TRAINING_COLUMNS = (
    ID_COLUMNS
    + CATEGORICAL_COLUMNS
    + VERSION_COLUMNS
    + NUMERIC_COLUMNS
    + [DATE_COLUMN, OUTCOME_COLUMN]
    + LEAKAGE_COLUMNS
)
SCORING_COLUMNS = [
    column
    for column in TRAINING_COLUMNS
    if column not in {OUTCOME_COLUMN, *LEAKAGE_COLUMNS}
]


class ContractViolation(ValueError):
    """Identify data errors that must stop the pipeline before modeling."""


def _display_values(series: pd.Series) -> list[str]:
    values = series.astype("string").fillna("<missing>").unique().tolist()
    return sorted(str(value) for value in values)


def validate(
    df: pd.DataFrame, mode: Literal["training", "scoring"]
) -> pd.DataFrame:
    """Return a validated copy so numeric and date coercion is shared by every caller."""
    if mode not in {"training", "scoring"}:
        raise ValueError(f"Unsupported validation mode: {mode!r}")

    expected = TRAINING_COLUMNS if mode == "training" else SCORING_COLUMNS
    missing = sorted(set(expected) - set(df.columns))
    if missing:
        raise ContractViolation(f"Missing required columns: {missing}")

    extras = sorted(set(df.columns) - set(expected))
    if extras:
        print(f"[validate] WARNING: unexpected columns will be ignored: {extras}")

    validated = df.copy()

    duplicate_mask = validated["deployment_id"].duplicated(keep=False)
    if duplicate_mask.any():
        duplicate_values = _display_values(
            validated.loc[duplicate_mask, "deployment_id"]
        )
        raise ContractViolation(
            f"Duplicate deployment_id values: {duplicate_values}"
        )

    if mode == "training":
        invalid_outcome_mask = ~validated[OUTCOME_COLUMN].isin(ALLOWED_OUTCOMES)
        if invalid_outcome_mask.any():
            values = _display_values(validated.loc[invalid_outcome_mask, OUTCOME_COLUMN])
            raise ContractViolation(
                f"Invalid deployment_outcome values: {values}; "
                f"allowed values are {sorted(ALLOWED_OUTCOMES)}"
            )

        parsed_dates = pd.to_datetime(validated[DATE_COLUMN], errors="coerce")
        invalid_date_mask = parsed_dates.isna()
        if invalid_date_mask.any():
            values = _display_values(validated.loc[invalid_date_mask, DATE_COLUMN])
            raise ContractViolation(
                f"Unparseable deployment_date values: {values}"
            )
        validated[DATE_COLUMN] = parsed_dates

    invalid_numeric: dict[str, list[str]] = {}
    nan_counts: dict[str, int] = {}
    for column in NUMERIC_COLUMNS:
        original = validated[column]
        coerced = pd.to_numeric(original, errors="coerce")
        invalid_mask = original.notna() & coerced.isna()
        if invalid_mask.any():
            invalid_numeric[column] = _display_values(original.loc[invalid_mask])
        validated[column] = coerced
        nan_counts[column] = int(coerced.isna().sum())

    if invalid_numeric:
        details = "; ".join(
            f"{column}={values}" for column, values in invalid_numeric.items()
        )
        raise ContractViolation(f"Non-numeric values in numeric columns: {details}")

    print("[validate] numeric NaN counts:")
    for column, count in nan_counts.items():
        print(f"[validate]   {column}: {count}")

    return validated

