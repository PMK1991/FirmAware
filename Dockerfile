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
    && useradd --uid 10001 --gid firmaware --create-home firmaware \
    # The base image ships its own setuptools under /usr/local, and it vendors
    # jaraco.context and wheel copies that carry their own CVEs. Nothing here
    # can reach any of it: the venv is built without --system-site-packages and
    # is first on PATH, so /opt/venv/bin/python never looks at /usr/local's
    # site-packages. Deleting it is a real reduction in attack surface rather
    # than a suppression, and it also removes the build-time toolchain from a
    # runtime image that has no business compiling anything.
    && rm -rf /usr/local/lib/python3.11/site-packages/setuptools \
              /usr/local/lib/python3.11/site-packages/setuptools-*.dist-info \
              /usr/local/lib/python3.11/site-packages/pkg_resources \
              /usr/local/lib/python3.11/site-packages/wheel \
              /usr/local/lib/python3.11/site-packages/wheel-*.dist-info

COPY --from=builder /opt/venv /opt/venv
# pip stays, and it is the only build-time tool that does.
#
# It was removed here once, because it is the last remaining source of scan
# findings: pip vendors its own copies of msgpack and setuptools under
# pip/_vendor and ships bom.cdx.json describing them, which trivy reads as
# installed packages even though nothing ever imports them. Removing it passed
# every check available before deployment -- the CLI ran, and the serving stage
# answered /score under AML's real contract, because azmlinfsrv never shells out
# to pip.
#
# The batch driver does. amlbi_main.py runs `python -m pip` while initialising,
# and without it the job dies at "No module named pip" with exit 42 before the
# scoring script is ever called, which surfaces as a batch job failure with an
# almost empty user log. Only a live batch run could show this.
#
# The two findings that removal was meant to clear are suppressed in
# .trivyignore instead, where the reasoning is written down and reviewable.

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

# Page stage, built for Azure Container Apps.
#
# Same base as everything else, so the page reads scores through exactly the
# `firmaware.io` code the pipeline writes them with -- a divergence there would
# show as a rendering bug rather than as a version mismatch.
#
# Built with `--build-arg PIP_EXTRAS=app,azure --target app`. Note `app,azure`
# and NOT `train,azure`: the page never trains, never scores, and never loads a
# model, so scikit-learn, xgboost and mlflow have no business being in an image
# that is reachable from the internet. That is the whole reason `train` is an
# extra rather than a base dependency.
#
# Stage order here is FORCED, not chosen. ACR Tasks build with the classic
# builder, which builds every stage that precedes --target regardless of whether
# the target depends on it. With `app` placed before `azureml`, a
# `--target azureml --build-arg PIP_EXTRAS=train,azure` build ran this stage's
# streamlit guard and died -- observed, not theorised.
#
# Last works in both directions: `--target azureml` stops before this stage, and
# `--target app` builds `azureml` first, whose guard needs
# azureml-inference-server-http, which lives in the `azure` extra that this
# target also installs. The cost is that an untargeted `docker build` now yields
# the page rather than the serving image, which is why
# DockerfileTargetTests::test_every_build_names_its_target matters more than the
# stage-order tripwire beside it: every build in this repository names a target.
FROM runtime AS app
USER root

# The same build-time guard the azureml stage uses, for the same reason: a
# missing extra should fail here, in seconds, rather than as a container that
# rolls out and then fails its readiness probe.
#
# Both halves matter. streamlit is the server; azure.identity is how the page
# authenticates to ADLS, and without it `firmaware.io` raises ContractViolation
# on the first abfss:// read -- which looks like a permissions problem.
RUN python -c "import streamlit, azure.identity" \
    || (echo "build this target with --build-arg PIP_EXTRAS=app,azure" >&2; exit 1)

# app.py resolves DEMO_DIR relative to its own file, so demo/ has to sit beside
# it. .streamlit/config.toml carries the theme, and Streamlit only reads it from
# the working directory -- which is why WORKDIR /app is inherited, not restated.
COPY --chown=firmaware:firmaware app.py ./app.py
COPY --chown=firmaware:firmaware demo ./demo
COPY --chown=firmaware:firmaware .streamlit ./.streamlit

USER 10001:10001

EXPOSE 8501

# Reset the CLI entrypoint inherited from `runtime`, exactly as the azureml
# stage does, so the CMD below is the whole command.
ENTRYPOINT []

# Only the flags that running as a container behind a proxy requires. The theme
# and gatherUsageStats already live in .streamlit/config.toml and are
# deliberately not repeated here: two places to change one setting is how the
# hosted page and the Community Cloud page drift apart.
#
# enableXsrfProtection is Streamlit's default and is stated anyway, because this
# is the deployment where it is load-bearing -- the page is served from a public
# hostname with no login in front of it, so turning it off (a common reflex when
# websockets misbehave behind a proxy) would leave the websocket handshake open
# to any origin.
CMD ["streamlit", "run", "app.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.enableXsrfProtection=true"]
