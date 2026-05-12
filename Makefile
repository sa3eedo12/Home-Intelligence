.DEFAULT_GOAL := help
.RECIPEPREFIX := >

COMPOSE_BASE := docker compose -f docker-compose.yml -f docker-compose.rocm.yml -f docker-compose.npu.yml
COMPOSE_DEV := $(COMPOSE_BASE) -f docker-compose.dev.yml

.PHONY: up dev down logs ps pull-models prepare-npu-models test-sdk fmt lint help

up: ## Start core stack with ROCm and NPU overlays
>$(COMPOSE_BASE) up -d

dev: ## Start stack with development port exposure
>$(COMPOSE_DEV) up -d

down: ## Stop stack
>docker compose down

logs: ## Tail stack logs
>docker compose logs -f

ps: ## Show stack status
>docker compose ps

pull-models: ## Pull Ollama models
>bash infra/ollama/pull.sh

prepare-npu-models: ## Prepare all NPU models on host
>bash infra/lemonade/conversion/prepare_router.sh
>bash infra/lemonade/conversion/prepare_embed.sh
>bash infra/lemonade/conversion/prepare_whisper.sh
>bash infra/lemonade/conversion/prepare_vision.sh

test-sdk: ## Run SDK tests
>cd shared && pytest

test-orchestrator: ## Run orchestrator tests
>cd orchestrator && pytest

test-home-automation: ## Run home_automation agent tests
>cd agents/home_automation && pytest

test-system-health: ## Run system_health agent tests
>cd agents/system_health && pytest

test-dashboard-curator: ## Run dashboard_curator agent tests
>cd agents/dashboard_curator && pytest

fmt: ## Format SDK code
>ruff format shared

lint: ## Lint all Python code
>ruff check shared orchestrator agents

test: test-sdk test-orchestrator test-home-automation test-system-health test-dashboard-curator ## Run all tests

help: ## List available targets
>@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "%-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
