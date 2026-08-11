"""ADLS Gen2 behaviour of the URI shim, exercised against a fake SDK.

The real azure-storage-file-datalake package is never imported here. Every test
injects a fake client through the same ``client`` parameter the GCS tests use,
which is also the proof that the Azure import stays lazy: these pass whether or
not the SDK is installed.
"""

from __future__ import annotations

import json
import unittest
from typing import Any

import pandas as pd

from firmaware.io import (
    is_abfss_uri,
    is_gcs_uri,
    is_remote_uri,
    join_uri,
    latest_scores_uri,
    list_scores_uris,
    materialize_artifacts,
    publish_artifact_run,
    read_csv,
    read_json,
    write_scores,
)
from firmaware.schema import ContractViolation

ACCOUNT = "stfirmawaredev"
FILESYSTEM = "scores"
BASE = f"abfss://{FILESYSTEM}@{ACCOUNT}.dfs.core.windows.net"


class FakeDownload:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def readall(self) -> bytes:
        return self._payload


class FakeFileClient:
    def __init__(self, filesystem: FakeFileSystemClient, path: str) -> None:
        self.filesystem = filesystem
        self.path = path

    def download_file(self) -> FakeDownload:
        try:
            return FakeDownload(self.filesystem.files[self.path])
        except KeyError as error:
            raise FileNotFoundError(self.path) from error

    def upload_data(self, data: bytes, overwrite: bool = False) -> None:
        # Deliberately absent from the write path under test. The real SDK's
        # upload_data(overwrite=False) appends to a path it never creates, so a
        # fake that quietly did the right thing here is exactly what hid a live
        # PathNotFound. Kept only so an accidental reintroduction is loud.
        raise AssertionError(
            "write_scores must create, append and flush explicitly: "
            "upload_data(overwrite=False) does not create the file on ADLS"
        )

    def create_file(self, match_condition: Any = None) -> None:
        # Compared by name so this fake still needs no azure package, which is
        # what keeps these tests honest about the import staying lazy.
        if getattr(match_condition, "name", None) != "IfMissing":
            raise AssertionError(
                "the scores write must be create-if-missing; without it a replay "
                "would silently replace immutable evidence"
            )
        if self.path in self.filesystem.files:
            # The service answers If-None-Match: * with 409. FileExistsError
            # stands in for the SDK's ResourceExistsError, which this fake
            # cannot import.
            raise FileExistsError(self.path)
        self.filesystem.files[self.path] = b""

    def append_data(self, data: bytes, offset: int, length: int) -> None:
        if self.path not in self.filesystem.files:
            # What the real service returns when nothing created the path first.
            raise FileNotFoundError(self.path)
        self.filesystem.files[self.path] = (
            self.filesystem.files[self.path][:offset] + data
        )

    def flush_data(self, offset: int) -> None:
        if self.path not in self.filesystem.files:
            raise FileNotFoundError(self.path)
        self.filesystem.files[self.path] = self.filesystem.files[self.path][:offset]


class FakePath:
    def __init__(self, name: str, is_directory: bool = False) -> None:
        self.name = name
        self.is_directory = is_directory


class FakeFileSystemClient:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files

    def get_file_client(self, path: str) -> FakeFileClient:
        return FakeFileClient(self, path)

    def get_paths(self, path: str | None = None, recursive: bool = True):
        prefix = f"{path.rstrip('/')}/" if path else ""
        for name in sorted(self.files):
            if not name.startswith(prefix):
                continue
            remainder = name[len(prefix) :]
            if not recursive and "/" in remainder:
                continue
            yield FakePath(name)


class FakeDataLakeServiceClient:
    def __init__(self, filesystems: dict[str, dict[str, bytes]]) -> None:
        self.filesystems = filesystems

    def get_file_system_client(self, name: str) -> FakeFileSystemClient:
        return FakeFileSystemClient(self.filesystems.setdefault(name, {}))


def make_client(files: dict[str, bytes] | None = None) -> FakeDataLakeServiceClient:
    return FakeDataLakeServiceClient({FILESYSTEM: dict(files or {})})


class UriRecognitionTests(unittest.TestCase):
    def test_abfss_is_recognised_without_disturbing_gcs(self) -> None:
        self.assertTrue(is_abfss_uri(BASE))
        self.assertTrue(is_remote_uri(BASE))
        self.assertFalse(is_gcs_uri(BASE))
        self.assertTrue(is_gcs_uri("gs://bucket/path"))
        self.assertFalse(is_abfss_uri("gs://bucket/path"))
        self.assertFalse(is_remote_uri("outputs/scores.csv"))

    def test_join_uri_keeps_the_scheme_rather_than_using_os_separators(self) -> None:
        self.assertEqual(join_uri(BASE, "runs/1"), f"{BASE}/runs/1")
        self.assertEqual(join_uri(f"{BASE}/", "/runs/1"), f"{BASE}/runs/1")

    def test_a_malformed_adls_uri_names_the_expected_shape(self) -> None:
        for bad in (
            "abfss://no-account",
            "abfss://fs@account.blob.core.windows.net/path",
            "abfss://@account.dfs.core.windows.net/path",
        ):
            with self.assertRaises(ContractViolation) as caught:
                read_csv(bad, client=make_client())
            self.assertIn("abfss://filesystem@account", str(caught.exception))


class ReadTests(unittest.TestCase):
    def test_read_csv_downloads_the_object(self) -> None:
        client = make_client({"input/upcoming.csv": b"a,b\n1,2\n"})
        frame = read_csv(f"{BASE}/input/upcoming.csv", client=client)
        self.assertEqual(frame.to_dict(orient="records"), [{"a": 1, "b": 2}])

    def test_read_json_returns_the_object(self) -> None:
        client = make_client({"artifacts/metadata.json": b'{"threshold": 0.18}'})
        self.assertEqual(
            read_json(f"{BASE}/artifacts/metadata.json", client=client),
            {"threshold": 0.18},
        )

    def test_read_json_rejects_a_non_object_document(self) -> None:
        client = make_client({"artifacts/metadata.json": b"[1, 2]"})
        with self.assertRaises(ContractViolation):
            read_json(f"{BASE}/artifacts/metadata.json", client=client)

    def test_a_uri_with_no_file_path_is_rejected(self) -> None:
        with self.assertRaises(ContractViolation) as caught:
            read_csv(BASE, client=make_client())
        self.assertIn("no file path", str(caught.exception))


class WriteScoresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.scores = pd.DataFrame({"deployment_id": ["UPC_1"], "risk": [0.5]})
        self.columns = ["deployment_id", "risk"]

    def test_each_run_creates_its_own_object(self) -> None:
        client = make_client()
        written = write_scores(
            self.scores,
            f"{BASE}/scores",
            scored_at="2026-08-10T09:00:00",
            run_id="run-1",
            expected_columns=self.columns,
            client=client,
        )
        self.assertEqual(
            written,
            f"{BASE}/scores/scores_20260810T090000_run-1.csv",
        )
        stored = client.filesystems[FILESYSTEM]
        self.assertEqual(len(stored), 1)
        self.assertIn(b"deployment_id,risk", next(iter(stored.values())))

    def test_a_replayed_run_cannot_overwrite_the_evidence(self) -> None:
        client = make_client()
        arguments = {
            "scored_at": "2026-08-10T09:00:00",
            "run_id": "run-1",
            "expected_columns": self.columns,
            "client": client,
        }
        write_scores(self.scores, f"{BASE}/scores", **arguments)
        with self.assertRaises(FileExistsError):
            write_scores(self.scores, f"{BASE}/scores", **arguments)

    def test_scores_written_at_the_filesystem_root_need_no_prefix(self) -> None:
        client = make_client()
        written = write_scores(
            self.scores,
            BASE,
            scored_at="2026-08-10T09:00:00",
            run_id="run-1",
            expected_columns=self.columns,
            client=client,
        )
        self.assertEqual(written, f"{BASE}/scores_20260810T090000_run-1.csv")


class ListScoresTests(unittest.TestCase):
    def test_objects_are_listed_oldest_first_and_non_csv_ignored(self) -> None:
        client = make_client(
            {
                "scores/scores_20260810T090000_b.csv": b"x\n",
                "scores/scores_20260801T090000_a.csv": b"x\n",
                "scores/_manifest.json": b"{}",
                "scores/nested/scores_20260901T090000_c.csv": b"x\n",
            }
        )
        listed = list_scores_uris(f"{BASE}/scores", client=client)
        self.assertEqual(
            listed,
            [
                f"{BASE}/scores/scores_20260801T090000_a.csv",
                f"{BASE}/scores/scores_20260810T090000_b.csv",
            ],
        )
        self.assertEqual(latest_scores_uri(f"{BASE}/scores", client=client), listed[-1])

    def test_an_empty_prefix_lists_nothing_rather_than_failing(self) -> None:
        self.assertEqual(list_scores_uris(f"{BASE}/scores", client=make_client()), [])
        self.assertIsNone(latest_scores_uri(f"{BASE}/scores", client=make_client()))


class MaterializeArtifactsTests(unittest.TestCase):
    def test_the_prefix_is_downloaded_with_no_champion_pointer(self) -> None:
        client = make_client(
            {
                "artifacts/metadata.json": json.dumps({"threshold": 0.18}).encode(),
                "artifacts/model.joblib": b"binary",
                "artifacts/evaluation/test_metrics.json": b"{}",
            }
        )
        with materialize_artifacts(f"{BASE}/artifacts", client=client) as local:
            self.assertEqual(
                json.loads((local / "metadata.json").read_text()), {"threshold": 0.18}
            )
            self.assertEqual((local / "model.joblib").read_bytes(), b"binary")
            self.assertTrue((local / "evaluation" / "test_metrics.json").is_file())

    def test_a_run_without_metadata_is_refused(self) -> None:
        client = make_client({"artifacts/model.joblib": b"binary"})
        with (
            self.assertRaises(ContractViolation) as caught,
            materialize_artifacts(f"{BASE}/artifacts", client=client),
        ):
            pass
        self.assertIn("metadata.json", str(caught.exception))

    def test_an_empty_prefix_is_refused(self) -> None:
        with (
            self.assertRaises(ContractViolation) as caught,
            materialize_artifacts(f"{BASE}/artifacts", client=make_client()),
        ):
            pass
        self.assertIn("No artifacts found", str(caught.exception))


class PublishArtifactRunTests(unittest.TestCase):
    def test_publication_points_at_the_registry_instead_of_a_pointer(self) -> None:
        with self.assertRaises(ContractViolation) as caught:
            publish_artifact_run(".", f"{BASE}/artifacts", "run-1", "2026-08-10")
        self.assertIn("model registry", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
