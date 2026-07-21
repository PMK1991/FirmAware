"""Package raw contract-to-decision inference as a cloud-portable MLflow model."""

from __future__ import annotations

import json
from typing import Any

import mlflow.pyfunc
import pandas as pd
from mlflow.models import ModelSignature
from mlflow.types import ColSpec, Schema

from .features import derive_features, model_inputs
from .schema import NUMERIC_COLUMNS, SCORING_COLUMNS, validate
from .transform import Preprocessor

SCORE_COLUMNS = [
    "deployment_id",
    "risk_probability",
    "risk_prediction",
    "risk_band",
    "unseen_categories",
    "model_run",
]


def score_dataframe(
    raw: pd.DataFrame,
    model: Any,
    preprocessor: Preprocessor,
    metadata: dict[str, Any],
) -> pd.DataFrame:
    """Keep local batch and hosted MLflow inference behavior identical."""
    validated = validate(raw, mode="scoring")
    featured = derive_features(validated, mode="scoring")
    matrix, ood_report = preprocessor.apply(model_inputs(featured))
    probabilities = model.predict_proba(matrix.to_numpy())[:, 1]
    threshold = float(metadata["threshold"])
    return pd.DataFrame(
        {
            "deployment_id": ood_report["deployment_id"].tolist(),
            "risk_probability": [round(float(value), 4) for value in probabilities],
            "risk_prediction": [
                "NO_GO" if probability >= threshold else "GO"
                for probability in probabilities
            ],
            "risk_band": [
                "HIGH"
                if probability >= threshold
                else "MEDIUM"
                if probability >= threshold / 2
                else "LOW"
                for probability in probabilities
            ],
            "unseen_categories": [
                json.dumps(value, sort_keys=True, separators=(",", ":"))
                for value in ood_report["unseen"]
            ],
            "model_run": metadata["timestamp"],
        },
        columns=SCORE_COLUMNS,
    )


class FirmAwarePyFuncModel(mlflow.pyfunc.PythonModel):
    """Serve raw scoring rows without requiring callers to reproduce preprocessing."""

    def __init__(
        self,
        model: Any,
        preprocessor: Preprocessor,
        metadata: dict[str, Any],
    ) -> None:
        self.model = model
        self.preprocessor = preprocessor
        self.metadata = metadata

    def predict(
        self,
        context: mlflow.pyfunc.PythonModelContext,
        model_input: pd.DataFrame,
        params: dict[str, Any] | None = None,
    ) -> pd.DataFrame:
        return score_dataframe(
            model_input, self.model, self.preprocessor, self.metadata
        )


def serving_signature() -> ModelSignature:
    """Allow nullable numerics while requiring every scoring-contract column."""
    numeric = set(NUMERIC_COLUMNS)
    inputs = Schema(
        [
            ColSpec("double" if column in numeric else "string", column)
            for column in SCORING_COLUMNS
        ]
    )
    outputs = Schema(
        [
            ColSpec("string", "deployment_id"),
            ColSpec("double", "risk_probability"),
            ColSpec("string", "risk_prediction"),
            ColSpec("string", "risk_band"),
            ColSpec("string", "unseen_categories"),
            ColSpec("string", "model_run"),
        ]
    )
    return ModelSignature(inputs=inputs, outputs=outputs)
