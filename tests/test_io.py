from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import joblib
import pandas as pd
from sklearn.linear_model import LogisticRegression

from firmaware.cli import main
from firmaware.features import LABEL_COLUMN, derive_features, model_inputs
from firmaware.io import (
    join_uri,
    latest_scores_uri,
    list_scores_uris,
    materialize_artifacts,
    publish_artifact_run,
    read_csv,
    write_scores,
)
from firmaware.predict import predict
from firmaware.schema import ContractViolation, validate
from firmaware.transform import Preprocessor
from tests.test_schema import make_training_frame


class FakeBlob:
    def __init__(self, bucket: FakeBucket, name: str) -> None:
        self.bucket = bucket
        self.name = name

    @property
    def generation(self) -> int:
        return self.bucket.objects[self.name]["generation"]

    def upload_from_filename(
        self,
        filename: str,
        content_type: str | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        self.upload_from_string(
            Path(filename).read_bytes(),
            content_type=content_type,
            if_generation_match=if_generation_match,
        )

    def upload_from_string(
        self,
        payload: str | bytes,
        content_type: str | None = None,
        if_generation_match: int | None = None,
    ) -> None:
        current = self.bucket.objects.get(self.name)
        current_generation = current["generation"] if current else 0
        if (
            if_generation_match is not None
            and if_generation_match != current_generation
        ):
            raise RuntimeError("generation mismatch")
        value = payload.encode("utf-8") if isinstance(payload, str) else payload
        self.bucket.objects[self.name] = {
            "payload": value,
            "content_type": content_type,
            "generation": current_generation + 1,
        }

    def download_as_bytes(self) -> bytes:
        return self.bucket.objects[self.name]["payload"]

    def download_to_filename(self, filename: str) -> None:
        Path(filename).write_bytes(self.download_as_bytes())


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)

    def get_blob(self, name: str) -> FakeBlob | None:
        return FakeBlob(self, name) if name in self.objects else None

    def list_blobs(
        self, prefix: str = "", max_results: int | None = None
    ) -> list[FakeBlob]:
        names = sorted(name for name in self.objects if name.startswith(prefix))
        if max_results is not None:
            names = names[:max_results]
        return [FakeBlob(self, name) for name in names]


class FakeStorageClient:
    def __init__(self) -> None:
        self.buckets: dict[str, FakeBucket] = {}

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket())


class IoTests(unittest.TestCase):
    def test_uri_join_and_local_csv_remain_local(self) -> None:
        self.assertEqual(
            join_uri("gs://bucket/prefix", "input.csv"),
            "gs://bucket/prefix/input.csv",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "input.csv"
            pd.DataFrame({"value": [1, 2]}).to_csv(path, index=False)
            self.assertEqual(read_csv(path)["value"].tolist(), [1, 2])

    def test_cli_uses_environment_data_uri_when_input_is_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            scoring = make_training_frame(5).drop(
                columns=[
                    "deployment_outcome",
                    "time_to_failure_hours",
                    "rollback_required",
                ]
            )
            scoring.to_csv(
                Path(directory) / "upcoming_deployments.csv", index=False
            )
            with patch.dict(
                os.environ, {"FIRMAWARE_DATA_URI": directory}, clear=False
            ):
                self.assertEqual(
                    main(["validate", "--mode", "scoring"]), 0
                )

    def test_gcs_artifact_runs_are_immutable_and_pointer_is_verified(self) -> None:
        client = FakeStorageClient()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)
            (source / "metadata.json").write_text(
                '{"timestamp":"2026-07-22T00:00:00Z"}\n',
                encoding="utf-8",
            )
            (source / "model.joblib").write_bytes(b"model")

            pointer = publish_artifact_run(
                source,
                "gs://artifacts/model",
                "run-1",
                "2026-07-22T00:00:00Z",
                promoted_by="test",
                client=client,
            )
            self.assertEqual(pointer["run_id"], "run-1")
            with self.assertRaisesRegex(
                ContractViolation, "already exists"
            ):
                publish_artifact_run(
                    source,
                    "gs://artifacts/model",
                    "run-1",
                    "2026-07-22T00:00:00Z",
                    client=client,
                )
            with self.assertRaisesRegex(ContractViolation, "path-safe"):
                publish_artifact_run(
                    source,
                    "gs://artifacts/model",
                    "../unsafe",
                    "2026-07-22T00:00:00Z",
                    client=client,
                )

            with materialize_artifacts(
                "gs://artifacts/model", client=client
            ) as materialized:
                self.assertEqual(
                    json.loads(
                        (materialized / "metadata.json").read_text(
                            encoding="utf-8"
                        )
                    )["timestamp"],
                    "2026-07-22T00:00:00Z",
                )

            bucket = client.bucket("artifacts")
            bucket.objects["model/runs/run-1/metadata.json"]["payload"] = b"{}"
            with self.assertRaisesRegex(
                ContractViolation, "digest mismatch"
            ), materialize_artifacts(
                "gs://artifacts/model", client=client
            ):
                pass

    def test_gcs_scores_create_one_object_per_run(self) -> None:
        client = FakeStorageClient()
        scores = pd.DataFrame(
            {
                "deployment_id": ["d-1"],
                "risk_probability": [0.5],
            }
        )
        columns = scores.columns.tolist()
        first = write_scores(
            scores,
            "gs://scores/scores",
            "2026-07-22T00:00:00.000001Z",
            "run-1",
            columns,
            client=client,
        )
        second = write_scores(
            scores,
            "gs://scores/scores",
            "2026-07-22T00:00:01.000001Z",
            "run-1",
            columns,
            client=client,
        )
        self.assertNotEqual(first, second)
        self.assertEqual(len(client.bucket("scores").objects), 2)
        for stored in client.bucket("scores").objects.values():
            self.assertEqual(stored["generation"], 1)
        with self.assertRaises(RuntimeError):
            write_scores(
                scores,
                "gs://scores/scores",
                "2026-07-22T00:00:01.000001Z",
                "run-1",
                columns,
                client=client,
            )

    def test_predict_reads_champion_and_writes_immutable_gcs_score(self) -> None:
        client = FakeStorageClient()
        training = make_training_frame(40)
        featured = derive_features(validate(training, "training"), "training")
        inputs = model_inputs(featured)
        preprocessor = Preprocessor().fit(inputs.iloc[:32])
        matrix, _ = preprocessor.apply(inputs.iloc[:32])
        model = LogisticRegression(random_state=42).fit(
            matrix.to_numpy(),
            featured[LABEL_COLUMN].iloc[:32].to_numpy(),
        )
        scoring = training.iloc[[32]].drop(
            columns=[
                "deployment_outcome",
                "time_to_failure_hours",
                "rollback_required",
            ]
        )

        with tempfile.TemporaryDirectory() as directory:
            artifacts = Path(directory)
            joblib.dump(model, artifacts / "model.joblib")
            preprocessor.save(artifacts / "preprocessor.joblib")
            (artifacts / "metadata.json").write_text(
                json.dumps(
                    {
                        "spec_version": "1.0",
                        "threshold": 0.5,
                        "timestamp": "2026-07-22T00:00:00Z",
                        "artifact_run_id": "run-cloud",
                        "mlflow": {"run_id": "run-cloud"},
                    }
                ),
                encoding="utf-8",
            )
            publish_artifact_run(
                artifacts,
                "gs://artifact-bucket",
                "run-cloud",
                "2026-07-22T00:00:00Z",
                client=client,
            )

        client.bucket("data-bucket").blob(
            "upcoming_deployments.csv"
        ).upload_from_string(scoring.to_csv(index=False))
        with patch("firmaware.io._storage_client", return_value=client):
            scores = predict(
                "gs://data-bucket/upcoming_deployments.csv",
                artifacts_dir="gs://artifact-bucket",
                output_path="gs://score-bucket/scores",
            )

        self.assertEqual(len(scores), 1)
        objects = client.bucket("score-bucket").objects
        self.assertEqual(len(objects), 1)
        self.assertTrue(next(iter(objects)).startswith("scores/scores_"))

    def test_gcs_score_runs_are_listed_oldest_first(self) -> None:
        client = FakeStorageClient()
        scores = pd.DataFrame(
            {"deployment_id": ["d-1"], "risk_probability": [0.5]}
        )
        columns = scores.columns.tolist()
        written = [
            write_scores(
                scores,
                "gs://score-bucket/scores",
                scored_at,
                "run-1",
                columns,
                client=client,
            )
            for scored_at in (
                "2026-07-22T00:00:00.000001Z",
                "2026-07-23T00:00:00.000001Z",
                "2026-07-24T00:00:00.000001Z",
            )
        ]
        client.bucket("score-bucket").blob("scores/notes.txt").upload_from_string(
            "ignored"
        )

        listed = list_scores_uris("gs://score-bucket/scores", client=client)

        self.assertEqual(listed, written)
        self.assertEqual(
            latest_scores_uri("gs://score-bucket/scores", client=client),
            written[-1],
        )

    def test_missing_scores_location_yields_no_runs(self) -> None:
        client = FakeStorageClient()
        self.assertEqual(
            list_scores_uris("gs://score-bucket/scores", client=client), []
        )
        self.assertIsNone(
            latest_scores_uri("gs://score-bucket/scores", client=client)
        )
        with tempfile.TemporaryDirectory() as directory:
            absent = Path(directory) / "scores.csv"
            self.assertEqual(list_scores_uris(absent), [])
            self.assertIsNone(latest_scores_uri(absent))
            absent.write_text("deployment_id\nd-1\n", encoding="utf-8")
            self.assertEqual(latest_scores_uri(absent), str(absent))

    def test_local_run_order_follows_names_not_write_order(self) -> None:
        """Names embed the scoring timestamp, so copy order must not reorder runs."""
        with tempfile.TemporaryDirectory() as directory:
            scores_dir = Path(directory)
            newest = scores_dir / "scores_20260731T000000000001Z_run-b.csv"
            oldest = scores_dir / "scores_20260730T000000000001Z_run-a.csv"
            newest.write_text("deployment_id\nd-2\n", encoding="utf-8")
            oldest.write_text("deployment_id\nd-1\n", encoding="utf-8")

            self.assertEqual(
                list_scores_uris(scores_dir), [str(oldest), str(newest)]
            )
            self.assertEqual(latest_scores_uri(scores_dir), str(newest))


if __name__ == "__main__":
    unittest.main()
