from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from firmaware.cli import main
from firmaware.schema import ContractViolation, validate


def make_training_frame(rows: int = 120) -> pd.DataFrame:
    indexes = np.arange(rows)
    outcomes = np.array(["SUCCESS", "DEGRADED", "SUCCESS", "ROLLBACK"])
    vendors = np.array(["Cisco", "Juniper", "Arista", "Nokia"])
    device_types = np.array(
        ["ROUTER", "SWITCH", "FIREWALL", "GATEWAY", "SENSOR", "CONTROLLER"]
    )
    return pd.DataFrame(
        {
            "deployment_id": [f"deployment-{index:04d}" for index in indexes],
            "device_id": [f"device-{index:04d}" for index in indexes],
            "site_id": [f"site-{index % 12:02d}" for index in indexes],
            "firmware_fingerprint": [f"fingerprint-{index:04d}" for index in indexes],
            "vendor_name": vendors[indexes % len(vendors)],
            "device_type": device_types[indexes % len(device_types)],
            "hardware_series": [f"series-{index % 16:02d}" for index in indexes],
            "fleet_tier": np.array(["TIER_1", "TIER_2", "TIER_3"])[indexes % 3],
            "site_criticality": np.array(["LOW", "MEDIUM", "HIGH"])[indexes % 3],
            "deployment_type": np.array(["STANDARD", "PLANNED", "EMERGENCY"])[
                indexes % 3
            ],
            "current_firmware": [f"{1 + index % 3}.2.0" for index in indexes],
            "target_firmware": [f"{1 + (index + 1) % 4}.0.1" for index in indexes],
            "version_jump_magnitude": (indexes % 5).astype(float),
            "kernel_touched": indexes % 2,
            "bootloader_touched": (indexes // 2) % 2,
            "protocol_mismatch_flag": (indexes // 3) % 2,
            "maintenance_window": (indexes + 1) % 2,
            "network_stress_score": (indexes % 10) / 10,
            "error_rate_predeploy": (indexes % 7) / 100,
            "uptime_days": 30 + indexes,
            "past_failure_count": indexes % 4,
            "firmware_release_age_days": 10 + indexes % 60,
            "cve_count": indexes % 6,
            "max_cvss_score": (indexes % 10).astype(float),
            "cross_vendor_dependency_count": indexes % 3,
            "dependent_device_count": 1 + indexes % 20,
            "deployment_date": pd.date_range("2025-01-01", periods=rows, freq="D")
            .strftime("%Y-%m-%d")
            .tolist(),
            "deployment_outcome": outcomes[indexes % len(outcomes)],
            "time_to_failure_hours": (indexes % 48).astype(float),
            "rollback_required": (indexes % 4 == 3).astype(int),
        }
    )


class SchemaTests(unittest.TestCase):
    def test_valid_training_data_is_coerced(self) -> None:
        frame = make_training_frame(8)
        frame.loc[0, "network_stress_score"] = np.nan
        validated = validate(frame, mode="training")
        self.assertTrue(pd.api.types.is_datetime64_any_dtype(validated["deployment_date"]))
        self.assertEqual(int(validated["network_stress_score"].isna().sum()), 1)

    def test_vocabulary_drift_fails_and_names_value(self) -> None:
        frame = make_training_frame(8)
        frame.loc[0, "deployment_outcome"] = "ROLLBACK_REQUIRED"
        with self.assertRaisesRegex(ContractViolation, "ROLLBACK_REQUIRED"):
            validate(frame, mode="training")

    def test_missing_date_fails_and_cli_returns_contract_exit_code(self) -> None:
        frame = make_training_frame(8).drop(columns=["deployment_date"])
        with self.assertRaisesRegex(ContractViolation, "deployment_date"):
            validate(frame, mode="training")

        path = self.enterContext(_temporary_csv(frame))
        self.assertEqual(main(["validate", "--input", path, "--mode", "training"]), 1)

    def test_duplicate_id_and_non_numeric_value_fail(self) -> None:
        duplicate = make_training_frame(8)
        duplicate.loc[1, "deployment_id"] = duplicate.loc[0, "deployment_id"]
        with self.assertRaisesRegex(ContractViolation, "Duplicate deployment_id"):
            validate(duplicate, mode="training")

        non_numeric = make_training_frame(8)
        non_numeric.loc[0, "uptime_days"] = "not-a-number"
        with self.assertRaisesRegex(ContractViolation, "not-a-number"):
            validate(non_numeric, mode="training")


class _temporary_csv:
    def __init__(self, frame: pd.DataFrame) -> None:
        import tempfile

        self.frame = frame
        self.directory = tempfile.TemporaryDirectory()

    def __enter__(self) -> str:
        from pathlib import Path

        self.path = Path(self.directory.name) / "input.csv"
        self.frame.to_csv(self.path, index=False)
        return str(self.path)

    def __exit__(self, *args: object) -> None:
        self.directory.cleanup()


if __name__ == "__main__":
    unittest.main()

