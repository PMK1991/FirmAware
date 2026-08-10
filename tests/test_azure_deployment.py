from __future__ import annotations

import re
import unittest
from pathlib import Path

import pandas as pd
import yaml

from firmaware.schema import validate

ROOT = Path(__file__).resolve().parents[1]
AZURE_INFRA = ROOT / "infra" / "azure"
AZURE_DEPLOY = ROOT / "deploy" / "azure"


def _read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


class AzureIdentityTests(unittest.TestCase):
    """The RBAC posture must be auditable from the code alone."""

    def test_scores_role_can_read_write_and_append_but_not_delete(self) -> None:
        identity = _read("infra", "azure", "modules", "identity", "main.tf")
        block = identity.split('resource "azurerm_role_definition" "scores_appender"')[1]
        block = block.split("assignable_scopes")[0]

        prefix = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/"
        self.assertIn(f"{prefix}read", block)
        self.assertIn(f"{prefix}write", block)
        self.assertIn(f"{prefix}add/action", block)
        # The omission is the control. Scores are evidence: superseded, never
        # removed, and not by the identity that produced them either.
        self.assertNotIn(f"{prefix}delete", block)

    def test_scores_uses_the_appender_role_not_blob_contributor(self) -> None:
        identity = _read("infra", "azure", "modules", "identity", "main.tf")
        block = identity.split(
            'resource "azurerm_role_assignment" "workspace_scores_appender"'
        )[1].split("resource ")[0]
        self.assertIn("azurerm_role_definition.scores_appender", block)
        self.assertNotIn("Storage Blob Data Contributor", block)

    def test_runtime_identities_pull_images_but_cannot_push(self) -> None:
        identity = _read("infra", "azure", "modules", "identity", "main.tf")
        for name in ("workspace_acr_pull", "endpoint_acr_pull"):
            block = identity.split(f'resource "azurerm_role_assignment" "{name}"')[
                1
            ].split("resource ")[0]
            self.assertIn('role_definition_name = "AcrPull"', block)
            # Nothing that runs a job may change what a later job executes.
            self.assertNotIn("AcrPush", block)

    def test_privileged_roles_are_scoped_to_a_resource_group(self) -> None:
        identity = _read("infra", "azure", "modules", "identity", "main.tf")
        for name in ("cicd_rg_contributor", "cicd_rg_user_access_admin"):
            block = identity.split(f'resource "azurerm_role_assignment" "{name}"')[
                1
            ].split("resource ")[0]
            self.assertIn("scope                = var.resource_group_id", block)
        # Owner is never assigned, and no assignment targets a subscription.
        self.assertNotIn('role_definition_name = "Owner"', identity)
        self.assertNotIn("scope                = var.subscription_id", identity)

    def test_only_the_environment_subject_is_federated(self) -> None:
        """A branch credential alongside the environment one would let a job
        skip the required-reviewer gate.

        GitHub puts the environment into the OIDC subject whenever a job
        declares one, so a branch-subject credential existing alongside it is
        not a fallback: it is a second door into the same identity that opens
        on any push, without an approval.
        """
        bootstrap = _read("infra", "azure", "bootstrap.sh")
        subjects = re.findall(r'add_federation\s+"[^"]+"\s*\\\s*\n\s*"([^"]+)"', bootstrap)
        self.assertEqual(len(subjects), 1, "exactly one federated subject may exist")
        self.assertIn(":environment:", subjects[0])
        self.assertNotIn("ref:refs/heads/", bootstrap)


    def test_workspace_identity_holds_both_halves_on_key_vault(self) -> None:
        """Key Vault Administrator is data-plane only -- its `actions` list is
        empty -- so on an RBAC-enabled vault it cannot satisfy the control-plane
        Microsoft.KeyVault/vaults/read that workspace creation performs.

        Getting this wrong fails in a uniquely misleading way. Azure ML reports
        one missing action however many are absent, and then caches the failure
        against the workspace *name*, so the same name keeps failing for tens of
        minutes after the grants are correct while a fresh name succeeds at once.
        An operator reading that error will hunt for a permissions bug they have
        already fixed. Microsoft's documented pair is both roles, not either.
        """
        identity = _read("infra", "azure", "modules", "identity", "main.tf")
        for role, name in (
            ("Key Vault Administrator", "workspace_kv_admin"),
            ("Contributor", "workspace_kv_contributor"),
        ):
            block = identity.split(f'resource "azurerm_role_assignment" "{name}"')[1]
            block = block.split("}")[0]
            self.assertIn(f'role_definition_name = "{role}"', block)
            self.assertIn("var.key_vault_id", block)

    def test_workspace_identity_is_granted_on_every_documented_dependency(self) -> None:
        """Storage, Key Vault, ACR and Application Insights, each at the
        individual resource. The group-scope Reader is read-only and does not
        substitute: provisioning performs control-plane writes on all four."""
        identity = _read("infra", "azure", "modules", "identity", "main.tf")
        for name, scope in (
            ("workspace_kv_contributor", "var.key_vault_id"),
            ("workspace_acr_contributor", "var.container_registry_id"),
            ("workspace_system_storage_control", "var.workspace_storage_account_id"),
            ("workspace_app_insights_contributor", "var.application_insights_id"),
        ):
            block = identity.split(f'resource "azurerm_role_assignment" "{name}"')[1]
            block = block.split("}")[0]
            self.assertIn('role_definition_name = "Contributor"', block)
            self.assertIn(scope, block)

    def test_application_insights_precedes_the_workspace_that_uses_it(self) -> None:
        """It lives in foundation, not beside the workspace, because the
        workspace identity needs Contributor on it *before* the workspace is
        created. Declared in the workspace module that grant is unexpressible:
        identity would have to depend on the thing it is a prerequisite for."""
        self.assertIn(
            'resource "azurerm_application_insights" "this"',
            _read("infra", "azure", "modules", "foundation", "main.tf"),
        )
        self.assertNotIn(
            'resource "azurerm_application_insights"',
            _read("infra", "azure", "modules", "workspace", "main.tf"),
        )


class AzureStorageTests(unittest.TestCase):
    def test_shared_keys_and_public_blob_access_are_disabled(self) -> None:
        storage = _read("infra", "azure", "modules", "storage", "main.tf")
        for assignment in (
            "shared_access_key_enabled",
            "allow_nested_items_to_be_public",
        ):
            self.assertRegex(storage, rf"{assignment}\s*=\s*false")
        self.assertRegex(storage, r'min_tls_version\s*=\s*"TLS1_2"')
        self.assertRegex(storage, r"infrastructure_encryption_enabled\s*=\s*true")

    def test_scores_container_has_no_expiration_rule(self) -> None:
        """Tiering artifacts is a cost decision; expiring scores would destroy
        the audit trail the append-only role exists to protect."""
        storage = _read("infra", "azure", "modules", "storage", "main.tf")
        policy = storage.split('resource "azurerm_storage_management_policy"')[1]
        self.assertNotIn('prefix_match = ["scores', policy)


class AzurePolicyTests(unittest.TestCase):
    def test_six_mandatory_tags_are_required_by_policy(self) -> None:
        """The policy module requires exactly the keys of local.tags, so the two
        cannot drift: adding a tag to the map assigns a policy for it."""
        main = _read("infra", "azure", "main.tf")
        block = main.split("locals {")[1].split("\n}")[0]
        for tag in (
            "app",
            "env",
            "owner",
            "cost_center",
            "managed_by",
            "data_classification",
        ):
            self.assertRegex(block, rf"\n\s+{tag}\s*=")
        self.assertIn("required_tags     = keys(local.tags)", main)


class AzureEndpointTests(unittest.TestCase):
    def test_batch_endpoint_uses_a_system_assigned_identity(self) -> None:
        """Batch endpoints reject a user-assigned identity outright: "does not
        support creation of 'UserAssigned' resource identity. The supported types
        are 'SystemAssigned'". The online endpoint above does accept one, so the
        two deliberately differ.

        This gives nothing away. A batch endpoint is only a routing name; the
        work runs in a deployment on the training cluster under the workspace
        identity, so append-only on scores is still enforced where writes occur.
        """
        endpoints = _read("infra", "azure", "modules", "endpoints", "main.tf")
        batch = endpoints.split('resource "azapi_resource" "batch"')[1]
        identity = batch.split("identity {")[1].split("}")[0]
        self.assertIn('type = "SystemAssigned"', identity)
        self.assertNotIn("UserAssigned", identity)

        online = endpoints.split('resource "azapi_resource" "online"')[1]
        online_identity = online.split("identity {")[1].split("}")[0]
        self.assertIn('type         = "UserAssigned"', online_identity)


class AzureEnvironmentTests(unittest.TestCase):
    def test_prod_leaves_every_relaxable_control_at_its_secure_default(self) -> None:
        prod = _read("infra", "azure", "envs", "prod.tfvars")
        # Comments naming the relaxations are expected and wanted; only a real
        # assignment would be an override.
        assignments = "\n".join(
            line for line in prod.splitlines() if not line.lstrip().startswith("#")
        )
        for relaxation in (
            "network_isolation",
            "registry_sku",
            "key_vault_purge_protection",
            "scores_immutability_locked",
            "scores_retention_days",
            "storage_network_default_action",
        ):
            self.assertNotRegex(
                assignments,
                rf"(?m)^\s*{relaxation}\s*=",
                f"prod must not override {relaxation}",
            )

    def test_dev_relaxations_are_named_and_all_present_in_dev_only(self) -> None:
        dev = _read("infra", "azure", "envs", "dev.tfvars")
        self.assertIn("network_isolation = false", dev)
        self.assertIn('registry_sku = "Basic"', dev)
        self.assertIn("key_vault_purge_protection = false", dev)
        # The direct consequence of dropping private endpoints, stated rather
        # than implied: a default-deny firewall would lock dev out of its data.
        self.assertIn('storage_network_default_action = "Allow"', dev)

    def test_dev_disables_the_online_endpoint_that_cannot_scale_to_zero(self) -> None:
        dev = _read("infra", "azure", "envs", "dev.tfvars")
        self.assertIn("online_endpoint_enabled = false", dev)
        self.assertIn("monthly_budget = 40", dev)

    def test_oidc_subject_matches_the_workflow_environment(self) -> None:
        """A mismatch here fails the token exchange with an error that names
        neither file, so it is asserted rather than discovered at deploy time.

        bootstrap.sh is the only place the subject is written -- Terraform does
        not create the credential -- so its per-environment defaults are what
        must line up with the `environment:` each workflow declares.
        """
        bootstrap = _read("infra", "azure", "bootstrap.sh")
        defaults = re.findall(r'GITHUB_ENVIRONMENT:-(\w+)', bootstrap)
        self.assertEqual(defaults, ["production", "dev"], "prod branch is tested first")

        for github_environment, workflow_name in zip(
            defaults, ("azure-deploy-prod.yaml", "azure-deploy-dev.yaml")
        ):
            workflow = _read(".github", "workflows", workflow_name)
            self.assertIn(f"environment: {github_environment}", workflow)

    def test_tfvars_do_not_restate_the_federated_subject(self) -> None:
        """Two sources of truth for the subject drift, and the drift surfaces as
        an unauthorized token exchange rather than as a diff in a plan."""
        for name in ("dev.tfvars", "prod.tfvars"):
            body = _read("infra", "azure", "envs", name)
            self.assertNotRegex(body, r"(?m)^\s*github_\w+\s*=")


class AzurePipelineTests(unittest.TestCase):
    def test_pipeline_config_disables_in_training_registration(self) -> None:
        """The gate must be able to block registration.

        firmaware.train registers the model itself when register_model is true,
        which would leave a registry version behind even when the gate fails.
        """
        config = yaml.safe_load(
            (AZURE_DEPLOY / "azureml" / "config.azure.yaml").read_text(encoding="utf-8")
        )
        self.assertFalse(config["mlflow"]["register_model"])

    def test_pipeline_stops_at_the_first_failed_step(self) -> None:
        pipeline = yaml.safe_load(
            (AZURE_DEPLOY / "azureml" / "pipeline-train.yaml").read_text(
                encoding="utf-8"
            )
        )
        self.assertIs(pipeline["settings"]["continue_on_step_failure"], False)

    def test_register_runs_after_the_gate_and_consumes_its_output(self) -> None:
        pipeline = yaml.safe_load(
            (AZURE_DEPLOY / "azureml" / "pipeline-train.yaml").read_text(
                encoding="utf-8"
            )
        )
        register_inputs = pipeline["jobs"]["register"]["inputs"]
        wired = " ".join(str(value) for value in register_inputs.values())
        self.assertIn("parent.jobs.evaluate", wired)

    def test_deployments_reference_the_directory_holding_score_py(self) -> None:
        for name in ("deployment-blue", "deployment-green", "deployment-batch"):
            spec = yaml.safe_load(
                (AZURE_DEPLOY / "azureml" / f"{name}.yaml").read_text(encoding="utf-8")
            )
            code = AZURE_DEPLOY / "azureml" / spec["code_configuration"]["code"]
            script = code / spec["code_configuration"]["scoring_script"]
            self.assertTrue(script.is_file(), f"{name}: {script} does not exist")


class AzureScoringTests(unittest.TestCase):
    def test_score_returns_a_response_object_not_a_json_string(self) -> None:
        """A returned string makes Azure ML answer 200 whatever the body says,
        which would render every contract violation invisible to a caller."""
        score = (AZURE_DEPLOY / "azureml" / "score.py").read_text(encoding="utf-8")
        self.assertIn("AMLResponse", score)
        self.assertIn("422", score)

    def test_smoke_fixture_matches_the_scoring_contract(self) -> None:
        fixture = pd.read_csv(AZURE_DEPLOY / "fixtures" / "upcoming_smoke.csv")
        validate(fixture, mode="scoring")
        self.assertEqual(len(fixture), 5)
        # Exactly one out-of-corpus vendor, so the smoke test can assert that
        # unseen categories are reported rather than silently defaulted.
        self.assertEqual(int(fixture["vendor_name"].eq("Moxa").sum()), 1)


class AzureReleaseTests(unittest.TestCase):
    def test_prod_promotes_a_dev_digest_and_never_rebuilds(self) -> None:
        workflow = _read(".github", "workflows", "azure-deploy-prod.yaml")
        self.assertIn("environment: production", workflow)
        self.assertIn("az acr import", workflow)
        self.assertIn("refusing to promote a mutable tag", workflow)
        self.assertNotIn("build.sh", workflow)

    def test_prod_triggers_on_a_version_tag(self) -> None:
        """Acceptance criterion 7 is phrased in terms of tagging v0.1.0."""
        workflow = yaml.safe_load(
            _read(".github", "workflows", "azure-deploy-prod.yaml")
        )
        # PyYAML resolves a bare `on:` key to the boolean True.
        triggers = workflow.get("on", workflow.get(True))
        self.assertIn("v*", triggers["push"]["tags"])
        self.assertIn("workflow_dispatch", triggers)

    def test_prod_deploys_only_the_verified_digest(self) -> None:
        """The deploy job must never read the raw input: that would skip the
        provenance check entirely on the manual path."""
        workflow = _read(".github", "workflows", "azure-deploy-prod.yaml")
        deploy = workflow.split("\n  deploy:")[1]
        self.assertNotIn("inputs.image_digest", deploy)
        self.assertNotIn("inputs.model_version", deploy)
        self.assertIn("needs.verify-dev-provenance.outputs.image_digest", deploy)

    def test_ci_cannot_authenticate_to_azure(self) -> None:
        """A pull request from a fork must reach no cloud resource at all."""
        workflow = _read(".github", "workflows", "azure-ci.yaml")
        self.assertNotIn("id-token: write", workflow)
        self.assertNotIn("Azure/login", workflow)

    def test_deploy_targets_the_slot_carrying_least_traffic(self) -> None:
        """"Green is always the new one" is correct exactly once."""
        script = (AZURE_DEPLOY / "deploy_endpoint.sh").read_text(encoding="utf-8")
        self.assertIn("traffic", script)
        self.assertIn("blue", script)
        self.assertIn("green", script)

    def test_promotion_smoke_tests_through_the_endpoint_between_steps(self) -> None:
        script = (AZURE_DEPLOY / "promote_traffic.sh").read_text(encoding="utf-8")
        self.assertIn("for share in 10 50 100", script)
        # Empty, not unset: smoke_test.sh reads it with ${VAR-default}, so this
        # routes through the traffic map instead of probing a named slot.
        self.assertIn("FIRMAWARE_DEPLOYMENT= bash", script)
    def test_every_third_party_action_is_pinned_to_a_commit_sha(self) -> None:
        for name in (
            "azure-ci.yaml",
            "azure-deploy-dev.yaml",
            "azure-deploy-prod.yaml",
        ):
            workflow = _read(".github", "workflows", name)
            for line in workflow.splitlines():
                if "uses:" not in line:
                    continue
                reference = line.split("uses:")[1].strip().split()[0]
                if reference.startswith("./"):
                    continue
                self.assertRegex(
                    reference,
                    r"@[0-9a-f]{40}$",
                    f"{name}: {reference} is not pinned to a full commit SHA",
                )


class AzureFirstDeployTests(unittest.TestCase):
    """Nothing here is theoretical: each of these broke the first real deploy."""

    def test_backend_config_is_committed_because_ci_reads_it(self) -> None:
        # Both deploy workflows run `terraform init -backend-config=envs/*.backend.hcl`.
        # Git-ignoring that file leaves CI with no way to find its own state.
        ignore = _read(".gitignore")
        self.assertNotIn(
            "*.backend.hcl",
            ignore,
            "backend configs must be committed: the deploy workflows init from them",
        )
        bootstrap = (AZURE_INFRA / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("COMMIT THIS FILE", bootstrap)

    def test_deployment_specs_render_beside_the_code_they_upload(self) -> None:
        # `az ml` resolves a spec's relative `code:` against the spec's own
        # location, so rendering into /tmp would upload /tmp rather than the
        # scoring script.
        for name in ("deploy_endpoint.sh", "rollback_model.sh"):
            script = (AZURE_DEPLOY / name).read_text(encoding="utf-8")
            self.assertIn(
                "mktemp -p deploy/azure/azureml",
                script,
                f"{name} renders the deployment spec away from its code directory",
            )

    def test_traffic_maps_never_name_a_deployment_that_may_not_exist(self) -> None:
        # On the first release only one slot exists, and the endpoint rejects a
        # traffic map naming a deployment it does not have.
        promote = (AZURE_DEPLOY / "promote_traffic.sh").read_text(encoding="utf-8")
        self.assertIn("online-deployment list", promote)
        self.assertIn("live_exists", promote)

        rollback = (AZURE_DEPLOY / "rollback_endpoint.sh").read_text(encoding="utf-8")
        self.assertIn('traffic="${target}=100"', rollback)

    def test_exact_version_is_asserted_only_at_full_traffic(self) -> None:
        # At a 90/10 split a single probe lands on the old slot most of the time,
        # and the old slot correctly serves the previous version. Asserting the
        # new version there would roll back nearly every release.
        script = (AZURE_DEPLOY / "promote_traffic.sh").read_text(encoding="utf-8")
        self.assertIn("FIRMAWARE_MODEL_VERSION=", script)
        self.assertIn('if [[ "${share}" -eq 100 ]]', script)

    def test_batch_deployment_uses_the_batch_scoring_entry_point(self) -> None:
        spec = yaml.safe_load((AZURE_DEPLOY / "azureml" / "deployment-batch.yaml").read_text())
        # Batch calls run(mini_batch) with file paths and has no HTTP status to
        # set, so it cannot share the online endpoint's score.py.
        self.assertEqual(spec["code_configuration"]["scoring_script"], "batch_score.py")
        self.assertTrue((AZURE_DEPLOY / "azureml" / "batch_score.py").is_file())

    def test_every_deployment_carries_the_policy_mandated_tags(self) -> None:
        # A Deny policy at resource-group scope rejects untagged resources, and
        # `az ml` creates deployments outside Terraform's default_tags.
        required = {"owner", "cost_center", "data_classification", "managed_by"}
        for name in ("deployment-blue.yaml", "deployment-green.yaml", "deployment-batch.yaml"):
            spec = yaml.safe_load((AZURE_DEPLOY / "azureml" / name).read_text())
            self.assertTrue(
                required.issubset(spec.get("tags", {})),
                f"{name} is missing mandatory tags: {sorted(required - set(spec.get('tags', {})))}",
            )


class AzureAssetRegistrationTests(unittest.TestCase):
    """Every component references these by name; something has to create them."""

    def test_the_environment_the_components_reference_is_registered(self) -> None:
        script = AZURE_DEPLOY / "register_assets.sh"
        self.assertTrue(script.is_file(), "nothing registers firmaware-env")
        text = script.read_text(encoding="utf-8")
        self.assertIn("az_ml environment create", text)

        for workflow in ("azure-deploy-dev.yaml", "azure-deploy-prod.yaml"):
            self.assertIn(
                "register_assets.sh",
                _read(".github", "workflows", workflow),
                f"{workflow} never registers the environment it deploys against",
            )

    def test_pipeline_data_assets_are_registered_before_training(self) -> None:
        pipeline = (AZURE_DEPLOY / "azureml" / "pipeline-train.yaml").read_text()
        registrar = (AZURE_DEPLOY / "register_assets.sh").read_text(encoding="utf-8")
        for asset in ("firmaware-deployment-events", "firmaware-config"):
            self.assertIn(f"azureml:{asset}@latest", pipeline)
            self.assertIn(asset, registrar, f"{asset} is referenced but never created")

    def test_environment_does_not_layer_conda_on_a_conda_free_image(self) -> None:
        # The runtime image is python:3.11-slim. A conda_file cannot build on it,
        # so the Azure packages belong in the image instead.
        spec = yaml.safe_load((AZURE_DEPLOY / "azureml" / "environment.yaml").read_text())
        self.assertNotIn("conda_file", spec)
        self.assertFalse((AZURE_DEPLOY / "azureml" / "conda.yaml").exists())

        pyproject = _read("pyproject.toml")
        self.assertIn("azureml-inference-server-http", pyproject)
        self.assertIn("azureml-mlflow", pyproject)
        self.assertIn("PIP_EXTRAS=train,azure", (AZURE_DEPLOY / "build.sh").read_text())

    def test_serving_image_starts_an_inference_server(self) -> None:
        """AML does not override a custom image's entrypoint on an online
        deployment: it injects AZUREML_ENTRY_SCRIPT, mounts the code directory
        and runs the image as-is. The CLI entrypoint would exit immediately, so
        every deployment would crash-loop and never pass a readiness probe.
        """
        dockerfile = _read("Dockerfile")
        azureml_stage = dockerfile.split("FROM runtime AS azureml")[1]
        self.assertIn("azmlinfsrv", azureml_stage)
        # Shell form, or $AZUREML_ENTRY_SCRIPT is passed through as a literal.
        self.assertIn('CMD ["sh", "-c"', azureml_stage)
        self.assertIn("AZUREML_ENTRY_SCRIPT", azureml_stage)
        # Reset, or an AML component's explicit `command:` is appended to the
        # CLI entrypoint inherited from the runtime stage.
        self.assertIn("ENTRYPOINT []", azureml_stage)

        # The GCP path and the local CLI both depend on the runtime entrypoint,
        # which is why this is a separate target rather than an edit in place.
        self.assertIn('ENTRYPOINT ["python", "-m", "firmaware"]', dockerfile)
        self.assertIn("--target azureml", (AZURE_DEPLOY / "build.sh").read_text())

    def test_azure_dependency_set_is_exactly_pinned(self) -> None:
        """The azure extra must resolve, and only exact pins make that stable.

        An open `azureml-mlflow` range lets pip backtrack into 1.5x releases
        that cap azure-storage-blob below what the datalake client requires,
        turning a solvable set into ResolutionImpossible. mlflow is capped by
        azureml-mlflow in turn, so it is pinned to the highest version both
        accept and shared by every environment rather than diverging per-cloud.
        """
        pyproject = _read("pyproject.toml")
        self.assertIn('"azureml-mlflow==1.62.0.post5"', pyproject)
        self.assertIn('"mlflow==3.13.0"', pyproject)
        self.assertNotRegex(pyproject, r'"azureml-mlflow[><~]')

    def test_ci_builds_the_serving_target_it_scans(self) -> None:
        """The scan is also the only automated proof that the azure extra still
        resolves and that the serving stage still builds."""
        workflow = _read(".github", "workflows", "azure-ci.yaml")
        build = workflow.split("name: Build image")[1]
        self.assertIn("PIP_EXTRAS=train,azure", build)
        self.assertIn("--target azureml", build)

    def test_serving_port_and_routes_agree_with_the_server_defaults(self) -> None:
        spec = yaml.safe_load((AZURE_DEPLOY / "azureml" / "environment.yaml").read_text())
        inference = spec["inference_config"]
        port = int(re.search(r"--port (\d+)", _read("Dockerfile")).group(1))
        for route in ("liveness_route", "readiness_route", "scoring_route"):
            self.assertEqual(inference[route]["port"], port)
        self.assertEqual(inference["scoring_route"]["path"], "/score")


class AzureSecurityCheckTests(unittest.TestCase):
    """A control that cannot fail is not a control."""

    def test_append_only_role_lookup_matches_the_name_terraform_creates(self) -> None:
        # Terraform names it "Storage Blob Data Appender (firmaware-dev)" so two
        # environments do not collide, so an equality check never matches.
        terraform = (AZURE_INFRA / "modules" / "identity" / "main.tf").read_text()
        self.assertIn('name        = "Storage Blob Data Appender (${var.name_prefix})"', terraform)

        check = (AZURE_DEPLOY / "security_check.sh").read_text(encoding="utf-8")
        self.assertIn("starts_with(roleName, 'Storage Blob Data Appender')", check)

    def test_diagnostics_are_checked_where_categories_actually_exist(self) -> None:
        # Storage exposes diagnostic categories on the blob service, not on the
        # account, so including the bare account id guarantees a false failure.
        check = (AZURE_DEPLOY / "security_check.sh").read_text(encoding="utf-8")
        self.assertIn('"${sa_id}/blobServices/default"', check)
        self.assertNotIn('"${acr_id}" "${sa_id}"', check)

    def test_credential_check_reports_unverified_rather_than_passing(self) -> None:
        # The deploy identity cannot read Microsoft Graph. Swallowing that error
        # and printing a pass would assert something never checked.
        check = (AZURE_DEPLOY / "security_check.sh").read_text(encoding="utf-8")
        self.assertIn("UNVERIFIED", check)
        # The Graph passwordCredential object has no `type` property, so a
        # [?type=='Password'] filter matched nothing and the check was vacuous.
        commands = [line for line in check.splitlines() if "az ad app credential list" in line]
        self.assertTrue(commands, "the credential check no longer queries credentials")
        for line in commands:
            self.assertNotIn("type=='Password'", line)
        self.assertTrue(
            any("--cert" in line for line in commands),
            "certificate credentials are never checked",
        )


class AzurePrivilegeTests(unittest.TestCase):
    def test_role_assignment_condition_actually_denies_owner(self) -> None:
        # ForAnyOfAnyValues:GuidNotEquals is true whenever the list holds two
        # different GUIDs, so it permits everything including Owner. The
        # documented deny-list form is ForAllOfAllValues.
        terraform = (AZURE_INFRA / "modules" / "identity" / "main.tf").read_text()
        self.assertIn("ForAllOfAllValues:GuidNotEquals", terraform)
        self.assertNotIn("ForAnyOfAnyValues:GuidNotEquals", terraform)

    def test_ci_authenticates_as_the_identity_that_holds_the_roles(self) -> None:
        # Terraform used to create a second identity and grant it everything,
        # while CI authenticated as the bootstrap one and had nothing.
        terraform = (AZURE_INFRA / "modules" / "identity" / "main.tf").read_text()
        self.assertIn('data "azurerm_user_assigned_identity" "cicd"', terraform)
        self.assertNotIn('resource "azurerm_user_assigned_identity" "cicd"', terraform)

        # And Terraform must not own the federation either: it cannot create the
        # credential that authenticates the apply that creates it.
        self.assertNotIn("azurerm_federated_identity_credential", terraform)
        bootstrap = (AZURE_INFRA / "bootstrap.sh").read_text(encoding="utf-8")
        self.assertIn("az identity federated-credential create", bootstrap)

    def test_bootstrap_lets_the_ci_identity_read_itself(self) -> None:
        """The data source above is a control-plane GET on the identity.

        bootstrap's other grant, Storage Blob Data Contributor, is pure
        dataActions and confers no such read, so without this the very first CI
        plan dies with AuthorizationFailed before evaluating anything. Reader
        rather than Managed Identity Contributor: write would let the pipeline
        add federated credentials to its own identity and authorise new subjects.
        """
        bootstrap = (AZURE_INFRA / "bootstrap.sh").read_text(encoding="utf-8")
        grant = bootstrap.split('"Reader" "${identity_id}"')
        self.assertEqual(len(grant), 2, "exactly one Reader grant, on the identity")
        self.assertIn("ensure_role_assignment", grant[0][-200:])
        self.assertNotIn('"Managed Identity Contributor" "', bootstrap)

    def test_azure_client_id_is_scoped_per_environment(self) -> None:
        """bootstrap creates a separate identity per environment. Told to set
        AZURE_CLIENT_ID at repository scope, an operator bootstrapping the second
        environment silently overwrites the first, and OIDC then fails for
        whichever one lost."""
        bootstrap = (AZURE_INFRA / "bootstrap.sh").read_text(encoding="utf-8")
        guidance = bootstrap.split("AZURE_CLIENT_ID       =")[1]
        self.assertNotIn("repository variables", guidance)
        self.assertIn("ENVIRONMENT's variables", bootstrap)

    def test_prod_states_its_warm_instance_floor_explicitly(self) -> None:
        # deploy_endpoint.sh falls back to 0, which is a cold start on the first
        # request after a quiet period.
        self.assertIn("online_min_instances = 1", _read("infra", "azure", "envs", "prod.tfvars"))
        self.assertIn("online_min_instances = 0", _read("infra", "azure", "envs", "dev.tfvars"))
        self.assertIn("online_min_instances", _read("infra", "azure", "outputs.tf"))
        self.assertIn(
            "FIRMAWARE_MIN_INSTANCES: ${{ steps.tf.outputs.min_instances }}",
            _read(".github", "workflows", "azure-deploy-prod.yaml"),
        )


if __name__ == "__main__":
    unittest.main()
