"""Azure ML batch scoring entry script.

Separate from ``score.py`` because the two invocation contracts are genuinely
different, not merely stylistically so:

  * Online receives a JSON request body and must answer with an HTTP status --
    hence ``AMLResponse`` and the 422 path.
  * Batch receives ``mini_batch``, a **list of file paths**, and must return rows
    that the ``append_row`` output action concatenates into one file. There is no
    HTTP status to set and no caller to reject; a bad row must fail the run.

Sharing one script between them looks economical and is not: the batch runner
would call ``run(list_of_paths)`` against a function that expects a JSON string,
and ``azureml.contrib.services`` is not present in the batch image at all.

What *is* shared, deliberately, is the model. Both deployments load the same
registered PyFunc, so a batch score and a real-time score for the same row agree
by construction rather than by review.

``error_threshold: 0`` in the deployment spec means a single unscoreable row
fails the whole run. That is intentional: the scores container is append-only,
so a partial run that looked complete could never be cleaned up.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

_LOGGER = logging.getLogger("firmaware.batch_score")

_MODEL: Any = None
_METADATA: dict[str, Any] = {}


def _resolve_model_version() -> str:
    """Find the registry version this deployment serves.

    Not from an environment variable, because a batch deployment cannot have
    one. The schema advertises `environment_variables` and the CLI accepts it,
    but the service drops it: the created deployment reads back `{}` and the job
    definition carries only AML's own `AML_PARAMETER_*` values. Nothing else the
    runtime exposes names the version either -- `AZUREML_MODEL_DIR` resolves to a
    workspace-id path, and the model artifact predates its own registration.

    So `deploy_batch.sh` writes the version beside this script and the whole
    directory is uploaded as the deployment's code snapshot. That snapshot is
    created per deployment and immutable once uploaded, so the file cannot drift
    from the deployment that serves it.

    The environment variable is still consulted first, so a local run or a future
    platform that does honour it needs no change here.
    """
    from_env = os.environ.get("FIRMAWARE_MODEL_VERSION")
    if from_env:
        return from_env
    sidecar = os.path.join(os.path.dirname(os.path.abspath(__file__)), "model_version.txt")
    try:
        with open(sidecar, encoding="utf-8") as handle:
            version = handle.read().strip()
    except OSError:
        return "unknown"
    return version or "unknown"


_MODEL_VERSION = _resolve_model_version()

_OUTPUT_COLUMNS = [
    "deployment_id",
    "risk_probability",
    "risk_prediction",
    "risk_band",
    "unseen_categories",
    "model_run",
    "model_version",
    "threshold",
]


def init() -> None:
    """Load the registered PyFunc once per worker process."""
    global _MODEL, _METADATA
    import mlflow.pyfunc

    model_dir = os.path.join(os.environ["AZUREML_MODEL_DIR"], "model")
    _MODEL = mlflow.pyfunc.load_model(model_dir)

    try:
        _METADATA = dict(_MODEL.unwrap_python_model().metadata)
    except Exception as error:  # noqa: BLE001 - never block startup on metadata
        _LOGGER.warning("model metadata unavailable: %s", type(error).__name__)
        _METADATA = {}

    _LOGGER.info(
        "loaded model version %s (threshold %s)",
        _MODEL_VERSION,
        _METADATA.get("threshold", "unknown"),
    )


def run(mini_batch: list[str]) -> Any:
    """Score every row of every file in the mini-batch.

    Returns a DataFrame; ``append_row`` writes it to the run's output file.
    Exceptions are deliberately allowed to propagate -- with
    ``error_threshold: 0`` a failure here fails the run, which is what keeps a
    partial result out of the append-only scores container.
    """
    import pandas as pd

    frames = []
    for path in mini_batch:
        _LOGGER.info("scoring %s", path)
        frame = _read(path)
        if frame.empty:
            _LOGGER.warning("%s contained no rows", path)
            continue

        scored = _MODEL.predict(_as_served(frame))
        scored = scored.copy()
        scored["model_version"] = _MODEL_VERSION
        scored["threshold"] = _threshold()
        # The PyFunc emits unseen_categories as a JSON string so it survives the
        # tabular signature. Kept as a string here: append_row writes CSV, and a
        # dict would be flattened into an unparseable repr.
        scored["unseen_categories"] = scored["unseen_categories"].map(_as_json)
        frames.append(scored.reindex(columns=_OUTPUT_COLUMNS))

    if not frames:
        return pd.DataFrame(columns=_OUTPUT_COLUMNS)

    result = pd.concat(frames, ignore_index=True)
    unseen_rows = int((result["unseen_categories"] != "{}").sum())
    if unseen_rows:
        _LOGGER.warning(
            "%d of %d rows carried unseen categories", unseen_rows, len(result)
        )
    return result


def _read(path: str):
    """Read one input file, honouring the two formats the pipeline produces."""
    import pandas as pd

    if path.endswith(".parquet"):
        return pd.read_parquet(path)
    return pd.read_csv(path)


def _as_served(frame: Any) -> Any:
    """Cast columns the loaded model declares as double, so int64 input is taken.

    `serving_signature` types every numeric as `double` to keep nullable inputs
    expressible, and MLflow's schema enforcement refuses to widen int64 to double
    -- it calls the conversion unsafe and rejects the batch before the model
    runs. `pd.read_csv` infers int64 for any column of whole numbers, so without
    this an ordinary CSV of upcoming deployments fails wholesale with "Can not
    safely convert int64 to float64".

    The column list comes from the loaded model's own signature rather than from
    `firmaware.schema`, and that is deliberate. Training logs the package with
    `code_paths`, so a copy of `firmaware` is frozen inside the model artifact
    and MLflow puts it ahead of the image's copy on sys.path. Importing a helper
    from the package here would resolve to whatever was captured when *that
    model version* was trained, so a serving-side fix would appear to do nothing
    until the model was retrained. The signature is carried by the same artifact
    and cannot fall out of step with it.

    A column that will not convert is left alone rather than raised on, so the
    model's contract validation still reports it as a named violation instead of
    a bare pandas ValueError.
    """
    schema = _MODEL.metadata.get_input_schema()
    if schema is None:
        return frame

    converted = {}
    for spec in schema.inputs:
        name = getattr(spec, "name", None)
        if name is None or name not in frame.columns:
            continue
        if getattr(spec.type, "name", str(spec.type)) != "double":
            continue
        try:
            converted[name] = frame[name].astype("float64")
        except (TypeError, ValueError):
            continue
    return frame.assign(**converted) if converted else frame


def _as_json(value: Any) -> str:
    """Serialise compactly: the output writer is space-separated.

    `json.dumps` defaults to `", "` and `": "` separators, and a space inside a
    value forces the writer to quote the field, so a compact form keeps every
    row unambiguously splittable on whitespace.
    """
    if isinstance(value, str):
        return value
    if not value:
        return "{}"
    return json.dumps(value, separators=(",", ":"))


def _threshold() -> float | None:
    value = _METADATA.get("threshold")
    return None if value is None else float(value)
