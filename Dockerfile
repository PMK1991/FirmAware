FROM python:3.11-slim AS builder

ENV VIRTUAL_ENV=/opt/venv
ENV PATH="${VIRTUAL_ENV}/bin:${PATH}"

RUN python -m venv "${VIRTUAL_ENV}" \
    && pip install --no-cache-dir --upgrade pip setuptools wheel

WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

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
