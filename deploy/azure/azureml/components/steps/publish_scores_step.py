"""Publish a batch scoring run into the append-only scores container.

Batch endpoints cannot write here themselves, and the reason is worth recording
because it looks like a permissions problem and is not:

  * `--output-path` rejects an ADLS Gen2 datastore outright
    (`DatastoreTypeNotSupported`), and the lake is Gen2 because the pipeline
    addresses it over `abfss://`.
  * Registering the same container again as an `azure_blob` datastore gets
    past that, and scoring then succeeds -- but the run still fails when the
    driver concatenates its output, with HTTP 409 "blob is immutable due to a
    policy" on a `Put Blob` of a file that does not exist yet. The container
    allows protected append writes, which permits append-style writes only;
    AML's writer uploads whole blobs.

So the batch endpoint stages its raw output on the workspace store, which is
scratch space, and this step promotes it to evidence. The promotion is what
`write_scores` already does for the `predict` CLI: one immutable object per run,
created with `overwrite=False`, over the append-style ADLS create/append/flush
path that the container's policy does permit.

Running as an AML job rather than in CI is deliberate. The append-only role on
the scores container belongs to the workspace identity; the CI identity holds
`AzureML Data Scientist` and no storage data-plane grant at all, so a publish
step running on the runner could not write here even if the policy allowed it.
"""

from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from firmaware.io import write_scores
from firmaware.model import SCORE_COLUMNS

# The staged file's column order, which is the only contract there is: the batch
# endpoint driver hardcodes `--append_row_dataframe_header False`, so the file
# carries no header to check against. It must match batch_score._OUTPUT_COLUMNS.
STAGED_COLUMNS = [*SCORE_COLUMNS, "model_version", "threshold"]
PUBLISHED_COLUMNS = [*STAGED_COLUMNS, "scored_at"]


def _read_staged(staged_dir: Path, file_name: str) -> pd.DataFrame:
    """Parse the driver's append_row output into the canonical score frame."""
    staged = staged_dir / file_name
    if not staged.is_file():
        matches = sorted(staged_dir.rglob(file_name))
        if not matches:
            raise SystemExit(
                f"[publish] no {file_name} under {staged_dir}; the batch run "
                "did not stage any output, so there is nothing to publish"
            )
        staged = matches[0]

    # Space-separated, not comma: ParallelRunStep's append_row writer joins
    # fields with a space and quotes any field containing one. csv.reader with
    # that delimiter handles the quoting correctly; pandas would need the same
    # configuration and would still infer dtypes we do not want.
    with staged.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.reader(handle, delimiter=" ") if row]

    if not rows:
        raise SystemExit(f"[publish] {staged} is empty; refusing to publish")

    bad = [index for index, row in enumerate(rows) if len(row) != len(STAGED_COLUMNS)]
    if bad:
        raise SystemExit(
            f"[publish] {staged} rows {bad[:5]} have the wrong field count; "
            f"expected {len(STAGED_COLUMNS)} fields in the order {STAGED_COLUMNS}"
        )

    return pd.DataFrame(rows, columns=STAGED_COLUMNS)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", required=True)
    parser.add_argument("--scores-uri", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--staged-file-name", default="predictions.csv")
    args = parser.parse_args()

    scores = _read_staged(Path(args.staged), args.staged_file_name)

    versions = set(scores["model_version"])
    if len(versions) != 1:
        raise SystemExit(f"[publish] mixed model versions in one run: {versions}")
    version = versions.pop()
    if version == "unknown":
        # Publishing these would put un-attributable rows into a container that
        # cannot be corrected for the length of its retention window.
        raise SystemExit(
            "[publish] model_version is 'unknown'; the code-snapshot sidecar did "
            "not reach the scoring container. Refusing to write unattributable "
            "rows into an immutable store."
        )

    scored_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    scores["scored_at"] = scored_at
    scores = scores[PUBLISHED_COLUMNS]

    written = write_scores(
        scores,
        args.scores_uri,
        scored_at,
        args.run_id,
        PUBLISHED_COLUMNS,
    )

    decisions = scores["risk_prediction"].value_counts().to_dict()
    unseen = int((~scores["unseen_categories"].isin(["{}", ""])).sum())
    print(f"[publish] {len(scores)} rows from model version {version} -> {written}")
    print(
        f"[publish] decisions: GO={decisions.get('GO', 0)}, "
        f"NO_GO={decisions.get('NO_GO', 0)}; rows with unseen categories: {unseen}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
