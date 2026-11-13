### workbench — build-host helpers.
###
### workbench is the federation's shared tool tier: a FastAPI tool API (`api`)
### plus a sandboxed code and document executor (`executor`), joined on the
### external `workbench-net`. Its state (workspaces, job records) lives on the
### external `workbench-data` volume, so `docker compose down -v` is safe; only
### `make nuke` (added with the compose files) removes it.
###
### The lifecycle targets (network/volumes/build/bundle/up/up-dev/stop/down/
### logs/pre-commit/verify/test) + the versioned image tag come from
### make/common.mk, vendored from nos-tromo/.github. Only workbench-specific
### config and the help text live here.

.DEFAULT_GOAL := help

REPO     := workbench
NETWORKS := workbench-net
VOLUMES  := workbench-data
include make/common.mk

.PHONY: help
help:
	@echo "workbench — FastAPI tool API + sandboxed executor."
	@echo
	@echo "  make network    create the external workbench-net"
	@echo "  make volumes    create the external workbench-data volume"
	@echo "  make build      build the api + executor images"
	@echo "  make bundle     ship images as a versioned .tar.gz (latest annotated release tag)"
	@echo "  make bundle-dev like 'bundle', but from the current working tree (dev/soak)"
	@echo "  make up         start api + executor, detached; no build (production shape, no host ports)"
	@echo "  make up-dev     like 'up' + host ports; detached, no build (run 'make build' first)"
	@echo "  make dev        build, then up-dev"
	@echo "  make stop       stop containers (keep them)"
	@echo "  make down       stop + remove containers"
	@echo "  make logs       tail combined logs"
	@echo "  make pre-commit run pre-commit hooks (ruff + pyrefly)"
	@echo "  make verify     pre-push gate: pre-commit (ruff + pyrefly); mirrors CI's lint gate"
	@echo "  make test       run pytest (test-backend)"
