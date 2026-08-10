"""Azure ML online scoring entry script.

Deliberately thin. All validation, feature derivation, preprocessing, scoring and
decision logic live in the registered MLflow PyFunc, which is the same artefact
the batch deployment loads -- there is exactly one preprocessing code path, so a
real-time score and a batch score for the same row cannot disagree.

Three behaviours this script owns, all carried from the pipeline spec into the
serving surface:

  * A contract violation answers **HTTP 422 naming the violation**, never a
    success-shaped default. This is why the module returns ``AMLResponse``
    objects rather than JSON strings: returning a string makes the response 200
    regardless of what the body says, and a caller that trusts the status code
    would treat a rejection as a score.
  * ``unseen_categories`` is always present and always real. The PyFunc emits it
    as a JSON string; it is decoded back into an object here so an out-of-corpus
    vendor is visible in the response body rather than being flattened to the
    encoder baseline and scored as if it were familiar.
  * ``model_version`` and ``threshold`` are echoed on every row, so a caller can
    tell which model made a decision and at what cut-off without consulting the
    registry. The smoke test asserts on both.

Payloads are never logged -- only the correlation id and row counts. The records
carry device, site and vendor identifiers, and this endpoint is not the place to
duplicate them into a log store with a different retention and access policy.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

_LOGGER = logging.getLogger("firmaware.score")

_MODEL: Any = None
_METADATA: dict[str, Any] = {}
_MODEL_VERSION = os.environ.get("FIRMAWARE_MODEL_VERSION", "unknown")

# The scoring contract, restated here only to reject a request before it reaches
# the model. The authoritative list lives in firmaware.schema and the PyFunc
# enforces it; this is a fast, cheap pre-check that produces a clearer message
# than a pandas KeyError.
_REQUIRED_TOP_LEVEL = "records"


def init() -> None:
    """Load the registered PyFunc once per container."""
    global _MODEL, _METADATA
    import mlflow.pyfunc

    model_dir = os.path.join(os.environ["AZUREML_MODEL_DIR"], "model")
    _MODEL = mlflow.pyfunc.load_model(model_dir)

    # The threshold is a property of the trained model, not of the deployment, so
    # it is read off the artefact rather than passed in as an environment
    # variable that could drift from the model actually loaded.
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


def run(raw_data: str) -> Any:
    """Score one or more deployment records."""
    import pandas as pd
    from azureml.contrib.services.aml_response import AMLResponse

    correlation_id = str(uuid.uuid4())

    try:
        payload = json.loads(raw_data)
    except ValueError as error:
        return _reject(correlation_id, f"request body is not valid JSON: {error}")

    if isinstance(payload, dict):
        if _REQUIRED_TOP_LEVEL not in payload:
            return _reject(
                correlation_id,
                f"request object must contain a {_REQUIRED_TOP_LEVEL!r} array",
            )
        records = payload[_REQUIRED_TOP_LEVEL]
    else:
        records = payload

    if not isinstance(records, list) or not records:
        return _reject(correlation_id, "records must be a non-empty array")

    try:
        frame = pd.DataFrame(records)
    except (TypeError, ValueError) as error:
        return _reject(correlation_id, f"records are not tabular: {error}")

    _LOGGER.info("scoring %d rows, correlation_id=%s", len(frame), correlation_id)

    try:
        scored = _MODEL.predict(frame)
    except Exception as error:
        if _is_contract_violation(error):
            return _reject(correlation_id, str(error))
        _LOGGER.error(
            "scoring failed: %s, correlation_id=%s",
            type(error).__name__,
            correlation_id,
        )
        raise

    results = [_shape(row) for row in scored.to_dict(orient="records")]
    unseen_rows = sum(1 for row in results if row["unseen_categories"])
    if unseen_rows:
        # Counted, never contented: which categories were unseen is in the
        # response the caller already has.
        _LOGGER.warning(
            "%d of %d rows carried unseen categories, correlation_id=%s",
            unseen_rows,
            len(results),
            correlation_id,
        )

    return AMLResponse(
        json.dumps(
            {
                "correlation_id": correlation_id,
                "model_version": _MODEL_VERSION,
                "threshold": _threshold(),
                "results": results,
            }
        ),
        200,
        json_str=True,
    )


def _shape(row: dict[str, Any]) -> dict[str, Any]:
    """Return the contracted response row, with unseen categories as an object."""
    raw = row.get("unseen_categories")
    if isinstance(raw, str):
        try:
            unseen = json.loads(raw)
        except ValueError:
            unseen = {}
    else:
        unseen = raw or {}

    return {
        "deployment_id": row.get("deployment_id"),
        "risk_probability": row.get("risk_probability"),
        "risk_prediction": row.get("risk_prediction"),
        "risk_band": row.get("risk_band"),
        # Always a dict, empty only when the row genuinely had no out-of-corpus
        # value. The smoke test asserts exactly one of five fixture rows is
        # non-empty, which is what proves this is populated rather than dropped.
        "unseen_categories": unseen,
        "model_run": row.get("model_run"),
        "model_version": _MODEL_VERSION,
        "threshold": _threshold(),
    }


def _threshold() -> float | None:
    value = _METADATA.get("threshold")
    return None if value is None else float(value)


def _is_contract_violation(error: Exception) -> bool:
    """Recognise the contract error across the pickle boundary.

    The PyFunc raises firmaware.schema.ContractViolation, but MLflow may surface
    it wrapped, so the type is matched by name across the whole exception chain
    rather than with isinstance against an import this script would otherwise not
    need.
    """
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if type(current).__name__ == "ContractViolation":
            return True
        current = current.__cause__ or current.__context__
    return False


def _reject(correlation_id: str, message: str) -> Any:
    from azureml.contrib.services.aml_response import AMLResponse

    _LOGGER.warning("rejected request %s: %s", correlation_id, message)
    return AMLResponse(
        json.dumps(
            {
                "correlation_id": correlation_id,
                "error": {"status": 422, "message": message},
            }
        ),
        422,
        json_str=True,
    )
