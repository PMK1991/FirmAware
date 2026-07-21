"""Persist one preprocessing path so training and scoring cannot diverge."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from .features import DERIVED_NUMERIC_COLUMNS
from .schema import CATEGORICAL_COLUMNS, NUMERIC_COLUMNS, ContractViolation


class Preprocessor:
    """Apply impute, encode, align, then scale in that fixed order."""

    def __init__(self) -> None:
        self.numeric_columns = NUMERIC_COLUMNS + DERIVED_NUMERIC_COLUMNS
        self.categorical_columns = list(CATEGORICAL_COLUMNS)
        self.medians: dict[str, float] = {}
        self.encoder = OneHotEncoder(handle_unknown="ignore", sparse_output=False)
        self.scaler = StandardScaler()
        self.feature_list: list[str] = []

    def _require_columns(self, X: pd.DataFrame) -> None:
        required = set(self.numeric_columns + self.categorical_columns)
        missing = sorted(required - set(X.columns))
        if missing:
            raise ContractViolation(
                f"Preprocessor input is missing required feature columns: {missing}"
            )

    def fit(self, X: pd.DataFrame) -> "Preprocessor":
        """Fit every statistic on the training side only to prevent temporal leakage."""
        self._require_columns(X)

        numeric = (
            X[self.numeric_columns]
            .apply(pd.to_numeric, errors="coerce")
            .astype(float)
        )
        medians = numeric.median()
        unusable = medians[medians.isna()].index.tolist()
        if unusable:
            raise ContractViolation(
                f"Cannot impute all-NaN training numeric columns: {unusable}"
            )
        self.medians = {column: float(medians[column]) for column in self.numeric_columns}
        imputed = numeric.fillna(self.medians)

        self.encoder.fit(X[self.categorical_columns])
        encoded_names = self.encoder.get_feature_names_out(
            self.categorical_columns
        ).tolist()
        self.feature_list = self.numeric_columns + encoded_names
        self.scaler.fit(imputed[self.numeric_columns])
        return self

    @staticmethod
    def _seen(value: object, categories: np.ndarray) -> bool:
        if pd.isna(value):
            return any(pd.isna(category) for category in categories)
        return any(not pd.isna(category) and value == category for category in categories)

    @staticmethod
    def _plain_value(value: object) -> Any:
        if pd.isna(value):
            return None
        if isinstance(value, np.generic):
            return value.item()
        return value

    def _ood_report(self, X: pd.DataFrame) -> pd.DataFrame:
        rows: list[dict[str, object]] = []
        for position, (deployment_id, row) in enumerate(
            X[self.categorical_columns].iterrows()
        ):
            unseen: dict[str, object] = {}
            for column_index, column in enumerate(self.categorical_columns):
                value = row[column]
                if not self._seen(value, self.encoder.categories_[column_index]):
                    unseen[column] = self._plain_value(value)
            output_id = deployment_id
            if "deployment_id" in X.columns:
                output_id = X.iloc[position]["deployment_id"]
            rows.append({"deployment_id": output_id, "unseen": unseen})
        return pd.DataFrame(rows, columns=["deployment_id", "unseen"])

    def apply(self, X: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Align raw zeros before scaling so missing columns never enter scaled space."""
        if not self.feature_list or not self.medians:
            raise ContractViolation("Preprocessor must be fitted before apply")
        self._require_columns(X)

        numeric = (
            X[self.numeric_columns]
            .apply(pd.to_numeric, errors="coerce")
            .astype(float)
        )
        imputed = numeric.fillna(self.medians)

        encoded_values = self.encoder.transform(X[self.categorical_columns])
        encoded_names = self.encoder.get_feature_names_out(
            self.categorical_columns
        ).tolist()
        encoded = pd.DataFrame(encoded_values, columns=encoded_names, index=X.index)

        matrix = pd.concat([imputed, encoded], axis=1)
        matrix = matrix.reindex(columns=self.feature_list, fill_value=0.0)
        matrix.loc[:, self.numeric_columns] = self.scaler.transform(
            matrix[self.numeric_columns]
        )
        matrix = matrix.astype(float)
        return matrix, self._ood_report(X)

    def save(self, path: str | Path) -> None:
        """Persist fitted state plus human-readable feature and imputation manifests."""
        output_path = Path(path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        joblib.dump(self, output_path)
        (output_path.parent / "feature_list.json").write_text(
            json.dumps(self.feature_list, indent=2) + "\n", encoding="utf-8"
        )
        (output_path.parent / "medians.json").write_text(
            json.dumps(self.medians, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: str | Path) -> "Preprocessor":
        """Load only a fitted FirmAware preprocessor artifact."""
        loaded = joblib.load(Path(path))
        if not isinstance(loaded, cls):
            raise ContractViolation(
                f"Artifact at {path} is not a FirmAware Preprocessor"
            )
        return loaded
