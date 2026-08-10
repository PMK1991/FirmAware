from __future__ import annotations

import unittest
from pathlib import Path

import pandas as pd

from firmaware.schema import validate

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def test_container_is_non_root_and_uses_module_entrypoint(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("FROM python:3.11-slim AS builder", dockerfile)
        self.assertIn("FROM python:3.11-slim AS runtime", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn(
            'ENTRYPOINT ["python", "-m", "firmaware"]', dockerfile
        )

    def test_smoke_fixture_has_one_moxa_row_and_valid_scoring_contract(self) -> None:
        fixture = pd.read_csv(
            ROOT / "deploy" / "gcp" / "fixtures" / "upcoming_smoke.csv"
        )
        validate(fixture, mode="scoring")
        self.assertEqual(len(fixture), 5)
        self.assertEqual(int(fixture["vendor_name"].eq("Moxa").sum()), 1)

    def test_scores_role_can_create_and_read_but_not_delete(self) -> None:
        iam = (
            ROOT / "infra" / "gcp" / "modules" / "iam" / "main.tf"
        ).read_text(encoding="utf-8")
        scores_resource = iam.split(
            'resource "google_storage_bucket_iam_member" "jobs_scores_roles"'
        )[1].split(
            'resource "google_project_iam_member" "deployer_project_roles"'
        )[0]
        self.assertIn("roles/storage.objectCreator", scores_resource)
        self.assertIn("roles/storage.objectViewer", scores_resource)
        self.assertNotIn("roles/storage.objectAdmin", scores_resource)

    def test_prod_promotes_successful_dev_digest_without_rebuild(self) -> None:
        workflow = (
            ROOT / ".github" / "workflows" / "gcp-deploy-prod.yaml"
        ).read_text(encoding="utf-8")
        self.assertIn("environment: production", workflow)
        self.assertIn("deployments/live.json", workflow)
        self.assertIn("Promotion changed digest", workflow)
        self.assertNotIn("docker build", workflow)


if __name__ == "__main__":
    unittest.main()
