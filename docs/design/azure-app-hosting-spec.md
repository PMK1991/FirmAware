# Spec — Host the FirmAware page on Azure Container Apps

> Implementation specification for a coding agent. Adds a keyless, VNet-integrated home for `app.py` that reads published scores directly from ADLS.
> Version 1.0 · 2026‑08‑01 · Extends the existing `infra/azure` root and `deploy/azure` scripts. **Do not modify `src/firmaware/` or `app.py`** — the read path already works; what is missing is somewhere to run it with an identity.
> Streamlit Community Cloud keeps serving the committed sample from `.[app]`. This is a second deployment of the same file, not a replacement.

---

## 0. Why this exists, stated once

`app.py` resolves `abfss://` through `io.py` and `DefaultAzureCredential` today. On Community Cloud there is no managed identity, so reading Azure would mean a client secret in Streamlit secrets — which contradicts the zero-keys posture the rest of the Azure deployment enforces. Container Apps supplies a managed identity, so the page can read real scores with **no credential material anywhere**.

---

## 1. Non-negotiables

Each is an acceptance test in §7.

1. **No secrets.** The container app has zero `secrets` entries. Storage and ACR access are via user-assigned managed identity. `az containerapp show --query properties.configuration.secrets` returns empty.
2. **Read-only by construction.** The app identity gets `Storage Blob Data Reader` on `scores`, `artifacts`, and `data` — and nothing else. It must **not** hold `scores_appender`, any Contributor role, or any Key Vault access. A write attempt from the app identity must fail with 403.
3. **Not anonymously public.** Ingress is external HTTPS, but behind **Entra ID authentication** (Container Apps built-in auth, `require_authentication = true`, unauthenticated requests → 302 to login). A page rendering GO/NO_GO decisions for named sites and devices is not a public artefact.
4. **Private egress to storage.** The app reaches ADLS over the existing private endpoints via VNet integration, not the public internet. Storage stays `public_network_access_enabled = false`.
5. **Digest-pinned images.** The revision references an ACR digest, never a tag.
6. **Tagged and observed.** The six mandatory tags; console and system logs to the existing Log Analytics workspace.
7. **HTTPS only.** `allow_insecure_connections = false`, minimum TLS 1.2.

---

## 2. Deliverables

```
Dockerfile                                 new `app` stage (§3)
infra/azure/modules/app/                    main.tf variables.tf outputs.tf versions.tf
infra/azure/modules/network/                add snet-apps, delegated to Microsoft.App/environments
infra/azure/modules/identity/               add app identity + three reader assignments
infra/azure/main.tf                         wire module "app"
infra/azure/envs/{dev,prod}.tfvars          app_min_replicas, app_allowed_principals
deploy/azure/deploy_app.sh                  build, push, create revision, shift traffic
deploy/azure/rollback_app.sh                revision traffic flip
deploy/azure/smoke_test_app.sh              §6
.github/workflows/azure-deploy-*.yaml       add app build + deploy + smoke steps
```

---

## 3. Container image — new `app` stage

Add to the existing multi-stage `Dockerfile`, after `runtime`:

```
FROM runtime AS app
```

- Built with `--build-arg PIP_EXTRAS=app,azure` and `--target app`. Fail the build with a clear message if `streamlit` or `azure.identity` is absent, mirroring how the `azureml` stage guards its extras.
- Runs as the existing non-root `10001:10001`. Never root.
- `EXPOSE 8501`; entrypoint `streamlit run app.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true`.
- `.streamlit/config.toml` already exists in the repo — do not duplicate its settings as flags; add only what running behind a proxy requires (`server.enableXsrfProtection`, `browser.gatherUsageStats = false`).
- The image must contain **no** training stack: `.[app,azure]` only. Verify size stays close to the Community Cloud build rather than the AML one.

---

## 4. Infrastructure

**Network.** New `snet-apps`, delegated to `Microsoft.App/environments`. Size it per the workload profile chosen (`/27` minimum for Consumption workload profiles; use `/23` if unsure and record the choice). NSG denies inbound from Internet except the Container Apps Environment's required inbound; egress to the private-endpoint subnet only.

**Container Apps Environment.** VNet-integrated into `snet-apps`, `internal_load_balancer_enabled = false` (external ingress, gated by auth per §1.3), Log Analytics workspace = the existing one from `foundation`. Zone redundancy in prod only.

**Container App.**
- Identity: `UserAssigned`, the new app identity.
- Registry: `login_server` = existing ACR, `identity` = the app identity (which therefore needs `AcrPull`). No admin credentials.
- Ingress: external, `target_port = 8501`, `transport = auto`, `allow_insecure_connections = false`.
- Auth: Container Apps built-in authentication, Entra ID provider, `require_authentication = true`, `unauthenticated_client_action = RedirectToLoginPage`. Restrict to the tenant; `app_allowed_principals` in tfvars records who may reach it.
- Probes: liveness and readiness against `/_stcore/health` (Streamlit's own health endpoint), not `/`.
- Scale: `min_replicas = var.app_min_replicas` — **0 in dev, 1 in prod**. Note in the module why: scale-to-zero costs a cold start and drops session state, which is acceptable for a read-only page in dev and not in prod.
- Env vars, sourced from the storage module's existing `container_uris` output — no hard-coded account names:
  ```
  FIRMAWARE_SCORES_URI    = "${container_uris["scores"]}/scores"
  FIRMAWARE_ARTIFACTS_URI = container_uris["artifacts"]
  FIRMAWARE_UPCOMING_URI  = "${container_uris["data"]}/upcoming_deployments.csv"
  AZURE_CLIENT_ID         = app identity client id   # DefaultAzureCredential needs this to pick the UAMI
  ```
  `AZURE_CLIENT_ID` is the one non-obvious requirement: with a user-assigned identity, `DefaultAzureCredential` cannot guess which identity to use and will fail at runtime without it.

**Identity module.** Add `azurerm_user_assigned_identity.app` and exactly four assignments, each with the file's existing style of justifying comment: `Storage Blob Data Reader` on `scores`, `artifacts`, and `data`; `AcrPull` on the registry. Nothing else. State explicitly in a comment that this identity is deliberately excluded from `scores_appender` — the page must never be able to add to the evidence it displays.

**Diagnostics.** Console and system logs to Log Analytics; a KQL-based alert on repeated container restarts and on 5xx ingress responses, routed to the existing action group.

---

## 5. CI/CD

Extend the existing Azure deploy workflows rather than adding new ones:

- **Build** the `app` target alongside the `azureml` target, tag by commit SHA, push, capture the digest. Trivy scan blocking on HIGH/CRITICAL, same as the ML image — it is a different dependency surface (Streamlit, Plotly, the ADLS client) and nothing else covers it.
- **Deploy** by creating a new revision pinned to the digest with `--revision-suffix` = short SHA, at **0% traffic**.
- **Smoke test** the new revision through its revision-specific FQDN.
- **Promote** traffic 100% to the new revision; retain the previous revision (0% traffic, still provisioned) as the rollback target.
- Prod additionally requires the environment approval and the digest-match check already used for the endpoint.

---

## 6. `smoke_test_app.sh {env}`

Against the new revision's FQDN, before traffic moves:

1. `/_stcore/health` returns 200.
2. An unauthenticated request to `/` returns **302 to login**, not 200 — proves §1.3 is live.
3. With a token, `/` returns 200 and the HTML contains the sidebar caption **"Reading published scores from Azure Data Lake Storage."** This is the assertion that actually proves the app reached ADLS: that caption is only emitted when the resolved run URI is `abfss://`. If it renders "Sample run bundled with the checkout", the app fell back to demo fixtures and the deploy must fail.
4. The revision reports the expected image digest.

Print the digest, revision name, and resolved scores URI so every deploy log states what is live and where it is reading from.

---

## 7. Acceptance criteria

1. `terraform apply` adds the environment, app, identity, subnet, and role assignments; a second apply shows zero diff.
2. `az containerapp show --query properties.configuration.secrets` returns an empty list.
3. The app identity holds exactly four role assignments; `az role assignment list --assignee <app-identity>` shows no Contributor, no Owner, no `scores_appender`, no Key Vault role.
4. A write with the app identity to the `scores` container returns **403**.
5. Unauthenticated `GET /` returns 302; authenticated returns 200.
6. The rendered page shows the Azure Data Lake caption and a real run timestamp, not the demo sample.
7. Storage remains `public_network_access_enabled = false` and the app still reads successfully — proving traffic went over the private endpoint.
8. Revision rollback: `deploy/azure/rollback_app.sh {env}` shifts traffic to the previous revision in **under 30 s**, matching the endpoint's rollback story.
9. Trivy reports no unresolved HIGH/CRITICAL in the app image.
10. Community Cloud still builds from `requirements.txt` (`.[app]`) and still renders the committed sample — this work must not break the public demo.
11. Cost recorded in `infra/azure/README.md`: dev at `min_replicas = 0` should be near zero when idle.

---

## 8. Notes for the implementing agent

- Follow the conventions already in `infra/azure`: one module per concern, a justifying comment on every role assignment, `checkov:skip` only with a written reason.
- Do not add a Key Vault reference, a connection string, or a storage account key anywhere in this work. If something appears to need one, that is a signal the identity wiring is wrong, not that a secret is required.
- Do not change `app.py`. If the page cannot read Azure, the fault is in the identity, the env vars, or the network — fix it there. The one exception worth flagging back rather than fixing silently: if `DefaultAzureCredential` proves slow to resolve on cold start, the remedy is `ManagedIdentityCredential` in `io.py`, which is an application change and needs its own review.
- Record in `infra/azure/README.md`: the auth decision and who is allowed in, the subnet sizing choice, the rollback rehearsal time, and the idle cost.
