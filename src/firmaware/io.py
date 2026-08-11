"""Keep local disk behavior while adding lazy, conflict-safe cloud object I/O.

Two remote schemes are understood, and neither is imported until a URI actually
uses it, so a local run never pays for a cloud SDK:

    gs://bucket/path                                    Google Cloud Storage
    abfss://filesystem@account.dfs.core.windows.net/p   Azure Data Lake Gen2

Both remote writers create objects rather than overwriting them, which is what
keeps the scores store append-only from the client side as well as the
platform side.
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import re
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any

import pandas as pd

from .schema import ContractViolation

GCS_SCHEME = "gs://"
ABFSS_SCHEME = "abfss://"
RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
ABFSS_PATTERN = re.compile(
    r"^abfss://(?P<filesystem>[^/@]+)@(?P<account>[^/@.]+)"
    r"\.dfs\.core\.windows\.net(?:/(?P<path>.*))?$"
)


def is_gcs_uri(uri: str | Path) -> bool:
    return str(uri).startswith(GCS_SCHEME)


def is_abfss_uri(uri: str | Path) -> bool:
    return str(uri).startswith(ABFSS_SCHEME)


def is_remote_uri(uri: str | Path) -> bool:
    return is_gcs_uri(uri) or is_abfss_uri(uri)


def join_uri(base: str | Path, child: str) -> str:
    base_text = str(base)
    if is_remote_uri(base_text):
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


def _parse_abfss_uri(uri: str | Path) -> tuple[str, str, str]:
    """Split abfss://filesystem@account.dfs.core.windows.net/path into parts."""
    text = str(uri)
    match = ABFSS_PATTERN.match(text)
    if match is None:
        raise ContractViolation(
            "Not a well-formed ADLS Gen2 URI "
            f"(expected abfss://filesystem@account.dfs.core.windows.net/path): {text}"
        )
    return (
        match.group("account"),
        match.group("filesystem"),
        (match.group("path") or "").strip("/"),
    )


def _abfss_uri(account: str, filesystem: str, path: str) -> str:
    return f"{ABFSS_SCHEME}{filesystem}@{account}.dfs.core.windows.net/{path}"


def _datalake_client(account: str) -> Any:
    """Authenticate with the ambient managed identity, never with a shared key.

    The storage accounts this talks to have key access disabled, so
    DefaultAzureCredential is the only way in: a managed identity on Azure, and
    a developer's own AAD login off it.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.storage.filedatalake import DataLakeServiceClient
    except ImportError as error:
        raise ContractViolation(
            "azure-storage-file-datalake and azure-identity are required "
            "for abfss:// paths"
        ) from error
    return DataLakeServiceClient(
        account_url=f"https://{account}.dfs.core.windows.net",
        credential=DefaultAzureCredential(),
    )


def _abfss_file(uri: str | Path, client: Any | None = None) -> Any:
    account, filesystem, path = _parse_abfss_uri(uri)
    if not path:
        raise ContractViolation(f"ADLS URI has no file path: {uri}")
    service = client or _datalake_client(account)
    return service.get_file_system_client(filesystem).get_file_client(path)


def _abfss_download(uri: str | Path, client: Any | None = None) -> bytes:
    return _abfss_file(uri, client).download_file().readall()


def read_csv(uri: str | Path, client: Any | None = None) -> pd.DataFrame:
    if is_abfss_uri(uri):
        return pd.read_csv(io.BytesIO(_abfss_download(uri, client)))
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
    if is_abfss_uri(uri):
        value = json.loads(_abfss_download(uri, client).decode("utf-8"))
        if not isinstance(value, dict):
            raise ContractViolation(f"Expected JSON object at {uri}")
        return value
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
    if is_abfss_uri(artifacts_uri):
        raise ContractViolation(
            "Artifact runs are not published to ADLS: on Azure the Azure ML "
            "model registry versions the run, so the champion pointer has no "
            "counterpart. Register the model instead."
        )
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
def _materialize_abfss_artifacts(
    artifacts_uri: str | Path, client: Any | None = None
) -> Iterator[Path]:
    """Download an artifact prefix from ADLS with no champion-pointer hop.

    GCS needs champion.json because nothing else records which run is live.
    Azure ML's model registry already does, and it does it with versions and
    tags, so the pointer would be a second source of truth. The prefix given
    here is therefore the run, and metadata.json is still required so a
    half-uploaded run cannot be deserialized.
    """
    account, filesystem, prefix = _parse_abfss_uri(artifacts_uri)
    service = client or _datalake_client(account)
    filesystem_client = service.get_file_system_client(filesystem)
    with tempfile.TemporaryDirectory(prefix="firmaware-artifacts-") as directory:
        target = Path(directory)
        found = False
        for entry in filesystem_client.get_paths(path=prefix or None, recursive=True):
            if getattr(entry, "is_directory", False):
                continue
            relative = entry.name[len(prefix) :].lstrip("/") if prefix else entry.name
            if not relative:
                continue
            relative_path = Path(relative)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                raise ContractViolation(
                    f"Unsafe object path under {artifacts_uri}: {relative}"
                )
            destination = target / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            payload = (
                filesystem_client.get_file_client(entry.name).download_file().readall()
            )
            destination.write_bytes(payload)
            found = True
        if not found:
            raise ContractViolation(f"No artifacts found under {artifacts_uri}")
        if not (target / "metadata.json").is_file():
            raise ContractViolation(
                f"Artifact run under {artifacts_uri} is missing metadata.json"
            )
        yield target


@contextmanager
def materialize_artifacts(
    artifacts_uri: str | Path, client: Any | None = None
) -> Iterator[Path]:
    """Download the champion run and verify its metadata before deserialization."""
    if is_abfss_uri(artifacts_uri):
        with _materialize_abfss_artifacts(artifacts_uri, client) as target:
            yield target
        return
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
    if is_abfss_uri(scores_uri):
        account, filesystem, prefix = _parse_abfss_uri(scores_uri)
        service = client or _datalake_client(account)
        filesystem_client = service.get_file_system_client(filesystem)
        names = sorted(
            entry.name
            for entry in filesystem_client.get_paths(
                path=prefix or None, recursive=False
            )
            if not getattr(entry, "is_directory", False)
            and entry.name.endswith(".csv")
        )
        return [_abfss_uri(account, filesystem, name) for name in names]

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


def _scores_object_name(scored_at: str, run_id: str) -> str:
    """One object per scoring run, named so lexical order is chronological."""
    safe_timestamp = "".join(
        character for character in scored_at if character.isalnum()
    )
    safe_run_id = "".join(
        character for character in run_id if character.isalnum() or character in "-_"
    )
    return f"scores_{safe_timestamp}_{safe_run_id}.csv"


def _if_missing() -> Any:
    """The create-if-missing condition, resolved without importing azure eagerly.

    `MatchConditions.IfMissing` is compared by identity inside the SDK, so the
    real enum member has to be used whenever the SDK is present -- a duck-typed
    stand-in would fail that comparison, the condition header would be dropped,
    and the create would silently become an overwrite.

    The fallback therefore exists for exactly one case: a caller that injected
    its own client, which is how the tests prove the azure import stays lazy.
    It is unreachable on Azure, because azure-core is a dependency of both
    azure-identity and azure-storage-file-datalake.
    """
    try:
        from azure.core import MatchConditions
    except ImportError:
        return Enum("MatchConditions", ["IfMissing"]).IfMissing
    return MatchConditions.IfMissing


def _create_append_flush(file_client: Any, payload: bytes) -> None:
    """Write one score object with create-if-missing, append, flush.

    Not `upload_data(overwrite=False)`, which is what this used to be and which
    does not do what its name suggests on ADLS: with `overwrite=False` the SDK
    appends to a path it never creates, so a brand-new score object fails with
    `PathNotFound`. The old form only ever looked correct because the test double
    implemented the intended semantics rather than the SDK's.

    The three-call form is also the only shape the scores container accepts.
    It carries a time-based immutability policy with protected append writes,
    which permits append-style writes and refuses whole-blob uploads outright --
    a single `Put Blob` of a file that does not exist yet still comes back 409
    "blob is immutable due to a policy".

    `IfMissing` sends `If-None-Match: *`, so the create is the ADLS counterpart
    of GCS's `if_generation_match=0`: a re-run cannot quietly replace the
    evidence of the previous one, and finds out at once rather than after
    writing.
    """
    file_client.create_file(match_condition=_if_missing())
    file_client.append_data(payload, offset=0, length=len(payload))
    file_client.flush_data(len(payload))


def write_scores(
    scores: pd.DataFrame,
    output_uri: str | Path,
    scored_at: str,
    run_id: str,
    expected_columns: list[str],
    client: Any | None = None,
) -> str:
    """Append locally, or create one immutable object per run in the cloud."""
    if is_abfss_uri(output_uri):
        account, filesystem, prefix = _parse_abfss_uri(output_uri)
        name = "/".join(
            part for part in (prefix, _scores_object_name(scored_at, run_id)) if part
        )
        service = client or _datalake_client(account)
        file_client = service.get_file_system_client(filesystem).get_file_client(name)
        payload = scores.to_csv(index=False, float_format="%.4f").encode("utf-8")
        _create_append_flush(file_client, payload)
        return _abfss_uri(account, filesystem, name)

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
    object_name = "/".join(
        part for part in (prefix, _scores_object_name(scored_at, run_id)) if part
    )
    payload = scores.to_csv(index=False, float_format="%.4f")
    storage_client = client or _storage_client()
    storage_client.bucket(bucket_name).blob(object_name).upload_from_string(
        payload,
        content_type="text/csv",
        if_generation_match=0,
    )
    return f"gs://{bucket_name}/{object_name}"
