from __future__ import annotations

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = [
    "01_data_exploration.ipynb",
    "02_feature_engineering.ipynb",
    "03_hyperparameter_tuning.ipynb",
    "04_training_and_evaluation.ipynb",
    "05_local_deployment_test.ipynb",
]


class NotebookTests(unittest.TestCase):
    def test_research_notebook_sequence_is_complete_and_executed(self) -> None:
        for name in NOTEBOOKS:
            path = ROOT / "notebooks" / name
            notebook = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(notebook["nbformat"], 4)
            self.assertTrue(notebook["cells"])
            code_cells = [
                cell
                for cell in notebook["cells"]
                if cell["cell_type"] == "code"
            ]
            self.assertTrue(code_cells)
            self.assertTrue(
                all(cell.get("execution_count") is not None for cell in code_cells)
            )
            self.assertTrue(any(cell.get("outputs") for cell in code_cells))
            errors = [
                output
                for cell in code_cells
                for output in cell.get("outputs", [])
                if output.get("output_type") == "error"
            ]
            self.assertEqual(errors, [])

    def test_research_code_does_not_import_pipeline_modules(self) -> None:
        for name in NOTEBOOKS:
            notebook = json.loads(
                (ROOT / "notebooks" / name).read_text(encoding="utf-8")
            )
            code = "\n".join(
                "".join(cell["source"])
                for cell in notebook["cells"]
                if cell["cell_type"] == "code"
            )
            self.assertNotIn("from firmaware", code)
            self.assertNotIn("import firmaware", code)


if __name__ == "__main__":
    unittest.main()
