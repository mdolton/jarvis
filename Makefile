# Jarvis build & deploy.
#
# Image is published to GitHub Container Registry (ghcr.io). Override any
# variable on the command line, e.g.  make deploy TAG=v0.2.0
#
# Quick start:
#   make check        # lint + tests
#   make login        # one-time per shell (needs CR_PAT, see `login` target)
#   make deploy       # build multi-arch image and push to ghcr.io

IMAGE     ?= ghcr.io/mdolton/jarvis
# Image tag: short git SHA, suffixed -dirty if the tree has uncommitted changes.
TAG       ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)$(shell git diff --quiet 2>/dev/null || echo -dirty)
PLATFORMS ?= linux/amd64,linux/arm64
BUILDER   ?= jarvis-builder
GHCR_USER ?= mdolton

.DEFAULT_GOAL := help

PROD_COMPOSE := docker-compose.prod.yml

.PHONY: help check lint fmt test image deploy buildx-setup login \
        run up down logs prod-pull prod-up prod-down prod-logs clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

# --- Quality ---------------------------------------------------------------

check: lint test ## Run lint + tests (run this before deploy)

lint: ## Lint with ruff
	uv run ruff check jarvis tests

fmt: ## Auto-format / fix with ruff
	uv run ruff check --fix jarvis tests
	uv run ruff format jarvis tests

test: ## Run the test suite
	uv run pytest -q

# --- Images ----------------------------------------------------------------

image: ## Build a single-arch image locally (native arch, loaded into docker)
	docker build -t $(IMAGE):$(TAG) -t $(IMAGE):latest .

deploy: buildx-setup ## Build multi-arch image (amd64+arm64) and push to ghcr.io
	docker buildx build \
	  --builder $(BUILDER) \
	  --platform $(PLATFORMS) \
	  -t $(IMAGE):$(TAG) \
	  -t $(IMAGE):latest \
	  --push .
	@echo "pushed $(IMAGE):$(TAG) and $(IMAGE):latest ($(PLATFORMS))"

buildx-setup: ## Create the buildx builder if it doesn't exist
	@docker buildx inspect $(BUILDER) >/dev/null 2>&1 \
	  || docker buildx create --name $(BUILDER) --driver docker-container --bootstrap

login: ## Log in to ghcr.io (set CR_PAT to a GitHub PAT with write:packages)
	@test -n "$(CR_PAT)" || { echo "set CR_PAT to a GitHub PAT with the 'write:packages' scope"; exit 1; }
	@echo "$(CR_PAT)" | docker login ghcr.io -u $(GHCR_USER) --password-stdin

# --- Local run (docker compose) -------------------------------------------

run: up ## Alias for `up`

up: ## Build and start the stack locally via docker compose
	docker compose up -d --build

down: ## Stop the local stack
	docker compose down

logs: ## Follow logs from the local stack
	docker compose logs -f

# --- Production (run on the server; pulls from ghcr.io) --------------------

prod-pull: ## Pull the published image on the server (override JARVIS_IMAGE_TAG)
	docker compose -f $(PROD_COMPOSE) pull

prod-up: ## Start/refresh the prod stack from the pulled image
	docker compose -f $(PROD_COMPOSE) up -d

prod-down: ## Stop the prod stack
	docker compose -f $(PROD_COMPOSE) down

prod-logs: ## Follow logs from the prod stack
	docker compose -f $(PROD_COMPOSE) logs -f

# --- Housekeeping ----------------------------------------------------------

clean: ## Remove the local image tags and the buildx builder
	-docker image rm $(IMAGE):$(TAG) $(IMAGE):latest 2>/dev/null
	-docker buildx rm $(BUILDER) 2>/dev/null
