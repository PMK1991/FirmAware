"""Resolve local or managed MLflow tracking without provider-specific code."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import mlflow


def _absolute_tracking_uri(uri: str, config_path: Path) -> str:
    if uri.startswith("sqlite:///"):
        database = uri[len("sqlite:///") :]
        if database == ":memory:":
            return uri
        database_path = Path(database)
        if not database_path.is_absolute():
            database_path = config_path.parent / database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{database_path.resolve().as_posix()}"
    if "://" not in uri:
        local_path = Path(uri)
        if not local_path.is_absolute():
            local_path = config_path.parent / local_path
        local_path.mkdir(parents=True, exist_ok=True)
        return local_path.resolve().as_uri()
    return uri


def _absolute_artifact_uri(uri: str | None, config_path: Path) -> str | None:
    if uri is None or "://" in uri:
        return uri
    local_path = Path(uri)
    if not local_path.is_absolute():
        local_path = config_path.parent / local_path
    local_path.mkdir(parents=True, exist_ok=True)
    return local_path.resolve().as_uri()


def _is_remote_tracking(uri: str) -> bool:
    return "://" in uri and not uri.startswith(("file://", "sqlite:///"))


def _redact_uri(uri: str) -> str:
    if uri.startswith(("file://", "sqlite:///")):
        return uri
    parsed = urlsplit(uri)
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    return urlunsplit((parsed.scheme, hostname, parsed.path, "", ""))


def configure_tracking(
    mlflow_config: dict[str, Any], config_path: str | Path
) -> dict[str, Any]:
    """Let standard environment variables redirect the same job to cloud tracking."""
    resolved = dict(mlflow_config)
    resolved_config_path = Path(config_path).resolve()
    configured_uri = os.getenv(
        "MLFLOW_TRACKING_URI", str(mlflow_config["tracking_uri"])
    )
    resolved["tracking_uri"] = _absolute_tracking_uri(
        configured_uri, resolved_config_path
    )
    artifact_root_override = os.getenv("FIRMAWARE_MLFLOW_ARTIFACT_ROOT")
    configured_artifact_root = (
        artifact_root_override
        if artifact_root_override is not None
        else mlflow_config.get("artifact_root")
    )
    if (
        artifact_root_override is None
        and _is_remote_tracking(resolved["tracking_uri"])
        and configured_artifact_root
        and "://" not in configured_artifact_root
    ):
        configured_artifact_root = None
    resolved["artifact_root"] = _absolute_artifact_uri(
        configured_artifact_root, resolved_config_path
    )
    resolved["display_tracking_uri"] = _redact_uri(resolved["tracking_uri"])
    resolved["experiment_name"] = os.getenv(
        "MLFLOW_EXPERIMENT_NAME", str(mlflow_config["experiment_name"])
    )
    resolved["run_name"] = os.getenv(
        "FIRMAWARE_MLFLOW_RUN_NAME", mlflow_config.get("run_name") or ""
    ) or None
    resolved["registered_model_name"] = os.getenv(
        "FIRMAWARE_REGISTERED_MODEL_NAME",
        str(mlflow_config["registered_model_name"]),
    )
    mlflow.set_tracking_uri(resolved["tracking_uri"])
    experiment = mlflow.get_experiment_by_name(resolved["experiment_name"])
    if experiment is None:
        mlflow.create_experiment(
            resolved["experiment_name"],
            artifact_location=resolved["artifact_root"],
        )
    mlflow.set_experiment(resolved["experiment_name"])
    return resolved
