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
# The RAG notebook is held to the same executed-and-clean standard but not to
# the no-import rule below. The ML notebooks are the research path that came
# before their modules; this one builds on modules that already exist (the
# corpus and chunker, plus schema and features for deployment flags), and its
# explicit cells are the research path for the parts that do not yet
# (retrieval, gate, generation).
RAG_NOTEBOOKS = ["06_rag_local.ipynb"]


class NotebookTests(unittest.TestCase):
    def test_research_notebook_sequence_is_complete_and_executed(self) -> None:
        # The RAG notebook's outputs quote the corpus, whose publication is
        # undecided, so a checkout may lack it; when present it must be clean.
        rag = [n for n in RAG_NOTEBOOKS if (ROOT / "notebooks" / n).is_file()]
        for name in NOTEBOOKS + rag:
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
