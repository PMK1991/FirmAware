IMAGE ?= firmaware:local
ENV ?= dev

.PHONY: build test run-local validate-local train-local deploy-dev

build:
	docker build --tag $(IMAGE) .

test:
	python -m pytest -q

run-local: build
	docker run --rm \
		--volume "$(CURDIR)/data:/workspace/data:ro" \
		--volume "$(CURDIR)/artifacts:/workspace/artifacts:ro" \
		--volume "$(CURDIR)/outputs:/workspace/outputs" \
		--env FIRMAWARE_DATA_URI=/workspace/data \
		--env FIRMAWARE_ARTIFACTS_URI=/workspace/artifacts \
		--env FIRMAWARE_SCORES_URI=/workspace/outputs/scores.csv \
		$(IMAGE) predict

validate-local: build
	docker run --rm \
		--volume "$(CURDIR)/data:/workspace/data:ro" \
		--env FIRMAWARE_DATA_URI=/workspace/data \
		$(IMAGE) validate --mode training

train-local: build
	docker run --rm \
		--volume "$(CURDIR)/data:/workspace/data:ro" \
		--volume "$(CURDIR)/artifacts:/workspace/artifacts" \
		--env FIRMAWARE_DATA_URI=/workspace/data \
		--env FIRMAWARE_ARTIFACTS_URI=/workspace/artifacts \
		$(IMAGE) train

deploy-dev:
	gh workflow run deploy-dev.yaml
