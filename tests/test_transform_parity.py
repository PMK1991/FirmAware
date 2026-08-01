from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

import firmaware.predict as predict_module
import firmaware.train as train_module
from firmaware.features import derive_features, model_inputs
from firmaware.schema import validate
from firmaware.transform import Preprocessor
from tests.test_schema import make_training_frame


class TransformParityTests(unittest.TestCase):
    def setUp(self) -> None:
        validated = validate(make_training_frame(40), mode="training")
        self.featured = derive_features(validated, mode="training")
        self.inputs = model_inputs(self.featured)

    def test_save_load_is_bit_exact_and_modules_share_class(self) -> None:
        preprocessor = Preprocessor().fit(self.inputs.iloc[:32])
        expected, _ = preprocessor.apply(self.inputs.iloc[32:])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "preprocessor.joblib"
            preprocessor.save(path)
            loaded = Preprocessor.load(path)
            actual, _ = loaded.apply(self.inputs.iloc[32:])

        pd.testing.assert_frame_equal(expected, actual, check_exact=True)
        self.assertIs(train_module.Preprocessor, predict_module.Preprocessor)

    def test_unseen_vendor_is_reported_and_vendor_block_is_zero(self) -> None:
        preprocessor = Preprocessor().fit(self.inputs.iloc[:32])
        scoring = self.inputs.iloc[[32]].copy()
        scoring.loc[:, "vendor_name"] = "Moxa"
        matrix, ood = preprocessor.apply(scoring)

        self.assertEqual(ood.iloc[0]["unseen"], {"vendor_name": "Moxa"})
        vendor_columns = [
            column for column in matrix.columns if column.startswith("vendor_name_")
        ]
        self.assertTrue(vendor_columns)
        self.assertEqual(float(matrix.loc[:, vendor_columns].sum(axis=1).iloc[0]), 0.0)

        known_matrix, _ = preprocessor.apply(self.inputs.iloc[[32]])
        self.assertEqual(
            float(known_matrix.loc[:, vendor_columns].sum(axis=1).iloc[0]), 1.0
        )


if __name__ == "__main__":
    unittest.main()

