from __future__ import annotations

import unittest

from firmaware.features import LABEL_COLUMN, derive_features, deployment_risk
from firmaware.schema import ContractViolation, validate

from tests.test_schema import make_training_frame


class FeatureTests(unittest.TestCase):
    def test_label_is_agnostic_after_contract_validation(self) -> None:
        frame = make_training_frame(4)
        validated = validate(frame, mode="training")
        labels = deployment_risk(validated["deployment_outcome"])
        self.assertEqual(labels.tolist(), [0, 1, 0, 1])

        frame.loc[0, "deployment_outcome"] = "NEW_FAILURE_TYPE"
        with self.assertRaisesRegex(ContractViolation, "NEW_FAILURE_TYPE"):
            validate(frame, mode="training")

    def test_signed_version_jump_and_interactions(self) -> None:
        frame = make_training_frame(4)
        frame.loc[0, ["current_firmware", "target_firmware"]] = ["4.2.0", "2.0.0"]
        frame.loc[0, ["deployment_type", "maintenance_window"]] = ["EMERGENCY", 0]
        frame.loc[0, ["kernel_touched", "bootloader_touched"]] = [0, 1]
        featured = derive_features(validate(frame, "training"), "training")

        deployment = featured.loc["deployment-0000"]
        self.assertEqual(deployment["major_version_jump"], -2)
        self.assertEqual(deployment["major_version_changed"], 1)
        self.assertEqual(deployment["emergency_no_maintenance"], 1)
        self.assertEqual(deployment["core_system_touched"], 1)
        self.assertIn(LABEL_COLUMN, featured.columns)
        self.assertNotIn("current_firmware", featured.columns)
        self.assertNotIn("device_id", featured.columns)

    def test_more_than_one_percent_version_parse_failures_hard_fail(self) -> None:
        frame = make_training_frame(100)
        frame.loc[[0, 1], "target_firmware"] = "not-semver"
        with self.assertRaisesRegex(ContractViolation, "more than 1%"):
            derive_features(validate(frame, "training"), "training")


if __name__ == "__main__":
    unittest.main()

