# Spec — Notebook-driven training on Azure ML: sweep → train → register

> Implementation specification for a coding agent. Adds a compute instance and a notebook-authored path that runs a **hyperparameter tuning (sweep) job**, then a **training job** on the winning configuration, then **model registration** — all tracked in MLflow.
> Version 1.0 · 2026‑08‑01 · Extends `infra/azure` and adds `notebooks/azure/`. **Do not modify `src/firmaware/`.**

---

## 0. Correction to the premise, because it changes the work

Nothing in this repository was built with **Azure ML Designer**. `deploy/azure/azureml/pipeline-train.yaml` is a **CLI v2 pipeline job**: `type: pipeline`, five `type: command` components under `components/`, each running a Python step from `components/steps/`. It is code, versioned in git, reviewed in PRs. Azure ML Studio renders any pipeline job as a graph, and that graph looks like Designer — that is almost certainly what was seen.

This matters for scope. The work below is **not a migration off Designer**; it is adding a second, interactive entry point to the same underlying code, plus a sweep stage that does not exist yet in any form.

| Path | Exists | Purpose |
|---|---|---|
| CLI v2 pipeline job | yes | Repeatable, reviewable, CI-triggerable training |
| Notebooks on a compute instance | **this spec** | Interactive authoring, sweep exploration, lineage inspection |
| Sweep (hyperparameter tuning) job | **this spec** | Does not exist on Azure today; tuning currently happens inside `train` |

Both paths must submit jobs that execute **the same packaged code**. That is the hard rule of §1.

---

## 1. Non-negotiables

1. **Notebooks orchestrate; they never reimplement.** Every cell that touches modelling calls `firmaware.*` or submits a job that does. A notebook that fits a model inline creates a second preprocessing path and is the exact defect this project exists to prevent. The local `notebooks/` deliberately duplicate logic for research — `notebooks/azure/` must not.
2. **Committed notebooks carry no outputs and no secrets.** `nbstripout` in a pre-commit hook; CI fails on any notebook containing `outputs` or an `execution_count`.
3. **The sweep's primary metric is cost, not accuracy.** Accuracy is meaningless here — the model sits below its base rate at the operating threshold by design. Optimise `expected_cost` (minimise), computed with the configured `cost_ratio_fn_fp`.
4. **Chronology is preserved inside every trial.** Trials use the same expanding-window time folds as `train.py`. A sweep that shuffles is measuring the wrong thing.
5. **Registration stays gated.** The notebook may not register a model that fails the evaluation gates; it calls the same gate the pipeline calls.
6. **The compute instance holds no credentials.** Managed identity or the signed-in user's Entra token. No keys, no `.env`, no `az login --service-principal`.
7. **Git is the source of truth.** Notebooks are cloned onto the instance, not authored only in the workspace. Anything that exists solely in the workspace file share is unversioned and does not count.

---

## 2. Deliverables

```
notebooks/azure/
  00_setup_and_connect.ipynb      workspace handle, MLflow URI, data asset check
  01_sweep_hyperparameters.ipynb  submit + monitor the sweep, read the winner
  02_train_and_register.ipynb     train on best params, evaluate, gate, register
  03_registry_and_lineage.ipynb   inspect versions, tags, metrics, promote/roll back
  README.md                       how to run, and the "orchestrate, never reimplement" rule
deploy/azure/azureml/
  sweep-train.yaml                the sweep job definition (CLI v2)
  components/train_trial.yaml     the trial component the sweep drives
  components/steps/train_trial_step.py
infra/azure/modules/workspace/    add compute instance + idle shutdown + schedule
infra/azure/envs/{dev,prod}.tfvars  instance size, owner principal, shutdown policy
deploy/azure/run_sweep.sh         submit the sweep from CI or a terminal
.github/workflows/azure-ci.yaml   add notebook lint + output-stripping gate
```

---

## 3. Compute instance

Terraform in the existing `workspace` module. It is a developer workstation inside the tenant, so it is treated as one:

- `azurerm_machine_learning_compute_instance`, size `Standard_DS3_v2` in dev (record the choice; it need only submit jobs, not train — the cluster trains).
- **Attached to the existing `snet-compute`**; `node_public_ip_enabled = false`. No public IP, no SSH — `ssh` block omitted entirely.
- **Assigned to a single user** (`assign_to_user` with the owner's object id from tfvars). A shared instance means shared identity and unattributable runs.
- **Idle shutdown mandatory**: `idle_time_before_shutdown = "PT30M"`, plus a stop schedule at 19:00 local on weekdays. An always-on instance is the single easiest way to make this deployment expensive.
- Identity: user-assigned, reusing the workspace identity's read access to `data` — the instance must be able to read inputs and write nothing to `scores`.
- Diagnostics to the existing Log Analytics workspace.

A cluster already exists for jobs. The instance never trains; it submits.

---

## 4. Notebook 1 — sweep job (`01_sweep_hyperparameters.ipynb`)

**`sweep-train.yaml`** (CLI v2), driven by the notebook via `MLClient.jobs.create_or_update`:

```
type: sweep
trial: ./components/train_trial.yaml     # runs firmaware training code, one config
sampling_algorithm: bayesian             # random for the first baseline run, then bayesian
objective:
  goal: minimize
  primary_metric: expected_cost          # must match the mlflow.log_metric name exactly
search_space:
  # XGBoost
  n_estimators:   {type: choice, values: [100, 200, 400]}
  max_depth:      {type: choice, values: [3, 5, 7]}
  learning_rate:  {type: loguniform, min_value: -5, max_value: -1}
  subsample:      {type: uniform, min_value: 0.6, max_value: 1.0}
limits:
  max_total_trials: 40
  max_concurrent_trials: 4               # must not exceed the cluster's max_nodes
  timeout: 7200
early_termination:
  type: bandit
  evaluation_interval: 2
  slack_factor: 0.2
```

Requirements on the trial step (`train_trial_step.py`):
- Accepts hyperparameters as CLI arguments, builds the model through `firmaware`'s existing candidate construction — it must not hand-roll an estimator.
- Runs the **expanding-window time folds**, sweeps the decision threshold inside the trial, and logs `expected_cost`, `recall_at_threshold`, `roc_auc`, `threshold`, and `false_negatives` via MLflow. The metric name for the objective must match `primary_metric` character for character; a mismatch produces a sweep that silently ranks nothing.
- Logs the data asset version and git SHA as tags on every child run.

The notebook: submits, streams status, then reads `best_child_run_id`, pulls its params and metrics into a dataframe, and plots `expected_cost` against trial index so the search's behaviour is visible. It writes the winning parameters to `outputs/best_params.json` for notebook 2 — **and** prints them, because the next notebook must be reproducible from the run id alone if the file is lost.

Also worth a cell: the parallel-coordinates or scatter view of parameters versus `expected_cost`. If the best trial is on a search-space boundary, the space is wrong and should be widened before trusting the winner.

---

## 5. Notebook 2 — training and registration (`02_train_and_register.ipynb`)

1. Load the winning parameters, either from `best_params.json` or by run id from the sweep.
2. Submit the **training job** — the existing `train` component with those hyperparameters, on the full training split. Not a local fit: the cluster runs it so the artefact is produced by the same environment CI uses.
3. Run the **evaluate + gate** step. Gates are the pipeline's: AUC ≥ baseline − 0.02, recall at threshold ≥ target, zero contract warnings. The notebook renders the gate result as a clear pass/fail, and **refuses to proceed on failure** — an `assert` with a readable message, not a printed warning that a tired reader scrolls past.
4. **Register through MLflow** with tags: git SHA, image digest, data asset version, sweep job name, best child run id, threshold, `cost_ratio_fn_fp`, and every test metric. The sweep lineage is the point — a registered version must answer "which search produced this?" without a human remembering.
5. Print the registered name and version, and the exact `az ml online-deployment` command to deploy it as green at 0% traffic. The notebook does **not** deploy; promotion stays with the reviewed workflow.

---

## 6. Notebook 3 — registry and lineage (`03_registry_and_lineage.ipynb`)

Read-only. Lists registered versions with their tags and metrics side by side, shows which version each endpoint deployment currently serves, and renders the metric history across versions so a regression is visible. Includes the rollback command as text, never executes it.

---

## 7. Tracking

One experiment, `firmaware`, for both entry points, so notebook runs and pipeline runs are comparable. Every run tagged `entry_point=notebook|pipeline`, `git_sha`, `data_asset_version`. Sweep child runs nest under the sweep parent automatically — do not flatten them. `MLFLOW_TRACKING_URI` comes from the workspace handle in notebook 00; it is never hard-coded.

---

## 8. Getting notebooks onto the instance

Repo is source of truth. On the instance: `git clone` into the user folder, work on a branch, commit and push from the instance terminal. The workspace file share is a working copy, not a store.

`nbstripout` installed as a pre-commit hook so outputs never reach git. CI enforces the same, because a hook only protects the machine it is installed on.

---

## 9. CI additions (`azure-ci.yaml`)

- `nbqa ruff notebooks/azure` — the same lint the package gets.
- Fail if any committed notebook has non-empty `outputs` or a non-null `execution_count`.
- Fail if a notebook in `notebooks/azure/` contains `fit(`, `GridSearchCV`, `OneHotEncoder`, or `StandardScaler` — a blunt but effective guard on §1.1. `notebooks/` (the local research set) is exempt, and the exemption is written into the check with its reason.
- `az ml` schema validation for `sweep-train.yaml` and `train_trial.yaml`, alongside the existing spec check.

---

## 10. Acceptance criteria

1. `terraform apply` creates the compute instance with no public IP, idle shutdown ≤ 30 min, and a single assigned user; a second apply shows zero diff.
2. A sweep of 40 trials completes; the Studio sweep view ranks children by `expected_cost`, and the best child's params match what the notebook reports.
3. A trial that logs a metric name other than `primary_metric` causes a visible failure, not a silently unranked sweep — verify once deliberately.
4. Training on the winning parameters produces a model whose test metrics are logged and whose registered version carries all tags from §5.4, including the sweep job name.
5. A gate failure blocks registration from the notebook: the registry gains no version and the notebook stops with a readable message.
6. Notebook and pipeline runs appear in the same experiment, distinguishable by `entry_point`.
7. CI rejects a notebook with committed outputs, and rejects a notebook that calls `fit(` under `notebooks/azure/`.
8. The compute instance can read `data` and **cannot** write to `scores` (403 verified).
9. Idle cost: the instance auto-stops within 30 minutes of inactivity, verified once by observation and recorded in `infra/azure/README.md`.
10. The existing CLI v2 pipeline still runs unchanged — this work adds a path, it does not replace one.

---

## 11. Notes for the implementing agent

- The sweep is the genuinely new capability. Everything else is a second front door onto code that already works — if a notebook needs a change in `src/firmaware/` to function, raise it rather than working around it in a cell.
- Start with `sampling_algorithm: random` for the first full run to get an unbiased picture of the space, then switch to `bayesian`. Bayesian on an untested space converges confidently onto a bad region.
- Record in `infra/azure/README.md`: instance size and why, shutdown policy, the sweep budget (trials × concurrency × runtime) and its cost, and whether the search space needed widening after the first run.
