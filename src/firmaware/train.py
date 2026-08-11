"""Tune chronologically, evaluate once, and publish the measured MLflow champion."""

from __future__ import annotations

import errno
import importlib.metadata
import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import mlflow
import numpy as np
import pandas as pd
import yaml
from sklearn.isotonic import IsotonicRegression
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import ParameterGrid, ParameterSampler
from xgboost import XGBClassifier

from .evaluation import (
    classification_details,
    curve_frames,
    evaluate_binary,
    feature_importance,
    select_threshold,
    threshold_sweep,
)
from .features import LABEL_COLUMN, derive_features, model_inputs
from .io import (
    is_gcs_uri,
    join_uri,
    publish_artifact_run,
    read_csv,
)
from .model import FirmAwarePyFuncModel, score_dataframe, serving_signature
from .schema import (
    CATEGORICAL_COLUMNS,
    DATE_COLUMN,
    NUMERIC_COLUMNS,
    SCORING_COLUMNS,
    ContractViolation,
    validate,
)
from .tracking import configure_tracking
from .transform import Preprocessor

SPEC_VERSION = "1.0"
REQUIRED_CONFIG_KEYS = {
    "seed",
    "cutoff_quantile",
    "cutoff_date",
    "cost_ratio_fn_fp",
    "calibrate",
    "models",
    "tuning",
    "mlflow",
}
REQUIRED_MODEL_KEYS = {
    "logistic": {"C", "max_iter"},
    "xgboost": {"n_estimators", "max_depth", "learning_rate"},
}
REQUIRED_TUNING_KEYS = {
    "enabled",
    "strategy",
    "champion_selection",
    "cv_folds",
    "initial_train_quantile",
    "max_candidates",
    "search_spaces",
}
REQUIRED_MLFLOW_KEYS = {
    "tracking_uri",
    "artifact_root",
    "experiment_name",
    "run_name",
    "register_model",
    "registered_model_name",
    "artifact_path",
    "log_row_level_artifacts",
}


@dataclass
class IsotonicCalibratedModel:
    """Map held-out validation probabilities without refitting the measured model."""

    base_model: Any
    calibrator: IsotonicRegression

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        raw = self.base_model.predict_proba(X)[:, 1]
        calibrated = np.asarray(self.calibrator.predict(raw), dtype=float)
        return np.column_stack([1.0 - calibrated, calibrated])


@dataclass
class PreparedTuningFold:
    number: int
    cutoff: pd.Timestamp
    train_matrix: pd.DataFrame
    train_labels: pd.Series
    validation_matrix: pd.DataFrame
    validation_labels: np.ndarray
    train_size: int
    validation_size: int
    train_date_range: dict[str, str]
    validation_date_range: dict[str, str]


def _require_mapping(
    parent: dict[str, Any], key: str, required: set[str]
) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ContractViolation(f"Config key {key!r} must be a mapping")
    missing = sorted(required - set(value))
    if missing:
        raise ContractViolation(f"Config {key} is missing required keys: {missing}")
    return value


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ContractViolation("Config must contain a YAML mapping")

    missing = sorted(REQUIRED_CONFIG_KEYS - set(config))
    if missing:
        raise ContractViolation(f"Config is missing required keys: {missing}")

    models = _require_mapping(config, "models", set(REQUIRED_MODEL_KEYS))
    for model_name, required_keys in REQUIRED_MODEL_KEYS.items():
        model_config = _require_mapping(models, model_name, required_keys)
        if not all(
            isinstance(model_config[key], (int, float)) for key in required_keys
        ):
            raise ContractViolation(
                f"Config model {model_name} parameters must be numeric"
            )

    tuning = _require_mapping(config, "tuning", REQUIRED_TUNING_KEYS)
    if not isinstance(tuning["enabled"], bool):
        raise ContractViolation("tuning.enabled must be true or false")
    if tuning["strategy"] not in {"grid", "randomized"}:
        raise ContractViolation("tuning.strategy must be 'grid' or 'randomized'")
    if tuning["champion_selection"] not in {
        "holdout_expected_cost",
        "cv_expected_cost",
    }:
        raise ContractViolation(
            "tuning.champion_selection must be 'holdout_expected_cost' "
            "or 'cv_expected_cost'"
        )
    if not isinstance(tuning["cv_folds"], int) or tuning["cv_folds"] < 2:
        raise ContractViolation("tuning.cv_folds must be an integer of at least 2")
    initial_train_quantile = tuning["initial_train_quantile"]
    if (
        not isinstance(initial_train_quantile, (int, float))
        or not 0 < float(initial_train_quantile) < 1
    ):
        raise ContractViolation(
            "tuning.initial_train_quantile must be between 0 and 1"
        )
    max_candidates = _require_mapping(
        tuning, "max_candidates", set(REQUIRED_MODEL_KEYS)
    )
    spaces = _require_mapping(tuning, "search_spaces", set(REQUIRED_MODEL_KEYS))
    for model_name, parameter_names in REQUIRED_MODEL_KEYS.items():
        candidate_limit = max_candidates[model_name]
        if not isinstance(candidate_limit, int) or candidate_limit < 1:
            raise ContractViolation(
                f"tuning.max_candidates.{model_name} must be a positive integer"
            )
        search_space = _require_mapping(
            spaces, model_name, parameter_names
        )
        for parameter_name, values in search_space.items():
            if not isinstance(values, list) or not values:
                raise ContractViolation(
                    f"tuning.search_spaces.{model_name}.{parameter_name} "
                    "must be a non-empty list"
                )

    mlflow_config = _require_mapping(config, "mlflow", REQUIRED_MLFLOW_KEYS)
    for key in (
        "tracking_uri",
        "experiment_name",
        "registered_model_name",
        "artifact_path",
    ):
        if not isinstance(mlflow_config[key], str) or not mlflow_config[key]:
            raise ContractViolation(f"mlflow.{key} must be a non-empty string")
    if mlflow_config["artifact_root"] is not None and (
        not isinstance(mlflow_config["artifact_root"], str)
        or not mlflow_config["artifact_root"]
    ):
        raise ContractViolation(
            "mlflow.artifact_root must be null or a non-empty string"
        )
    if mlflow_config["run_name"] is not None and not isinstance(
        mlflow_config["run_name"], str
    ):
        raise ContractViolation("mlflow.run_name must be null or a string")
    if not isinstance(mlflow_config["register_model"], bool):
        raise ContractViolation("mlflow.register_model must be true or false")
    if not isinstance(mlflow_config["log_row_level_artifacts"], bool):
        raise ContractViolation(
            "mlflow.log_row_level_artifacts must be true or false"
        )

    quantile = config["cutoff_quantile"]
    if not isinstance(quantile, (int, float)) or not 0 < float(quantile) < 1:
        raise ContractViolation("cutoff_quantile must be between 0 and 1")
    if (
        not isinstance(config["cost_ratio_fn_fp"], (int, float))
        or config["cost_ratio_fn_fp"] <= 0
    ):
        raise ContractViolation("cost_ratio_fn_fp must be positive")
    if not isinstance(config["calibrate"], bool):
        raise ContractViolation("calibrate must be true or false")
    return config


def split_by_time(
    featured: pd.DataFrame, config: dict[str, Any]
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]:
    """Use a strict date boundary so no calendar date appears on both sides."""
    ordered = featured.sort_values(DATE_COLUMN, kind="mergesort")
    dates = pd.to_datetime(ordered[DATE_COLUMN], errors="coerce")
    if dates.isna().any():
        raise ContractViolation("Training split received unparseable deployment_date")

    if config["cutoff_date"] is not None:
        cutoff = pd.to_datetime(config["cutoff_date"], errors="coerce")
        if pd.isna(cutoff):
            raise ContractViolation(
                f"Invalid cutoff_date: {config['cutoff_date']!r}"
            )
    elif len(ordered):
        position = int(np.floor(len(ordered) * float(config["cutoff_quantile"])))
        position = min(max(position, 1), len(ordered) - 1)
        cutoff = dates.iloc[position]
    else:
        cutoff = pd.NaT

    train_side = ordered.loc[dates < cutoff].copy()
    test_side = ordered.loc[dates >= cutoff].copy()
    if train_side.empty or test_side.empty:
        if dates.empty:
            date_range = "<empty>"
        else:
            date_range = f"{dates.min().isoformat()} to {dates.max().isoformat()}"
        raise ContractViolation(
            "Time split produced an empty train or test side "
            f"(cutoff={cutoff}, date range={date_range}, "
            f"train={len(train_side)}, test={len(test_side)})"
        )
    if train_side[DATE_COLUMN].max() >= test_side[DATE_COLUMN].min():
        raise ContractViolation("Time split boundary is not strictly ordered")
    return train_side, test_side, pd.Timestamp(cutoff)


def _base_model(name: str, params: dict[str, Any], seed: int) -> Any:
    if name == "logistic":
        return LogisticRegression(
            C=float(params["C"]),
            max_iter=int(params["max_iter"]),
            class_weight=params.get("class_weight"),
            random_state=seed,
        )
    if name == "xgboost":
        return XGBClassifier(
            n_estimators=int(params["n_estimators"]),
            max_depth=int(params["max_depth"]),
            learning_rate=float(params["learning_rate"]),
            min_child_weight=float(params.get("min_child_weight", 1)),
            subsample=float(params.get("subsample", 1.0)),
            colsample_bytree=float(params.get("colsample_bytree", 1.0)),
            reg_alpha=float(params.get("reg_alpha", 0.0)),
            reg_lambda=float(params.get("reg_lambda", 1.0)),
            gamma=float(params.get("gamma", 0.0)),
            objective="binary:logistic",
            eval_metric="logloss",
            tree_method="hist",
            random_state=seed,
            n_jobs=1,
            verbosity=0,
        )
    raise ValueError(f"Unknown model: {name}")


def _fit_candidate(
    name: str,
    params: dict[str, Any],
    X: pd.DataFrame,
    y: pd.Series,
    seed: int,
    calibrate: bool,
) -> Any:
    model = _base_model(name, params, seed)
    values = X.to_numpy()
    labels = y.to_numpy()
    if not calibrate:
        model.fit(values, labels)
        return model

    validation_size = max(1, int(np.ceil(len(X) * 0.2)))
    split_position = len(X) - validation_size
    if split_position < 1 or validation_size < 20:
        raise ContractViolation(
            "Calibration requires at least 20 chronological validation rows"
        )
    fit_labels = labels[:split_position]
    if len(np.unique(fit_labels)) < 2:
        raise ContractViolation(
            f"Calibration slice leaves {name} training data with only one class"
        )
    calibration_labels = labels[split_position:]
    if len(np.unique(calibration_labels)) < 2:
        raise ContractViolation(
            f"Calibration slice for {name} contains only one class"
        )
    model.fit(values[:split_position], fit_labels)
    validation_probabilities = model.predict_proba(values[split_position:])[:, 1]
    calibrator = IsotonicRegression(out_of_bounds="clip")
    calibrator.fit(validation_probabilities, calibration_labels)
    return IsotonicCalibratedModel(model, calibrator)


def _date_range(frame: pd.DataFrame) -> dict[str, str]:
    return {
        "start": pd.Timestamp(frame[DATE_COLUMN].min()).isoformat(),
        "end": pd.Timestamp(frame[DATE_COLUMN].max()).isoformat(),
    }


def _candidate_parameters(
    config: dict[str, Any], model_name: str
) -> list[dict[str, Any]]:
    if not config["tuning"]["enabled"]:
        return [dict(config["models"][model_name])]

    search_space = config["tuning"]["search_spaces"][model_name]
    if config["tuning"]["strategy"] == "grid":
        sampled = ParameterGrid(search_space)
    else:
        total_candidates = int(
            np.prod([len(values) for values in search_space.values()])
        )
        candidate_count = min(
            int(config["tuning"]["max_candidates"][model_name]),
            total_candidates,
        )
        model_offset = 0 if model_name == "logistic" else 10_000
        sampled = ParameterSampler(
            search_space,
            n_iter=candidate_count,
            random_state=int(config["seed"]) + model_offset,
        )

    return [
        {
            key: value.item() if isinstance(value, np.generic) else value
            for key, value in dict(params).items()
        }
        for params in sampled
    ]


def _rolling_time_folds(
    train_side: pd.DataFrame,
    fold_count: int,
    initial_train_quantile: float,
) -> list[tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]]:
    """Create expanding folds without allowing one date on both sides."""
    ordered = train_side.sort_values(DATE_COLUMN, kind="mergesort")
    dates = pd.to_datetime(ordered[DATE_COLUMN])
    unique_dates = np.array(sorted(pd.unique(dates)))
    initial_date_count = int(
        np.floor(len(unique_dates) * float(initial_train_quantile))
    )
    initial_date_count = max(initial_date_count, 1)
    validation_dates = unique_dates[initial_date_count:]
    if len(validation_dates) < fold_count:
        raise ContractViolation(
            "Not enough unique deployment dates for tuning.cv_folds "
            f"(dates after initial window={len(validation_dates)}, "
            f"folds={fold_count})"
        )

    folds: list[tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp]] = []
    for date_chunk in np.array_split(validation_dates, fold_count):
        cutoff = pd.Timestamp(date_chunk[0])
        fold_train = ordered.loc[dates < cutoff].copy()
        fold_validation = ordered.loc[dates.isin(date_chunk)].copy()
        if fold_train.empty or fold_validation.empty:
            raise ContractViolation(
                f"Rolling tuning fold at {cutoff.isoformat()} is empty"
            )
        if (
            fold_train[DATE_COLUMN].max()
            >= fold_validation[DATE_COLUMN].min()
        ):
            raise ContractViolation(
                f"Rolling tuning fold at {cutoff.isoformat()} is not strict"
            )
        folds.append((fold_train, fold_validation, cutoff))
    return folds


def _prepare_tuning_folds(
    train_side: pd.DataFrame, config: dict[str, Any]
) -> list[PreparedTuningFold]:
    fold_frames = _rolling_time_folds(
        train_side,
        int(config["tuning"]["cv_folds"]),
        float(config["tuning"]["initial_train_quantile"]),
    )
    prepared: list[PreparedTuningFold] = []
    for number, (fold_train, fold_validation, cutoff) in enumerate(
        fold_frames, start=1
    ):
        preprocessor = Preprocessor().fit(model_inputs(fold_train))
        train_matrix, _ = preprocessor.apply(model_inputs(fold_train))
        validation_matrix, validation_ood = preprocessor.apply(
            model_inputs(fold_validation)
        )
        if validation_ood["unseen"].map(bool).any():
            count = int(validation_ood["unseen"].map(bool).sum())
            print(
                f"[tune] WARNING: fold {number} has {count} rows "
                "with unseen categories"
            )
        train_labels = fold_train[LABEL_COLUMN].astype(int)
        if train_labels.nunique() < 2:
            raise ContractViolation(
                f"Tuning fold {number} training side contains only one class"
            )
        prepared.append(
            PreparedTuningFold(
                number=number,
                cutoff=cutoff,
                train_matrix=train_matrix,
                train_labels=train_labels,
                validation_matrix=validation_matrix,
                validation_labels=fold_validation[LABEL_COLUMN]
                .astype(int)
                .to_numpy(),
                train_size=len(fold_train),
                validation_size=len(fold_validation),
                train_date_range=_date_range(fold_train),
                validation_date_range=_date_range(fold_validation),
            )
        )
        print(
            f"[tune] prepared fold {number}: train={len(fold_train)}, "
            f"validation={len(fold_validation)}, cutoff={cutoff.date()}"
        )
    return prepared


def _evaluate_candidate_on_folds(
    model_name: str,
    params: dict[str, Any],
    folds: list[PreparedTuningFold],
    seed: int,
    calibrate: bool,
    cost_ratio: float,
) -> tuple[float, dict[str, float | int | None], list[dict[str, Any]]]:
    labels_by_fold: list[np.ndarray] = []
    probabilities_by_fold: list[np.ndarray] = []
    for fold in folds:
        model = _fit_candidate(
            model_name,
            params,
            fold.train_matrix,
            fold.train_labels,
            seed,
            calibrate,
        )
        probabilities = model.predict_proba(
            fold.validation_matrix.to_numpy()
        )[:, 1]
        labels_by_fold.append(fold.validation_labels)
        probabilities_by_fold.append(probabilities)

    pooled_labels = np.concatenate(labels_by_fold)
    pooled_probabilities = np.concatenate(probabilities_by_fold)
    threshold, _ = select_threshold(
        pooled_labels, pooled_probabilities, cost_ratio
    )
    metrics, _ = evaluate_binary(
        pooled_labels, pooled_probabilities, threshold, cost_ratio
    )
    fold_metrics = []
    for fold, labels, probabilities in zip(
        folds, labels_by_fold, probabilities_by_fold
    ):
        values, _ = evaluate_binary(
            labels, probabilities, threshold, cost_ratio
        )
        fold_metrics.append(
            {
                "fold": fold.number,
                "cutoff": fold.cutoff.isoformat(),
                **values,
            }
        )
    return threshold, metrics, fold_metrics


def _result_rank(result: dict[str, Any]) -> tuple[float, float, int, str]:
    metrics = result["metrics"]
    auc = metrics["roc_auc"]
    return (
        float(metrics["expected_cost"]),
        -float(auc or 0.0),
        0 if result["model_name"] == "logistic" else 1,
        json.dumps(result["params"], sort_keys=True),
    )


def _mlflow_metrics(
    metrics: dict[str, float | int | None], prefix: str = ""
) -> dict[str, float]:
    return {
        f"{prefix}{name}": float(value)
        for name, value in metrics.items()
        if value is not None and np.isfinite(float(value))
    }


def _trial_frame(trials: list[dict[str, Any]]) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for trial in trials:
        row: dict[str, Any] = {
            "trial_id": trial["trial_id"],
            "run_id": trial["run_id"],
            "model_name": trial["model_name"],
            "params": json.dumps(trial["params"], sort_keys=True),
            "fold_metrics": json.dumps(
                trial.get("fold_metrics", []), sort_keys=True
            ),
        }
        row.update({f"param_{key}": value for key, value in trial["params"].items()})
        row.update({f"metric_{key}": value for key, value in trial["metrics"].items()})
        rows.append(row)
    return pd.DataFrame(rows)


def _loggable_params(params: dict[str, Any]) -> dict[str, Any]:
    return {
        key: "none" if value is None else value
        for key, value in params.items()
    }


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _write_evaluation_artifacts(
    output_dir: Path,
    tuning_trials: list[dict[str, Any]],
    final_results: dict[str, dict[str, Any]],
    champion_name: str,
    champion_probabilities: np.ndarray,
    champion_predictions: np.ndarray,
    test_side: pd.DataFrame,
    test_ood: pd.DataFrame,
    cost_ratio: float,
    feature_list: list[str],
    log_row_level_artifacts: bool,
) -> Path:
    evaluation_dir = output_dir / "evaluation"
    evaluation_dir.mkdir(parents=True, exist_ok=True)

    trial_frame = _trial_frame(tuning_trials)
    trial_frame.to_csv(evaluation_dir / "tuning_trials.csv", index=False)

    comparison_rows = []
    for model_name, result in final_results.items():
        row = {
            "model_name": model_name,
            "params": json.dumps(result["params"], sort_keys=True),
            "validation_threshold": result["threshold"],
        }
        row.update(result["metrics"])
        comparison_rows.append(row)
    comparison = pd.DataFrame(comparison_rows)
    comparison.to_csv(evaluation_dir / "final_model_comparison.csv", index=False)

    champion = final_results[champion_name]
    threshold = float(champion["threshold"])
    if log_row_level_artifacts:
        test_predictions = pd.DataFrame(
            {
                "deployment_id": test_side.index.tolist(),
                "deployment_date": pd.to_datetime(test_side[DATE_COLUMN])
                .map(lambda value: value.isoformat())
                .tolist(),
                "actual_risk": test_side[LABEL_COLUMN].astype(int).tolist(),
                "risk_probability": champion_probabilities,
                "predicted_risk": champion_predictions,
                "risk_prediction": np.where(
                    champion_predictions == 1, "NO_GO", "GO"
                ),
                "risk_band": np.where(
                    champion_probabilities >= threshold,
                    "HIGH",
                    np.where(
                        champion_probabilities >= threshold / 2, "MEDIUM", "LOW"
                    ),
                ),
                "unseen_categories": [
                    json.dumps(value, sort_keys=True, separators=(",", ":"))
                    for value in test_ood["unseen"]
                ],
            }
        )
        test_predictions.to_csv(
            evaluation_dir / "test_predictions.csv", index=False
        )

    threshold_sweep(
        test_side[LABEL_COLUMN].astype(int).to_numpy(),
        champion_probabilities,
        cost_ratio,
    ).to_csv(evaluation_dir / "test_threshold_diagnostics.csv", index=False)
    roc_frame, pr_frame = curve_frames(
        test_side[LABEL_COLUMN].astype(int).to_numpy(), champion_probabilities
    )
    roc_frame.to_csv(evaluation_dir / "roc_curve.csv", index=False)
    pr_frame.to_csv(evaluation_dir / "precision_recall_curve.csv", index=False)
    feature_importance(champion["model"], feature_list).to_csv(
        evaluation_dir / "feature_importance.csv", index=False
    )

    _write_json(evaluation_dir / "test_metrics.json", champion["metrics"])
    _write_json(
        evaluation_dir / "confusion_matrix.json",
        {
            key: champion["metrics"][key]
            for key in (
                "true_negatives",
                "false_positives",
                "false_negatives",
                "true_positives",
            )
        },
    )
    _write_json(
        evaluation_dir / "classification_report.json",
        classification_details(
            test_side[LABEL_COLUMN].astype(int).to_numpy(), champion_predictions
        ),
    )

    report = [
        "<html><body>",
        "<h1>FirmAware evaluation</h1>",
        f"<p>Champion: <strong>{champion_name}</strong></p>",
        "<h2>Final holdout metrics</h2>",
        comparison.to_html(index=False),
        "<h2>Hyperparameter trials</h2>",
        trial_frame.to_html(index=False),
        "<h2>Top feature importance</h2>",
        feature_importance(champion["model"], feature_list)
        .head(30)
        .to_html(index=False),
        "</body></html>",
    ]
    (evaluation_dir / "evaluation_report.html").write_text(
        "\n".join(report), encoding="utf-8"
    )
    return evaluation_dir


def _pip_requirements() -> list[str]:
    distributions = ("pandas", "scikit-learn", "xgboost", "PyYAML", "mlflow")
    return [
        f"{distribution}=={importlib.metadata.version(distribution)}"
        for distribution in distributions
    ]


def _serving_input_example(preprocessor: Preprocessor) -> pd.DataFrame:
    row: dict[str, Any] = {
        "deployment_id": "example-deployment",
        "device_id": "example-device",
        "site_id": "example-site",
        "firmware_fingerprint": "example-fingerprint",
        "current_firmware": "1.0.0",
        "target_firmware": "2.0.0",
        DATE_COLUMN: "2000-01-01",
    }
    for column_index, column in enumerate(CATEGORICAL_COLUMNS):
        categories = preprocessor.encoder.categories_[column_index]
        available = [value for value in categories if not pd.isna(value)]
        row[column] = str(available[0]) if available else "EXAMPLE"
    for column in NUMERIC_COLUMNS:
        row[column] = float(preprocessor.medians[column])
    return pd.DataFrame([row], columns=SCORING_COLUMNS)


def _replace_contents(staging: Path, target: Path) -> None:
    """Move the staged children into an existing target directory.

    The directory swap in `_promote_artifacts` is the preferred path because it
    is atomic. It is not always available: when the target is a mount point --
    an Azure ML pipeline output is a FUSE mount -- renaming it fails with EBUSY
    no matter what permissions the job has, because the kernel will not rename a
    mount. Writing through the mount is the only way to publish to it.

    This is not atomic, and it does not need to be. A pipeline output directory
    is private to the step that writes it and is uploaded only after the step
    exits, so no reader can observe the intermediate state that atomicity exists
    to hide.
    """
    for existing in target.iterdir():
        if existing.is_dir() and not existing.is_symlink():
            shutil.rmtree(existing)
        else:
            existing.unlink()
    for item in staging.iterdir():
        # shutil.move rather than Path.replace: staging is on the node's local
        # disk and target is the mount, so this is a cross-device move that
        # rename(2) cannot do.
        shutil.move(str(item), str(target / item.name))
    staging.rmdir()


def _promote_artifacts(staging: Path, target: Path, run_id: str) -> None:
    backup = target.parent / f".{target.name}-{run_id}.backup"
    if backup.exists():
        shutil.rmtree(backup)
    if target.exists():
        try:
            target.replace(backup)
        except OSError as error:
            if error.errno not in (errno.EBUSY, errno.EXDEV):
                raise
            _replace_contents(staging, target)
            return
    try:
        staging.replace(target)
    except OSError:
        if backup.exists() and not target.exists():
            backup.replace(target)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def _train_impl(
    input_path: str | Path,
    config_path: str | Path,
    artifacts_dir: str | Path,
    remote_artifacts_uri: str | None,
) -> dict[str, Any]:
    """Tune on rolling CV, compare family winners on holdout, and publish."""
    config = load_config(config_path)
    tracking = configure_tracking(config["mlflow"], config_path)
    raw = read_csv(input_path)
    validated = validate(raw, mode="training")
    featured = derive_features(validated, mode="training")
    train_side, test_side, cutoff = split_by_time(featured, config)
    tuning_folds = _prepare_tuning_folds(train_side, config)

    timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    run_name = tracking["run_name"] or f"firmaware-{timestamp}"
    target_output_dir = Path(artifacts_dir)
    target_output_dir.parent.mkdir(parents=True, exist_ok=True)

    with mlflow.start_run(run_name=run_name) as parent_run:
        parent_run_id = parent_run.info.run_id
        output_dir = (
            target_output_dir.parent
            / f".{target_output_dir.name}-{parent_run_id}.staging"
        )
        if output_dir.exists():
            shutil.rmtree(output_dir)
        output_dir.mkdir(parents=True)
        mlflow.set_tags(
            {
                "pipeline": "firmaware",
                "stage": "tune-train-evaluate",
                "spec_version": SPEC_VERSION,
                "selection_data": "pooled_expanding_time_cv",
                "final_evaluation_data": "outer_family_selection_holdout",
                "row_level_artifacts": str(
                    config["mlflow"]["log_row_level_artifacts"]
                ).lower(),
            }
        )
        mlflow.log_params(
            {
                "seed": config["seed"],
                "outer_cutoff": cutoff.isoformat(),
                "tuning_strategy": config["tuning"]["strategy"],
                "cv_folds": config["tuning"]["cv_folds"],
                "initial_train_quantile": config["tuning"][
                    "initial_train_quantile"
                ],
                "cost_ratio_fn_fp": config["cost_ratio_fn_fp"],
                "calibrated": config["calibrate"],
                "tuning_enabled": config["tuning"]["enabled"],
            }
        )

        tuning_trials: list[dict[str, Any]] = []
        trial_number = 0
        for model_name in ("logistic", "xgboost"):
            for params in _candidate_parameters(config, model_name):
                trial_number += 1
                trial_id = f"{model_name}-{trial_number:03d}"
                with mlflow.start_run(run_name=trial_id, nested=True) as trial_run:
                    threshold, metrics, fold_metrics = (
                        _evaluate_candidate_on_folds(
                            model_name,
                            params,
                            tuning_folds,
                            int(config["seed"]),
                            bool(config["calibrate"]),
                            float(config["cost_ratio_fn_fp"]),
                        )
                    )
                    mlflow.set_tags(
                        {
                            "stage": "hyperparameter-tuning",
                            "model_family": model_name,
                            "trial_id": trial_id,
                            "validation_scheme": "expanding_time_cv",
                        }
                    )
                    mlflow.log_params(
                        {
                            "model_name": model_name,
                            **_loggable_params(params),
                        }
                    )
                    mlflow.log_metrics(_mlflow_metrics(metrics, "validation_"))
                    for fold_metrics_row in fold_metrics:
                        fold_number = int(fold_metrics_row["fold"])
                        mlflow.log_metrics(
                            _mlflow_metrics(
                                {
                                    key: value
                                    for key, value in fold_metrics_row.items()
                                    if key not in {"fold", "cutoff"}
                                },
                                f"fold_{fold_number}_",
                            )
                        )
                    tuning_trials.append(
                        {
                            "trial_id": trial_id,
                            "run_id": trial_run.info.run_id,
                            "model_name": model_name,
                            "params": params,
                            "threshold": threshold,
                            "metrics": metrics,
                            "fold_metrics": fold_metrics,
                        }
                    )
                print(
                    f"[tune] {trial_id} pooled_cost="
                    f"{metrics['expected_cost']:.3f} "
                    f"roc_auc={metrics['roc_auc']} threshold={threshold:.2f}"
                )

        family_best = {
            model_name: min(
                (
                    trial
                    for trial in tuning_trials
                    if trial["model_name"] == model_name
                ),
                key=_result_rank,
            )
            for model_name in ("logistic", "xgboost")
        }
        cv_preferred_trial = min(tuning_trials, key=_result_rank)

        final_preprocessor = Preprocessor().fit(model_inputs(train_side))
        train_matrix, _ = final_preprocessor.apply(model_inputs(train_side))
        test_matrix, test_ood = final_preprocessor.apply(model_inputs(test_side))
        if test_ood["unseen"].map(bool).any():
            count = int(test_ood["unseen"].map(bool).sum())
            print(f"[train] WARNING: {count} test rows contain unseen categories")

        train_labels = train_side[LABEL_COLUMN].astype(int)
        test_labels = test_side[LABEL_COLUMN].astype(int).to_numpy()
        final_results: dict[str, dict[str, Any]] = {}
        for model_name in ("logistic", "xgboost"):
            selected = family_best[model_name]
            model = _fit_candidate(
                model_name,
                selected["params"],
                train_matrix,
                train_labels,
                int(config["seed"]),
                bool(config["calibrate"]),
            )
            probabilities = model.predict_proba(test_matrix.to_numpy())[:, 1]
            metrics, predictions = evaluate_binary(
                test_labels,
                probabilities,
                float(selected["threshold"]),
                float(config["cost_ratio_fn_fp"]),
            )
            final_results[model_name] = {
                "model": model,
                "params": selected["params"],
                "threshold": selected["threshold"],
                "validation_metrics": selected["metrics"],
                "metrics": metrics,
                "probabilities": probabilities,
                "predictions": predictions,
            }
            with mlflow.start_run(
                run_name=f"final-{model_name}", nested=True
            ):
                mlflow.set_tags(
                    {"stage": "holdout-evaluation", "model_family": model_name}
                )
                mlflow.log_params(
                    {
                        "model_name": model_name,
                        "selection_threshold": selected["threshold"],
                        **_loggable_params(selected["params"]),
                    }
                )
                mlflow.log_metrics(_mlflow_metrics(metrics, "test_"))

        if config["tuning"]["champion_selection"] == "holdout_expected_cost":
            champion_name = min(
                final_results,
                key=lambda name: (
                    float(final_results[name]["metrics"]["expected_cost"]),
                    -float(final_results[name]["metrics"]["roc_auc"] or 0.0),
                    0 if name == "logistic" else 1,
                ),
            )
        else:
            champion_name = cv_preferred_trial["model_name"]
        champion = final_results[champion_name]
        print("[train] final family holdout comparison")
        print("[train] model      cost  roc_auc  threshold")
        for model_name in ("logistic", "xgboost"):
            result = final_results[model_name]
            auc = result["metrics"]["roc_auc"]
            auc_text = "n/a" if auc is None else f"{auc:.6f}"
            print(
                f"[train] {model_name:<10} "
                f"{result['metrics']['expected_cost']:>4.0f}  "
                f"{auc_text:>7}  {result['threshold']:.2f}"
            )
        print(
            f"[train] CV-preferred family: {cv_preferred_trial['model_name']}; "
            f"published champion ({config['tuning']['champion_selection']}): "
            f"{champion_name} "
            f"{champion['params']}"
        )

        metadata: dict[str, Any] = {
            "model_name": champion_name,
            "params": champion["params"],
            "threshold": champion["threshold"],
            "cost_ratio_fn_fp": config["cost_ratio_fn_fp"],
            "cutoff_date": cutoff.isoformat(),
            "validation_cutoff_date": tuning_folds[0].cutoff.isoformat(),
            "train_size": len(train_side),
            "validation_train_size": tuning_folds[0].train_size,
            "validation_size": sum(
                fold.validation_size for fold in tuning_folds
            ),
            "test_size": len(test_side),
            "train_date_range": _date_range(train_side),
            "validation_train_date_range": tuning_folds[0].train_date_range,
            "validation_date_range": {
                "start": tuning_folds[0].validation_date_range["start"],
                "end": tuning_folds[-1].validation_date_range["end"],
            },
            "test_date_range": _date_range(test_side),
            "metrics": {
                name: final_results[name]["metrics"]
                for name in ("logistic", "xgboost")
            },
            "tuning": {
                "strategy": (
                    f"{config['tuning']['strategy']}_expanding_time_cv"
                ),
                "cv_fold_count": len(tuning_folds),
                "initial_train_quantile": config["tuning"][
                    "initial_train_quantile"
                ],
                "folds": [
                    {
                        "fold": fold.number,
                        "cutoff": fold.cutoff.isoformat(),
                        "train_size": fold.train_size,
                        "validation_size": fold.validation_size,
                        "train_date_range": fold.train_date_range,
                        "validation_date_range": fold.validation_date_range,
                    }
                    for fold in tuning_folds
                ],
                "candidate_count": len(tuning_trials),
                "selection_metric": "expected_cost",
                "champion_selection": config["tuning"]["champion_selection"],
                "cv_preferred_model": cv_preferred_trial["model_name"],
                "champion_validation_metrics": cv_preferred_trial["metrics"],
                "family_best": {
                    name: {
                        "params": family_best[name]["params"],
                        "threshold": family_best[name]["threshold"],
                        "validation_metrics": family_best[name]["metrics"],
                        "fold_metrics": family_best[name]["fold_metrics"],
                    }
                    for name in ("logistic", "xgboost")
                },
            },
            "seed": config["seed"],
            "calibrated": config["calibrate"],
            "timestamp": timestamp,
            "spec_version": SPEC_VERSION,
            "artifact_run_id": parent_run_id,
            "artifact_uri": (
                join_uri(remote_artifacts_uri, f"runs/{parent_run_id}")
                if remote_artifacts_uri
                else str(target_output_dir)
            ),
            "mlflow": {
                "tracking_uri": tracking["display_tracking_uri"],
                "experiment_name": tracking["experiment_name"],
                "run_id": parent_run_id,
                "model_uri": (
                    f"runs:/{parent_run_id}/{tracking['artifact_path']}"
                ),
                "registered_model_name": (
                    tracking["registered_model_name"]
                    if tracking["register_model"]
                    else None
                ),
                "registered_model_version": None,
                "registered_model_uri": None,
            },
        }

        joblib.dump(champion["model"], output_dir / "model.joblib")
        final_preprocessor.save(output_dir / "preprocessor.joblib")
        metadata_path = output_dir / "metadata.json"
        _write_json(metadata_path, metadata)

        evaluation_dir = _write_evaluation_artifacts(
            output_dir,
            tuning_trials,
            final_results,
            champion_name,
            champion["probabilities"],
            champion["predictions"],
            test_side,
            test_ood,
            float(config["cost_ratio_fn_fp"]),
            final_preprocessor.feature_list,
            bool(config["mlflow"]["log_row_level_artifacts"]),
        )

        mlflow.log_params(
            {
                "champion_model": champion_name,
                "champion_params": json.dumps(champion["params"], sort_keys=True),
                "champion_threshold": champion["threshold"],
                "candidate_count": len(tuning_trials),
            }
        )
        mlflow.log_metrics(_mlflow_metrics(champion["metrics"], "test_"))
        mlflow.log_metrics(
            _mlflow_metrics(
                cv_preferred_trial["metrics"], "cv_preferred_"
            )
        )
        mlflow.log_artifacts(str(evaluation_dir), artifact_path="evaluation")
        for artifact_name in (
            "feature_list.json",
            "medians.json",
        ):
            mlflow.log_artifact(
                str(output_dir / artifact_name), artifact_path="pipeline"
            )

        input_example = _serving_input_example(final_preprocessor)
        score_dataframe(
            input_example, champion["model"], final_preprocessor, metadata
        )
        package_dir = Path(__file__).resolve().parent
        registered_model_name = (
            tracking["registered_model_name"]
            if tracking["register_model"]
            else None
        )
        model_info = mlflow.pyfunc.log_model(
            artifact_path=tracking["artifact_path"],
            python_model=FirmAwarePyFuncModel(
                champion["model"], final_preprocessor, metadata
            ),
            code_paths=[str(package_dir)],
            input_example=input_example,
            signature=serving_signature(),
            pip_requirements=_pip_requirements(),
            registered_model_name=registered_model_name,
        )
        registered_version = getattr(
            model_info, "registered_model_version", None
        )
        if registered_version is not None and registered_model_name is not None:
            metadata["mlflow"]["registered_model_version"] = str(
                registered_version
            )
            metadata["mlflow"]["registered_model_uri"] = (
                f"models:/{registered_model_name}/{registered_version}"
            )
        _write_json(metadata_path, metadata)
        mlflow.log_artifact(str(metadata_path), artifact_path="pipeline")
        if remote_artifacts_uri:
            pointer = publish_artifact_run(
                output_dir,
                remote_artifacts_uri,
                parent_run_id,
                promoted_at=timestamp,
            )
            print(
                f"[artifacts] champion: {remote_artifacts_uri.rstrip('/')}/"
                f"champion.json -> {pointer['run_id']}"
            )
        else:
            _promote_artifacts(output_dir, target_output_dir, parent_run_id)
        print(f"[mlflow] run: {metadata['mlflow']['model_uri']}")
        if metadata["mlflow"]["registered_model_uri"]:
            print(
                "[mlflow] registered model: "
                f"{metadata['mlflow']['registered_model_uri']}"
            )

    return metadata


def train(
    input_path: str | Path,
    config_path: str | Path = "config.yaml",
    artifacts_dir: str | Path = "artifacts",
) -> dict[str, Any]:
    """Train locally or publish an immutable run plus champion pointer to GCS."""
    if is_gcs_uri(artifacts_dir):
        with tempfile.TemporaryDirectory(
            prefix="firmaware-training-"
        ) as directory:
            return _train_impl(
                input_path,
                config_path,
                Path(directory) / "artifacts",
                str(artifacts_dir),
            )
    return _train_impl(input_path, config_path, artifacts_dir, None)
