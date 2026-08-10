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

# Every placeholder the release scripts know how to substitute. A YAML file that
# uses anything outside this set would reach Azure with the literal text intact.
SUBSTITUTED = {
    "ENV",
    "MODEL_VERSION",
    "IMAGE_DIGEST",
    "INSTANCE_TYPE",
    "MIN_INSTANCES",
    "WORKSPACE_MLFLOW_URI",
    "ACR_LOGIN_SERVER",
    # The four mandatory tags a Deny policy enforces at resource-group scope.
    # Deployments are created by `az ml`, not Terraform, so the tags cannot come
    # from the provider's default_tags and have to be rendered in.
    "OWNER",
    "COST_CENTER",
    "DATA_CLASSIFICATION",
    "MANAGED_BY",
    # Terraform owns the endpoints, so this one is resolved from a Terraform
    # output rather than by a release script. The file is the reviewable
    # declaration of what the azapi resource creates.
    "ENDPOINT_IDENTITY_RESOURCE_ID",
}

# Not an az ml spec: the application's own training config, read by the pipeline
# steps rather than submitted to Azure.
NOT_AZURE_ML_SPECS = {"config.azure.yaml"}

PLACEHOLDER = re.compile(r"\$\{\{([A-Z_]+)\}\}")


def main() -> int:
    problems: list[str] = []
    specs = sorted(SPEC_DIR.rglob("*.yaml"))
    if not specs:
        print(f"no Azure ML specs found under {SPEC_DIR}", file=sys.stderr)
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

        for name in sorted(set(PLACEHOLDER.findall(text)) - SUBSTITUTED):
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
