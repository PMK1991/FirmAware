"""Keep local disk behavior while adding lazy, generation-safe GCS I/O."""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from .schema import ContractViolation

GCS_SCHEME = "gs://"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def is_gcs_uri(uri: str | Path) -> bool:
    return str(uri).startswith(GCS_SCHEME)


def join_uri(base: str | Path, child: str) -> str:
    base_text = str(base)
    if is_gcs_uri(base_text):
        return f"{base_text.rstrip('/')}/{child.lstrip('/')}"
    return str(Path(base_text) / Path(child))


def _parse_gcs_uri(uri: str | Path) -> tuple[str, str]:
    text = str(uri)
    if not is_gcs_uri(text):
        raise ValueError(f"Not a GCS URI: {text}")
    bucket, separator, object_name = text[len(GCS_SCHEME) :].partition("/")
    if not bucket:
        raise ContractViolation(f"GCS URI has no bucket name: {text}")
    return bucket, object_name.rstrip("/") if separator else ""


def _storage_client() -> Any:
    try:
        from google.cloud import storage
    except ImportError as error:
        raise ContractViolation(
            "google-cloud-storage is required for gs:// paths"
        ) from error
    return storage.Client()


def read_csv(uri: str | Path, client: Any | None = None) -> pd.DataFrame:
    if not is_gcs_uri(uri):
        return pd.read_csv(Path(uri))
    bucket_name, object_name = _parse_gcs_uri(uri)
    if not object_name:
        raise ContractViolation(f"GCS CSV URI has no object name: {uri}")
    storage_client = client or _storage_client()
    payload = (
        storage_client.bucket(bucket_name)
        .blob(object_name)
        .download_as_bytes()
    )
    return pd.read_csv(io.BytesIO(payload))


def read_json(uri: str | Path, client: Any | None = None) -> dict[str, Any]:
    if not is_gcs_uri(uri):
        return json.loads(Path(uri).read_text(encoding="utf-8"))
    bucket_name, object_name = _parse_gcs_uri(uri)
    storage_client = client or _storage_client()
    payload = (
        storage_client.bucket(bucket_name)
        .blob(object_name)
        .download_as_bytes()
    )
    value = json.loads(payload.decode("utf-8"))
    if not isinstance(value, dict):
        raise ContractViolation(f"Expected JSON object at {uri}")
    return value


def _metadata_digest(metadata_path: Path) -> str:
    return hashlib.sha256(metadata_path.read_bytes()).hexdigest()


def publish_artifact_run(
    local_dir: str | Path,
    artifacts_uri: str | Path,
    run_id: str,
    promoted_at: str,
    promoted_by: str | None = None,
    client: Any | None = None,
) -> dict[str, str]:
    """Upload an immutable run before atomically moving the champion pointer."""
    if not is_gcs_uri(artifacts_uri):
        raise ValueError("Artifact run publication requires a gs:// URI")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ContractViolation(f"Artifact run_id is not path-safe: {run_id!r}")
    source = Path(local_dir)
    metadata_path = source / "metadata.json"
    if not metadata_path.is_file():
        raise ContractViolation("Training output is missing metadata.json")

    bucket_name, root_prefix = _parse_gcs_uri(artifacts_uri)
    run_prefix = "/".join(
        part for part in (root_prefix, "runs", run_id) if part
    )
    storage_client = client or _storage_client()
    bucket = storage_client.bucket(bucket_name)
    if next(iter(bucket.list_blobs(prefix=f"{run_prefix}/", max_results=1)), None):
        raise ContractViolation(
            f"Immutable artifact run already exists: gs://{bucket_name}/{run_prefix}"
        )

    files = sorted(path for path in source.rglob("*") if path.is_file())
    for path in files:
        relative = path.relative_to(source).as_posix()
        object_name = f"{run_prefix}/{relative}"
        content_type = mimetypes.guess_type(path.name)[0]
        bucket.blob(object_name).upload_from_filename(
            str(path),
            content_type=content_type,
            if_generation_match=0,
        )

    pointer_name = "/".join(
        part for part in (root_prefix, "champion.json") if part
    )
    current = bucket.get_blob(pointer_name)
    generation = int(current.generation) if current is not None else 0
    pointer = {
        "run_id": run_id,
        "digest_of_metadata": _metadata_digest(metadata_path),
        "promoted_at": promoted_at,
        "promoted_by": promoted_by
        or os.getenv("FIRMAWARE_PROMOTED_BY", "firmaware-train"),
    }
    bucket.blob(pointer_name).upload_from_string(
        json.dumps(pointer, indent=2, sort_keys=True) + "\n",
        content_type="application/json",
        if_generation_match=generation,
    )
    return pointer


@contextmanager
def materialize_artifacts(
    artifacts_uri: str | Path, client: Any | None = None
) -> Iterator[Path]:
    """Download the champion run and verify its metadata before deserialization."""
    if not is_gcs_uri(artifacts_uri):
        yield Path(artifacts_uri)
        return

    storage_client = client or _storage_client()
    bucket_name, root_prefix = _parse_gcs_uri(artifacts_uri)
    bucket = storage_client.bucket(bucket_name)
    pointer_name = "/".join(
        part for part in (root_prefix, "champion.json") if part
    )
    pointer_blob = bucket.get_blob(pointer_name)
    if pointer_blob is None:
        raise ContractViolation(
            f"Champion pointer is missing: gs://{bucket_name}/{pointer_name}"
        )
    pointer = json.loads(pointer_blob.download_as_bytes().decode("utf-8"))
    run_id = pointer.get("run_id")
    expected_digest = pointer.get("digest_of_metadata")
    if not isinstance(run_id, str) or not run_id:
        raise ContractViolation("Champion pointer has no valid run_id")
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ContractViolation(
            f"Champion pointer run_id is not path-safe: {run_id!r}"
        )
    if not isinstance(expected_digest, str) or not expected_digest:
        raise ContractViolation(
            "Champion pointer has no valid digest_of_metadata"
        )

    run_prefix = "/".join(
        part for part in (root_prefix, "runs", run_id) if part
    )
    with tempfile.TemporaryDirectory(prefix="firmaware-artifacts-") as directory:
        target = Path(directory)
        blobs = list(bucket.list_blobs(prefix=f"{run_prefix}/"))
        if not blobs:
            raise ContractViolation(
                f"Champion artifact run is missing: gs://{bucket_name}/{run_prefix}"
            )
        for blob in blobs:
            relative = blob.name[len(run_prefix) + 1 :]
            if not relative:
                continue
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ContractViolation(
                    f"Unsafe object path in champion run {run_id}: {relative}"
                )
            destination = target / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            blob.download_to_filename(str(destination))
        metadata_path = target / "metadata.json"
        if not metadata_path.is_file():
            raise ContractViolation(
                f"Champion run {run_id} is missing metadata.json"
            )
        actual_digest = _metadata_digest(metadata_path)
        if actual_digest != expected_digest:
            raise ContractViolation(
                f"Champion metadata digest mismatch for run {run_id}: "
                f"expected {expected_digest}, found {actual_digest}"
            )
        yield target


def list_scores_uris(
    scores_uri: str | Path, client: Any | None = None
) -> list[str]:
    """List score objects oldest first so callers can offer run selection.

    Object names embed the scoring timestamp, so name order is run order in both
    stores; sorting on names keeps local and GCS listings identical.
    """
    if not is_gcs_uri(scores_uri):
        path = Path(scores_uri)
        if path.is_file():
            return [str(path)]
        if not path.is_dir():
            return []
        return [
            str(item)
            for item in sorted(
                (item for item in path.glob("*.csv") if item.is_file()),
                key=lambda item: item.name,
            )
        ]

    bucket_name, prefix = _parse_gcs_uri(scores_uri)
    storage_client = client or _storage_client()
    bucket = storage_client.bucket(bucket_name)
    names = sorted(
        blob.name
        for blob in bucket.list_blobs(prefix=f"{prefix}/" if prefix else "")
        if blob.name.endswith(".csv")
    )
    return [f"gs://{bucket_name}/{name}" for name in names]


def latest_scores_uri(
    scores_uri: str | Path, client: Any | None = None
) -> str | None:
    """Return the newest score object so readers never depend on run ordering."""
    candidates = list_scores_uris(scores_uri, client=client)
    return candidates[-1] if candidates else None


def write_scores(
    scores: pd.DataFrame,
    output_uri: str | Path,
    scored_at: str,
    run_id: str,
    expected_columns: list[str],
    client: Any | None = None,
) -> str:
    """Append locally or create one immutable GCS object per scoring run."""
    if not is_gcs_uri(output_uri):
        output_path = Path(output_uri)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists() and output_path.stat().st_size:
            existing_columns = pd.read_csv(output_path, nrows=0).columns.tolist()
            if existing_columns != expected_columns:
                raise ContractViolation(
                    "Existing scores file has incompatible columns: "
                    f"{existing_columns}"
                )
            scores.to_csv(
                output_path,
                mode="a",
                header=False,
                index=False,
                float_format="%.4f",
            )
        else:
            scores.to_csv(
                output_path,
                mode="a",
                header=True,
                index=False,
                float_format="%.4f",
            )
        return str(output_path)

    bucket_name, prefix = _parse_gcs_uri(output_uri)
    safe_timestamp = "".join(
        character for character in scored_at if character.isalnum()
    )
    safe_run_id = "".join(
        character for character in run_id if character.isalnum() or character in "-_"
    )
    filename = f"scores_{safe_timestamp}_{safe_run_id}.csv"
    object_name = "/".join(part for part in (prefix, filename) if part)
    payload = scores.to_csv(index=False, float_format="%.4f")
    storage_client = client or _storage_client()
    storage_client.bucket(bucket_name).blob(object_name).upload_from_string(
        payload,
        content_type="text/csv",
        if_generation_match=0,
    )
    return f"gs://{bucket_name}/{object_name}"
