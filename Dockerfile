FROM python:3.11-slim AS builder

# Which optional-dependency sets go into the image. Defaults to `train`, so the
# GCP Cloud Run Jobs build exactly what they always did; the Azure build passes
# `train,azure` to add the MLflow bridge, the ADLS client and the inference
# server. A build arg rather than a second Dockerfile keeps one build recipe, so
# the two clouds cannot drift apart in their base layers.
ARG PIP_EXTRAS=train

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN python -m venv "${VIRTUAL_ENV}" \
    && pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[${PIP_EXTRAS}]"

FROM python:3.11-slim AS runtime

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV MLFLOW_TRACKING_URI=sqlite:////tmp/firmaware-mlflow.db
ENV FIRMAWARE_MLFLOW_ARTIFACT_ROOT=/tmp/firmaware-mlruns

RUN apt-get update \
    && apt-get install --no-install-recommends --yes libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 firmaware \
    && useradd --uid 10001 --gid firmaware --create-home firmaware

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=firmaware:firmaware config.yaml ./config.yaml

USER 10001:10001
ENTRYPOINT ["python", "-m", "firmaware"]

# Serving stage, built only for Azure ML managed online endpoints.
#
# It exists because AML does NOT override a custom image's entrypoint on an
# online deployment: it injects AZUREML_ENTRY_SCRIPT / AZUREML_MODEL_DIR, mounts
# the code_configuration directory, then runs the image as-is and probes the
# routes declared in the environment's inference_config. The `runtime` stage
# above runs the CLI and exits, so a deployment built from it would crash-loop
# and never pass a readiness probe.
#
# Kept as a separate target rather than changing `runtime` because GCP's Cloud
# Run Jobs and this repo's own CLI both depend on that entrypoint. Same layers,
# same packages, different final instruction.
FROM runtime AS azureml

USER root
# The inference server is in the `azure` extra, so this stage is only coherent
# when the image was built with it. Failing here, at build time, is far cheaper
# than a deployment that rolls out and then fails its probe.
RUN python -c "import azureml_inference_server_http" \
    || (echo "build this target with --build-arg PIP_EXTRAS=train,azure" >&2; exit 1)
USER 10001:10001

# Reset so AML pipeline components, which pass an explicit `command:`, are not
# appended as arguments to the CLI entrypoint inherited from `runtime`.
ENTRYPOINT []

# Shell form on purpose: the server needs $AZUREML_ENTRY_SCRIPT expanded at
# runtime, and AML sets it only once the container starts. Port 5001 with
# liveness `/` and scoring `/score` are azmlinfsrv's documented defaults and
# must match inference_config in deploy/azure/azureml/environment.yaml.
CMD ["sh", "-c", "azmlinfsrv --entry_script \"${AZUREML_ENTRY_SCRIPT:-/var/azureml-app/score.py}\" --port 5001"]
