from __future__ import annotations

import json
import os
import re
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

SWITCH_PATTERN = re.compile(
    r'class="(fa-switch(?: active)?)"><span class="fa-switch-label">([^<]+)<'
)

# Restated from the page's contract so the assertion is independent of app.py.
FLAG_RULES = {
    "Major Version Change": lambda r: r["major_version_changed"] > 0,
    "Core System Touched": lambda r: r["kernel_touched"] or r["bootloader_touched"],
    "Emergency / No Window": lambda r: (
        r["deployment_type"] == "EMERGENCY" and not r["maintenance_window"]
    ),
    "High CVSS (>= 7.0)": lambda r: r["max_cvss_score"] >= 7.0,
    "Protocol Mismatch": lambda r: r["protocol_mismatch_flag"] != 0,
    "High Network Stress (>= 0.70)": lambda r: r["network_stress_score"] >= 0.70,
    "Repeat Failure Device": lambda r: r["past_failure_count"] >= 1,
    "Tier 1 Device": lambda r: r["fleet_tier"] == "TIER_1",
    "High Criticality Site": lambda r: r["site_criticality"] == "HIGH",
}

CLEAR_ROW = {
    "current_firmware": "2.2.0",
    "target_firmware": "2.3.1",
    "kernel_touched": 0,
    "bootloader_touched": 0,
    "deployment_type": "STANDARD",
    "maintenance_window": 1,
    "max_cvss_score": 3.0,
    "protocol_mismatch_flag": 0,
    "network_stress_score": 0.10,
    "past_failure_count": 0,
    "fleet_tier": "TIER_3",
    "site_criticality": "LOW",
}

RAISED_ROW = {
    "current_firmware": "2.2.0",
    "target_firmware": "5.0.1",
    "kernel_touched": 1,
    "bootloader_touched": 1,
    "deployment_type": "EMERGENCY",
    "maintenance_window": 0,
    "max_cvss_score": 9.6,
    "protocol_mismatch_flag": 1,
    "network_stress_score": 0.95,
    "past_failure_count": 3,
    "fleet_tier": "TIER_1",
    "site_criticality": "HIGH",
}


def build_fixture(directory: Path, threshold: float = 0.4) -> dict[str, str]:
    """Write the minimum score/artifact/input trio the page reads."""
    upcoming = make_training_frame(12).drop(columns=SCORING_ONLY_COLUMNS)

    # Pin one all-clear and one all-raised row so the flag panel is exercised at
    # both extremes regardless of how the synthetic generator evolves.
    for column, value in CLEAR_ROW.items():
        upcoming.loc[0, column] = value
    for column, value in RAISED_ROW.items():
        upcoming.loc[1, column] = value

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
    def start(self, environment: dict[str, str]) -> AppTest:
        """Keep the environment patched for the whole test.

        Every AppTest interaction re-executes the script, and the page reads its
        URIs at import time, so the patch must outlive the first run.
        """
        self.enterContext(patch.dict(os.environ, environment, clear=False))
        return AppTest.from_file(str(APP), default_timeout=180).run()

    @staticmethod
    def picker(page: AppTest, label: str):
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

    def test_risk_flag_panel_matches_the_underlying_deployment_data(self) -> None:
        """A panel with every switch off must mean no flags, not a failed render."""
        with tempfile.TemporaryDirectory() as directory:
            environment = build_fixture(Path(directory))
            page = self.start(environment)
            page.radio[0].set_value("Deployment Inspector").run()

            upcoming = pd.read_csv(
                Path(environment["FIRMAWARE_DATA_URI"]) / "upcoming_deployments.csv"
            )
            derived = derive_features(
                validate(upcoming, "scoring"), "scoring"
            ).reset_index()
            context = upcoming.merge(
                derived[["deployment_id", "major_version_changed"]],
                on="deployment_id",
                how="left",
            )

            seen_all_raised = False
            seen_all_clear = False
            for record in context.to_dict("records"):
                deployment = record["deployment_id"]
                expected = sorted(
                    label for label, rule in FLAG_RULES.items() if rule(record)
                )
                self.picker(page, "Select a deployment").set_value(deployment).run()
                self.assertEqual(page.exception, [])

                grid = next(
                    str(block.value)
                    for block in page.markdown
                    if 'class="fa-switch-grid"' in str(block.value)
                )
                switches = SWITCH_PATTERN.findall(grid)
                shown = sorted(
                    label for css, label in switches if "active" in css
                )

                self.assertEqual(len(switches), len(FLAG_RULES), deployment)
                self.assertEqual(shown, expected, deployment)

                headers = " ".join(
                    str(block.value)
                    for block in page.markdown
                    if "Risk Flag Panel" in str(block.value)
                )
                self.assertIn(
                    f"{len(expected)} of {len(FLAG_RULES)} raised", headers
                )

                seen_all_raised = seen_all_raised or len(expected) == len(FLAG_RULES)
                seen_all_clear = seen_all_clear or not expected

            self.assertTrue(seen_all_raised, "fixture never raises every flag")
            self.assertTrue(seen_all_clear, "fixture never leaves the panel clear")

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
