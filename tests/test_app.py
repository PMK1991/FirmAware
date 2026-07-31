from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from firmaware.features import derive_features
from firmaware.schema import validate
from tests.test_schema import make_training_frame

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app.py"

try:  # the page ships as the optional "app" extra
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover - exercised only without the extra
    AppTest = None


SCORING_ONLY_COLUMNS = [
    "deployment_outcome",
    "time_to_failure_hours",
    "rollback_required",
]


def build_fixture(directory: Path, threshold: float = 0.4) -> dict[str, str]:
    """Write the minimum score/artifact/input trio the page reads."""
    upcoming = make_training_frame(12).drop(columns=SCORING_ONLY_COLUMNS)
    upcoming_path = directory / "upcoming_deployments.csv"
    upcoming.to_csv(upcoming_path, index=False)

    featured = derive_features(validate(upcoming, "scoring"), "scoring")
    probabilities = [
        round(0.05 + (index * 0.07), 4) for index in range(len(featured))
    ]
    scores = pd.DataFrame(
        {
            "deployment_id": featured.index.astype(str),
            "risk_probability": probabilities,
            "risk_prediction": [
                "NO_GO" if value >= threshold else "GO" for value in probabilities
            ],
            "risk_band": [
                "HIGH"
                if value >= threshold
                else "MEDIUM"
                if value >= threshold / 2
                else "LOW"
                for value in probabilities
            ],
            "model_run": "run-under-test",
            "scored_at": "2026-07-31T00:00:00.000001Z",
        }
    )
    scores_path = directory / "scores.csv"
    scores.to_csv(scores_path, index=False)

    artifacts = directory / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "metadata.json").write_text(
        json.dumps({"threshold": threshold, "model_name": "xgboost"}),
        encoding="utf-8",
    )

    return {
        "FIRMAWARE_SCORES_URI": str(scores_path),
        "FIRMAWARE_DATA_URI": str(directory),
        "FIRMAWARE_ARTIFACTS_URI": str(artifacts),
    }


@unittest.skipIf(AppTest is None, "streamlit is not installed")
class AppTests(unittest.TestCase):
    def start(self, environment: dict[str, str]) -> "AppTest":
        """Keep the environment patched for the whole test.

        Every AppTest interaction re-executes the script, and the page reads its
        URIs at import time, so the patch must outlive the first run.
        """
        self.enterContext(patch.dict(os.environ, environment, clear=False))
        return AppTest.from_file(str(APP), default_timeout=180).run()

    @staticmethod
    def picker(page: "AppTest", label: str):
        """Select widgets by label; index shifts once the run selector appears."""
        return next(widget for widget in page.selectbox if widget.label == label)

    def test_both_views_render_every_scored_deployment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = self.start(build_fixture(Path(directory)))

            page.radio[0].set_value("Fleet Overview").run()
            self.assertEqual(page.exception, [])
            self.assertEqual(len(page.dataframe[0].value), 12)

            page.radio[0].set_value("Deployment Inspector").run()
            picker = self.picker(page, "Select a deployment")
            deployments = [
                option
                for option in picker.options
                if str(option).startswith("deployment-")
            ]
            self.assertEqual(len(deployments), 12)
            for deployment in deployments[:3]:
                self.picker(page, "Select a deployment").set_value(deployment).run()
                self.assertEqual(page.exception, [])

    def test_page_reports_missing_scores_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            environment = build_fixture(Path(directory))
            Path(environment["FIRMAWARE_SCORES_URI"]).unlink()

            page = self.start(environment)

            self.assertEqual(page.exception, [])
            self.assertTrue(page.error)
            self.assertIn("No scores found", page.error[0].value)

    def test_decisions_and_bands_follow_the_champion_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            page = self.start(build_fixture(Path(directory), threshold=0.4))
            page.radio[0].set_value("Fleet Overview").run()

            rendered = " ".join(str(block.value) for block in page.markdown)
            self.assertIn("0.4000", rendered)

            register = page.dataframe[0].value
            probability = (
                register["P(Failure)"].astype(str).str.rstrip("%").astype(float) / 100
            )
            decision = register["Prediction"].astype(str)

            self.assertTrue(probability.between(0, 1).all())
            self.assertTrue(decision.eq("NO_GO").any() and decision.eq("GO").any())
            self.assertTrue(((probability >= 0.4) == decision.eq("NO_GO")).all())
            self.assertTrue(
                (register["Band"].astype(str).eq("HIGH") == (probability >= 0.4)).all()
            )

    def test_gauge_zones_track_the_champion_threshold(self) -> None:
        """A hardcoded cutoff would make the gauge contradict model.score_dataframe."""
        with tempfile.TemporaryDirectory() as directory:
            page = self.start(build_fixture(Path(directory), threshold=0.4))
            page.radio[0].set_value("Fleet Overview").run()

            gauge = json.loads(page.get("plotly_chart")[0].proto.spec)["data"][0][
                "gauge"
            ]

            self.assertEqual(gauge["steps"][0]["range"], [0, 20.0])
            self.assertEqual(gauge["steps"][1]["range"], [20.0, 40.0])
            self.assertEqual(gauge["steps"][2]["range"], [40.0, 100])
            self.assertEqual(gauge["threshold"]["value"], 40.0)

    def test_operator_can_select_an_earlier_scoring_run(self) -> None:
        """A scores directory holds one immutable object per run, newest last."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = build_fixture(root)
            runs = root / "scores"
            runs.mkdir()
            full = pd.read_csv(environment["FIRMAWARE_SCORES_URI"])
            full.to_csv(runs / "scores_20260730T000000000001Z_run-a.csv", index=False)
            full.head(3).to_csv(
                runs / "scores_20260731T000000000001Z_run-b.csv", index=False
            )
            environment["FIRMAWARE_SCORES_URI"] = str(runs)

            page = self.start(environment)
            page.radio[0].set_value("Fleet Overview").run()
            self.assertEqual(page.exception, [])
            self.assertEqual(len(page.dataframe[0].value), 3)  # newest run by default

            selector = self.picker(page, "Scoring run")
            self.assertEqual(len(selector.options), 2)
            selector.set_value(
                str(runs / "scores_20260730T000000000001Z_run-a.csv")
            ).run()
            self.assertEqual(page.exception, [])
            self.assertEqual(len(page.dataframe[0].value), 12)

    def test_page_never_writes_to_the_pipeline_locations(self) -> None:
        """The page is read-only, so rendering must not create or mutate files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            page = self.start(build_fixture(root))
            before = {
                path: path.stat().st_mtime_ns
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }

            page.radio[0].set_value("Fleet Overview").run()
            self.assertEqual(page.exception, [])

            after = {
                path: path.stat().st_mtime_ns
                for path in sorted(root.rglob("*"))
                if path.is_file()
            }
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
