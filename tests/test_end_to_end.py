from __future__ import annotations

import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path

import mlflow
import numpy as np
import pandas as pd
import yaml
from mlflow.tracking import MlflowClient
from mlflow.store.model_registry.sqlalchemy_store import (
    SqlAlchemyStore as RegistrySqlAlchemyStore,
)
from mlflow.store.tracking.sqlalchemy_store import SqlAlchemyStore

from firmaware.features import derive_features
from firmaware.predict import predict
from firmaware.schema import ContractViolation, validate
from firmaware.train import load_config, split_by_time, train

from tests.test_schema import make_training_frame


def _config() -> dict[str, object]:
    return {
        "seed": 42,
        "cutoff_quantile": 0.8,
        "cutoff_date": None,
        "cost_ratio_fn_fp": 5,
        "calibrate": False,
        "models": {
            "logistic": {"C": 1.0, "max_iter": 1000},
            "xgboost": {
                "n_estimators": 20,
                "max_depth": 3,
                "learning_rate": 0.1,
            },
        },
        "tuning": {
            "enabled": True,
            "strategy": "grid",
            "champion_selection": "holdout_expected_cost",
            "cv_folds": 2,
            "initial_train_quantile": 0.5,
            "max_candidates": {"logistic": 1, "xgboost": 1},
            "search_spaces": {
                "logistic": {"C": [1.0], "max_iter": [1000]},
                "xgboost": {
                    "n_estimators": [20],
                    "max_depth": [3],
                    "learning_rate": [0.1],
                },
            },
        },
        "mlflow": {
            "tracking_uri": "sqlite:///mlflow.db",
            "artifact_root": "mlruns",
            "experiment_name": "firmaware-tests",
            "run_name": None,
            "register_model": True,
            "registered_model_name": "FirmAwareRiskModel",
            "artifact_path": "model",
            "log_row_level_artifacts": True,
        },
    }


def _dispose_mlflow_engines() -> None:
    for store_class in (SqlAlchemyStore, RegistrySqlAlchemyStore):
        for engine in store_class._engine_map.values():
            engine.dispose()
        store_class._engine_map.clear()


class EndToEndTests(unittest.TestCase):
    def test_strict_time_split_and_empty_side_failure(self) -> None:
        featured = derive_features(
            validate(make_training_frame(20), "training"), "training"
        )
        config = _config()
        train_side, test_side, _ = split_by_time(featured, config)
        self.assertLess(
            train_side["deployment_date"].max(), test_side["deployment_date"].min()
        )

        featured.loc[:, "deployment_date"] = pd.Timestamp("2025-01-01")
        with self.assertRaisesRegex(ContractViolation, "empty train or test"):
            split_by_time(featured, config)

    def test_deterministic_train_append_only_predict_and_no_leakage(self) -> None:
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            stack.callback(_dispose_mlflow_engines)
            root = Path(directory)
            training_path = root / "deployment_events.csv"
            scoring_path = root / "upcoming_deployments.csv"
            config_path = root / "config.yaml"
            artifacts_one = root / "artifacts-one"
            artifacts_two = root / "artifacts-two"
            scores_path = root / "outputs" / "scores.csv"

            training = make_training_frame(180)
            training.to_csv(training_path, index=False)
            scoring = training.iloc[:55].drop(
                columns=[
                    "deployment_outcome",
                    "time_to_failure_hours",
                    "rollback_required",
                ]
            )
            scoring.loc[0, "vendor_name"] = "Moxa"
            scoring.to_csv(scoring_path, index=False)
            config_path.write_text(yaml.safe_dump(_config()), encoding="utf-8")

            first = train(training_path, config_path, artifacts_one)
            second = train(training_path, config_path, artifacts_two)
            self.assertEqual(first["metrics"], second["metrics"])
            self.assertEqual(first["tuning"]["candidate_count"], 2)
            self.assertIsNotNone(
                first["mlflow"]["registered_model_version"]
            )

            feature_list = json.loads(
                (artifacts_one / "feature_list.json").read_text(encoding="utf-8")
            )
            forbidden = {
                "deployment_id",
                "device_id",
                "site_id",
                "firmware_fingerprint",
                "time_to_failure_hours",
                "rollback_required",
                "deployment_date",
            }
            self.assertTrue(forbidden.isdisjoint(feature_list))

            first_scores = predict(
                scoring_path, artifacts_dir=artifacts_one, output_path=scores_path
            )
            time.sleep(0.002)
            second_scores = predict(
                scoring_path, artifacts_dir=artifacts_one, output_path=scores_path
            )
            appended = pd.read_csv(scores_path)
            self.assertEqual(len(first_scores), 55)
            self.assertEqual(len(second_scores), 55)
            self.assertEqual(len(appended), 110)
            self.assertEqual(appended["scored_at"].nunique(), 2)
            self.assertEqual(
                first_scores.loc[0, "unseen_categories"],
                '{"vendor_name":"Moxa"}',
            )
            self.assertEqual(
                int(first_scores["unseen_categories"].ne("{}").sum()), 1
            )

            mlflow.set_tracking_uri(first["mlflow"]["tracking_uri"])
            client = MlflowClient()
            tracked_run = client.get_run(first["mlflow"]["run_id"])
            self.assertIn("test_expected_cost", tracked_run.data.metrics)
            evaluation_artifacts = {
                artifact.path
                for artifact in client.list_artifacts(
                    first["mlflow"]["run_id"], "evaluation"
                )
            }
            self.assertIn(
                "evaluation/evaluation_report.html", evaluation_artifacts
            )
            self.assertIn(
                "evaluation/test_predictions.csv", evaluation_artifacts
            )
            registered_version = client.get_model_version(
                first["mlflow"]["registered_model_name"],
                first["mlflow"]["registered_model_version"],
            )
            self.assertEqual(
                registered_version.run_id, first["mlflow"]["run_id"]
            )

            hosted_model = mlflow.pyfunc.load_model(first["mlflow"]["model_uri"])
            hosted_scores = hosted_model.predict(scoring.iloc[:5])
            pd.testing.assert_frame_equal(
                hosted_scores.reset_index(drop=True),
                first_scores.drop(columns=["scored_at"])
                .iloc[:5]
                .reset_index(drop=True),
                check_dtype=False,
            )
            nullable_scoring = scoring.iloc[:1].copy()
            nullable_scoring.loc[:, "uptime_days"] = np.nan
            nullable_hosted = hosted_model.predict(nullable_scoring)
            self.assertEqual(len(nullable_hosted), 1)

            metadata = json.loads(
                (artifacts_one / "metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(metadata["spec_version"], "1.0")
            self.assertIn(metadata["model_name"], {"logistic", "xgboost"})
            self.assertGreaterEqual(metadata["threshold"], 0.01)
            self.assertLessEqual(metadata["threshold"], 0.99)
            self.assertTrue(
                (artifacts_one / "evaluation" / "roc_curve.csv").is_file()
            )
            self.assertTrue(
                (
                    artifacts_one
                    / "evaluation"
                    / "classification_report.json"
                ).is_file()
            )

    def test_all_config_keys_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            config = _config()
            del config["seed"]
            path.write_text(yaml.safe_dump(config), encoding="utf-8")
            with self.assertRaisesRegex(ContractViolation, "seed"):
                load_config(path)


if __name__ == "__main__":
    unittest.main()
