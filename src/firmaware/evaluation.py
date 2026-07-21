"""Produce cost-aware metrics and diagnostics from one fixed decision threshold."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    brier_score_loss,
    classification_report,
    confusion_matrix,
    f1_score,
    log_loss,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)


def threshold_sweep(
    labels: np.ndarray, probabilities: np.ndarray, cost_ratio: float
) -> pd.DataFrame:
    """Expose every decision-line tradeoff instead of logging only the winner."""
    rows: list[dict[str, float | int]] = []
    for threshold_index in range(1, 100):
        threshold = threshold_index / 100
        predictions = probabilities >= threshold
        false_negatives = int(((predictions == 0) & (labels == 1)).sum())
        false_positives = int(((predictions == 1) & (labels == 0)).sum())
        rows.append(
            {
                "threshold": threshold,
                "false_negatives": false_negatives,
                "false_positives": false_positives,
                "expected_cost": false_negatives * cost_ratio + false_positives,
            }
        )
    return pd.DataFrame(rows)


def select_threshold(
    labels: np.ndarray, probabilities: np.ndarray, cost_ratio: float
) -> tuple[float, pd.DataFrame]:
    """Keep the lowest threshold when expected-cost candidates tie."""
    sweep = threshold_sweep(labels, probabilities, cost_ratio)
    best_row = sweep.loc[sweep["expected_cost"].idxmin()]
    return float(best_row["threshold"]), sweep


def evaluate_binary(
    labels: np.ndarray,
    probabilities: np.ndarray,
    threshold: float,
    cost_ratio: float,
) -> tuple[dict[str, float | int | None], np.ndarray]:
    predictions = (probabilities >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(labels, predictions, labels=[0, 1]).ravel()
    try:
        roc_auc: float | None = float(roc_auc_score(labels, probabilities))
        average_precision: float | None = float(
            average_precision_score(labels, probabilities)
        )
    except ValueError:
        roc_auc = None
        average_precision = None

    specificity = float(tn / (tn + fp)) if tn + fp else 0.0
    false_positive_rate = float(fp / (fp + tn)) if fp + tn else 0.0
    false_negative_rate = float(fn / (fn + tp)) if fn + tp else 0.0
    clipped = np.clip(probabilities, 1e-15, 1 - 1e-15)
    metrics: dict[str, float | int | None] = {
        "accuracy": float(accuracy_score(labels, predictions)),
        "precision": float(precision_score(labels, predictions, zero_division=0)),
        "recall": float(recall_score(labels, predictions, zero_division=0)),
        "specificity": specificity,
        "false_positive_rate": false_positive_rate,
        "false_negative_rate": false_negative_rate,
        "f1": float(f1_score(labels, predictions, zero_division=0)),
        "roc_auc": roc_auc,
        "average_precision": average_precision,
        "expected_cost": float(fn * cost_ratio + fp),
        "brier": float(brier_score_loss(labels, probabilities)),
        "log_loss": float(log_loss(labels, clipped, labels=[0, 1])),
        "true_negatives": int(tn),
        "false_positives": int(fp),
        "false_negatives": int(fn),
        "true_positives": int(tp),
        "threshold": threshold,
    }
    return metrics, predictions


def classification_details(
    labels: np.ndarray, predictions: np.ndarray
) -> dict[str, Any]:
    return classification_report(
        labels,
        predictions,
        labels=[0, 1],
        target_names=["success", "risky"],
        output_dict=True,
        zero_division=0,
    )


def curve_frames(
    labels: np.ndarray, probabilities: np.ndarray
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if len(np.unique(labels)) < 2:
        return (
            pd.DataFrame(columns=["false_positive_rate", "true_positive_rate", "threshold"]),
            pd.DataFrame(columns=["recall", "precision", "threshold"]),
        )

    false_positive_rate, true_positive_rate, roc_thresholds = roc_curve(
        labels, probabilities
    )
    precision, recall, pr_thresholds = precision_recall_curve(labels, probabilities)
    roc_frame = pd.DataFrame(
        {
            "false_positive_rate": false_positive_rate,
            "true_positive_rate": true_positive_rate,
            "threshold": roc_thresholds,
        }
    )
    pr_frame = pd.DataFrame(
        {
            "recall": recall[:-1],
            "precision": precision[:-1],
            "threshold": pr_thresholds,
        }
    )
    return roc_frame, pr_frame


def feature_importance(model: Any, feature_list: list[str]) -> pd.DataFrame:
    """Normalize coefficients and tree importances into one portable table."""
    fitted_model = getattr(model, "base_model", model)
    if hasattr(fitted_model, "coef_"):
        importance = np.asarray(fitted_model.coef_).reshape(-1)
    elif hasattr(fitted_model, "feature_importances_"):
        importance = np.asarray(fitted_model.feature_importances_).reshape(-1)
    else:
        return pd.DataFrame(columns=["feature", "importance", "absolute_importance"])

    frame = pd.DataFrame(
        {
            "feature": feature_list,
            "importance": importance,
            "absolute_importance": np.abs(importance),
        }
    )
    return frame.sort_values("absolute_importance", ascending=False, kind="mergesort")

