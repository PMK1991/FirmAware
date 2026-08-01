# FirmAware notebooks

These notebooks intentionally duplicate the ML logic in explicit cells. They
are the traceable research path that should be understood before the same
behavior is abstracted into production modules. Run them in order:

1. `01_data_exploration.ipynb` — contract-aware data quality, target, temporal,
   categorical, and numeric exploration.
2. `02_feature_engineering.ipynb` — explicit feature formulas, leakage
   removal, train-only imputation/encoding/scaling, and unseen categories.
3. `03_hyperparameter_tuning.ipynb` — expanding time folds, deterministic
   candidate search, pooled out-of-time threshold selection, and fold
   stability.
4. `04_training_and_evaluation.ipynb` — tracked training controls and detailed
   evaluation of the published artifacts.
5. `05_local_deployment_test.ipynb` — local CLI append-only checks, OOD
   assertions, and optional non-root Docker parity.

## Start Jupyter

From the repository root:

```powershell
python -m pip install -e ".[train]"
python -m pip install -r notebooks\requirements.txt
python -m jupyter lab notebooks
```

The real CSV files remain under the ignored `data\` directory. The notebooks
locate the repository whether their kernel starts at the root or in
`notebooks\`.

## Runtime controls

- Notebook 3 defaults to `FAST_MODE = True`: 5,000 chronological rows, two
  expanding folds, two logistic candidates, and three XGBoost candidates.
  Set it to `False` to use the complete configured search.
- Notebook 4 writes a standalone research artifact bundle under
  `notebook_runs\model\`; it never overwrites production artifacts.
- Notebook 5 reloads that research bundle and repeats scoring transformations
  explicitly. Its append-only output stays under `notebook_runs\deployment`.

Generated notebook artifacts live under `notebook_runs\`, which is ignored.
The committed notebooks include the latest verified outputs so exploration,
metrics, plots, and deployment assertions are reviewable directly on GitHub.
