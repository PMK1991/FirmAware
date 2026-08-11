from __future__ import annotations

import errno
import gc
import importlib.util
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from unittest import mock

import mlflow
import numpy as np
import pandas as pd
import yaml
from mlflow.exceptions import MlflowException
from mlflow.tracking import MlflowClient
from sqlalchemy.engine import Engine

from firmaware.features import derive_features
from firmaware.predict import predict
from firmaware.schema import NUMERIC_COLUMNS, ContractViolation, validate
from firmaware.train import _promote_artifacts, load_config, split_by_time, train
from tests.test_schema import make_training_frame


def _as_served(frame: pd.DataFrame) -> pd.DataFrame:
    """Cast scoring numerics to the double the serving signature declares.

    Nullable numerics force every numeric ColSpec to double, and MLflow refuses
    to cast int64 to double, so integer-valued columns have to be sent as
    doubles. A raw frame types them int64 on Linux and int32 on Windows, and
    int32 does convert, so skipping this passes locally and fails in CI.
    """
    return frame.astype({column: "float64" for column in NUMERIC_COLUMNS})


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


def _batch_score_module(model: Any) -> Any:
    """Load the real batch entry script and point it at a loaded model.

    Imported from its path rather than reimplemented, so the assertion covers
    the file that actually ships. `batch_score.py` reads `_MODEL` at call time
    and imports nothing from the package at module scope, so binding it here is
    enough to exercise `_as_served` exactly as the batch driver would.
    """
    location = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "azure"
        / "azureml"
        / "batch_score.py"
    )
    spec = importlib.util.spec_from_file_location("firmaware_batch_score", location)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._MODEL = model
    return module


def _dispose_mlflow_engines() -> None:
    """Release the SQLite handles MLflow's SQLAlchemy pools hold open.

    Windows refuses to unlink an open file, so without this the temporary
    directory containing mlflow.db cannot be removed and the test fails during
    teardown having already passed every assertion.

    Engines are found by sweeping the garbage collector rather than by reaching
    into MLflow, because every private handle here moved between majors: the
    tracking store's cache is `_engine_map` in MLflow 3 and
    `_db_uri_sql_alchemy_engine_map` in 2.x, the registry store keeps no
    class-level cache at all in 2.x, and `_dispose_engine` is an instance
    method needing a store object this code never sees. Naming any of them
    means the helper silently stops disposing anything the moment the pin
    moves, which is exactly how it broke. Disposing is safe even for an engine
    still cached: SQLAlchemy replaces the pool and reconnects on next use.
    """
    for candidate in gc.get_objects():
        if isinstance(candidate, Engine):
            candidate.dispose()


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
            hosted_scores = hosted_model.predict(_as_served(scoring.iloc[:5]))
            pd.testing.assert_frame_equal(
                hosted_scores.reset_index(drop=True),
                first_scores.drop(columns=["scored_at"])
                .iloc[:5]
                .reset_index(drop=True),
                check_dtype=False,
            )
            nullable_scoring = _as_served(scoring.iloc[:1].copy())
            nullable_scoring.loc[:, "uptime_days"] = np.nan
            nullable_hosted = hosted_model.predict(nullable_scoring)
            self.assertEqual(len(nullable_hosted), 1)

            # The batch entry point reads CSV, so its numerics arrive as int64
            # and MLflow refuses to widen int64 to the signature's double. Every
            # other assertion here pre-casts through _as_served and so cannot see
            # this; it took a real batch job to surface it. Assert both halves:
            # that the raw frame is genuinely rejected, and that the entry
            # script's own coercion is what makes it acceptable.
            as_read_from_csv = scoring.iloc[:5].copy()
            for column in ("uptime_days", "past_failure_count", "cve_count"):
                as_read_from_csv[column] = (
                    as_read_from_csv[column].astype("float64").round().astype("int64")
                )
            with self.assertRaises(MlflowException) as refused:
                hosted_model.predict(as_read_from_csv)
            self.assertIn("int64", str(refused.exception))

            coerced = hosted_model.predict(
                _batch_score_module(hosted_model)._as_served(as_read_from_csv)
            )
            self.assertEqual(len(coerced), 5)
            self.assertEqual(
                list(coerced["risk_prediction"]),
                list(hosted_scores["risk_prediction"]),
            )

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


class PromoteArtifactsTests(unittest.TestCase):
    """The artifact promotion has to work on a mount as well as a plain dir."""

    def test_promotion_swaps_directories_when_rename_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "model_dir"
            target.mkdir()
            (target / "stale.json").write_text("old", encoding="utf-8")
            staging = root / ".model_dir-run1.staging"
            staging.mkdir()
            (staging / "model.json").write_text("new", encoding="utf-8")

            _promote_artifacts(staging, target, "run1")

            self.assertEqual(
                (target / "model.json").read_text(encoding="utf-8"), "new"
            )
            self.assertFalse((target / "stale.json").exists())
            self.assertFalse(staging.exists())

    def test_promotion_writes_through_a_target_that_cannot_be_renamed(
        self,
    ) -> None:
        """An Azure ML pipeline output is a FUSE mount.

        Renaming a mount point fails with EBUSY whatever the permissions, so the
        atomic swap is simply unavailable there and the contents have to be
        written through the mount instead. Before this was handled, training
        completed and then died publishing its own output.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "model_dir"
            target.mkdir()
            (target / "stale.json").write_text("old", encoding="utf-8")
            staging = root / ".model_dir-run1.staging"
            staging.mkdir()
            (staging / "model.json").write_text("new", encoding="utf-8")
            (staging / "nested").mkdir()
            (staging / "nested" / "metrics.json").write_text(
                "{}", encoding="utf-8"
            )

            original_replace = Path.replace

            def refuse_to_rename_the_mount(
                self: Path, destination: Any
            ) -> Any:
                if self == target:
                    raise OSError(errno.EBUSY, "Device or resource busy")
                return original_replace(self, destination)

            with mock.patch.object(
                Path, "replace", refuse_to_rename_the_mount
            ):
                _promote_artifacts(staging, target, "run1")

            # The same directory object survives; only its contents changed.
            self.assertTrue(target.is_dir())
            self.assertEqual(
                (target / "model.json").read_text(encoding="utf-8"), "new"
            )
            self.assertEqual(
                (target / "nested" / "metrics.json").read_text(
                    encoding="utf-8"
                ),
                "{}",
            )
            self.assertFalse((target / "stale.json").exists())
            self.assertFalse(staging.exists())

    def test_promotion_still_raises_on_an_unexpected_os_error(self) -> None:
        """Only EBUSY and EXDEV mean "write through"; nothing else is masked."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "model_dir"
            target.mkdir()
            staging = root / ".model_dir-run1.staging"
            staging.mkdir()
            (staging / "model.json").write_text("new", encoding="utf-8")

            def refuse_with_permission_denied(self: Path, destination: Any) -> Any:
                raise OSError(errno.EACCES, "Permission denied")

            with (
                mock.patch.object(Path, "replace", refuse_with_permission_denied),
                self.assertRaises(OSError),
            ):
                _promote_artifacts(staging, target, "run1")


if __name__ == "__main__":
    unittest.main()
