"""Validate every Azure ML spec in the repository before it can reach a workspace.

`az ml` will happily accept a YAML file that parses and then fail at submission
because a component path does not exist or a placeholder was never substituted.
Both of those are cheap to catch here and expensive to catch during a deploy, so
this runs on every pull request.

Four classes of error are checked:

  * the file is not valid YAML, or has no ``$schema``;
  * a pipeline references a component file that is not in the repository;
  * a deployment YAML contains a ``${{PLACEHOLDER}}`` that no release script
    substitutes, which would be shipped to Azure literally;
  * a ``code`` directory or ``scoring_script`` that does not exist.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC_DIR = ROOT / "deploy" / "azure" / "azureml"
SCRIPT_DIR = ROOT / "deploy" / "azure"

# Placeholders that no release script substitutes because nothing renders them:
# Terraform owns the online endpoint, and this value is one of its outputs. The
# file is the reviewable declaration of what the azapi resource creates.
RESOLVED_OUTSIDE_THE_SCRIPTS = {"ENDPOINT_IDENTITY_RESOURCE_ID"}

# Not an az ml spec: the application's own training config, read by the pipeline
# steps rather than submitted to Azure.
NOT_AZURE_ML_SPECS = {"config.azure.yaml"}

PLACEHOLDER = re.compile(r"\$\{\{([A-Z_]+)\}\}")

# Every renderer in deploy/azure passes its values as environment assignments on
# the continued line that invokes python3, so an assignment ending in a
# backslash is exactly a placeholder some script provides.
RENDERER_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=.*\\$", re.MULTILINE)


def substituted_placeholders() -> set[str]:
    """Read the names the release scripts actually provide.

    This used to be a literal set maintained by hand, which drifted the first
    time a script started rendering a new spec: the four placeholders in
    job-publish-scores.yaml were all substituted correctly by
    run_batch_scoring.sh, and the check failed anyway because nobody had
    updated the list. A check that reports a working deploy as broken gets
    edited to agree with the code, so it may as well read the code.
    """
    provided: set[str] = set()
    for script in sorted(SCRIPT_DIR.glob("*.sh")):
        provided |= set(RENDERER_ASSIGNMENT.findall(script.read_text(encoding="utf-8")))
    return provided | RESOLVED_OUTSIDE_THE_SCRIPTS


def main() -> int:
    problems: list[str] = []
    specs = sorted(SPEC_DIR.rglob("*.yaml"))
    if not specs:
        print(f"no Azure ML specs found under {SPEC_DIR}", file=sys.stderr)
        return 1

    substituted = substituted_placeholders()
    # If the renderers change shape, the derivation returns nothing and every
    # placeholder passes. Fail on the empty set rather than on nothing at all.
    if not substituted - RESOLVED_OUTSIDE_THE_SCRIPTS:
        print(
            f"no rendered placeholders found in {SCRIPT_DIR}; "
            "the renderers changed shape and this check is now blind",
            file=sys.stderr,
        )
        return 1

    for path in specs:
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")

        try:
            document = yaml.safe_load(text)
        except yaml.YAMLError as error:
            problems.append(f"{relative}: invalid YAML: {error}")
            continue

        if not isinstance(document, dict):
            problems.append(f"{relative}: expected a mapping at the top level")
            continue

        # Some files under this directory are read by other tools, not az ml.
        if path.name not in NOT_AZURE_ML_SPECS and "$schema" not in document:
            problems.append(f"{relative}: no $schema, so az ml cannot validate it")

        for name in sorted(set(PLACEHOLDER.findall(text)) - substituted):
            problems.append(
                f"{relative}: placeholder {name} is never substituted by a release script"
            )

        for job in (document.get("jobs") or {}).values():
            component = job.get("component") if isinstance(job, dict) else None
            if (
                isinstance(component, str)
                and component.startswith(".")
                and not (path.parent / component).resolve().is_file()
            ):
                problems.append(f"{relative}: missing component {component}")

        code = document.get("code")
        if isinstance(code, str) and not (path.parent / code).resolve().is_dir():
            problems.append(f"{relative}: code directory {code} does not exist")

        configuration = document.get("code_configuration") or {}
        scoring = configuration.get("scoring_script")
        scoring_root = configuration.get("code")
        if scoring and scoring_root:
            target = (path.parent / scoring_root / scoring).resolve()
            if not target.is_file():
                problems.append(
                    f"{relative}: scoring script {scoring} not found in {scoring_root}"
                )

        print(f"ok {relative}")

    if problems:
        print(file=sys.stderr)
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1

    print(f"\n{len(specs)} Azure ML specs validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
