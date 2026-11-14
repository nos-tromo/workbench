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
	@echo "  make test-executor-root  executor tests as root in the executor image: production shape, then Docker's default /dev/shm"
	@echo "  make test-isolation      build + start the test stack (own project, volume, network), run pytest -m isolation, remove it"
	@echo "  make retention-dry-run [DAYS=n]"
	@echo "                  what retention would delete now, in the running api: counts and ages only;"
	@echo "                  nothing is recorded or deleted (DAYS=n previews another period)"

### What retention would delete now (workbench/api/retention.py), asked of the running api container: counts and ages
### by kind, never a name, an id, a user or a chat. It reads the database read-only and records and deletes nothing.
### DAYS=n previews another period than WORKBENCH_RETENTION_DAYS.
.PHONY: retention-dry-run
retention-dry-run:
	$(COMPOSE) exec -T api python -m workbench.api.retention dry-run $(if $(DAYS),--days '$(DAYS)')

### Root executor tests, in the executor image (its `test` stage adds pytest).
### Shape 1 is the executor's production shape: root-only /dev/shm and /tmp, no
### POSIX queues. Shape 2 keeps Docker's default /dev/shm and queue limit, where
### the SHARED_IPC_ONLY tests exercise the defence-in-depth cleanup (R28).
### Neither sets compose's kernel.shm_rmid_forced=1: under it a segment nobody
### is attached to dies with its creator, so the SysV cleanup tests could not
### make the leftovers they clear. /scratch stands in for the data volume: the
### slot uids must reach it.
EXECUTOR_TEST_IMAGE  := workbench-executor:test-root
EXECUTOR_ROOT_RUN    := docker run --rm --init --read-only --network none --user 0:0 \
	--security-opt no-new-privileges:true --cap-drop ALL \
	$(foreach cap,SETUID SETGID CHOWN FOWNER DAC_OVERRIDE KILL,--cap-add $(cap)) \
	--tmpfs /tmp:mode=0700 --tmpfs /scratch:mode=0755 -e WORKBENCH_TEST_PUBLIC_TMP=/scratch \
	-v "$(CURDIR)/tests:/app/tests:ro" -w /app
EXECUTOR_ROOT_PYTEST := python -m pytest tests/executor -q -rfEs -p no:cacheprovider -W error

.PHONY: test-executor-root
test-executor-root:
	$(BUILD_ENV) docker build --target test -f docker/Dockerfile.executor -t $(EXECUTOR_TEST_IMAGE) .
	@echo ">> executor tests as root: production shape (/dev/shm and /tmp root's alone, fs.mqueue.queues_max=0)"
	$(EXECUTOR_ROOT_RUN) --tmpfs /dev/shm:size=1m,mode=0755 --sysctl fs.mqueue.queues_max=0 \
		$(EXECUTOR_TEST_IMAGE) $(EXECUTOR_ROOT_PYTEST)
	@echo ">> executor tests as root: Docker's default /dev/shm and queue limit (SHARED_IPC_ONLY)"
	$(EXECUTOR_ROOT_RUN) $(EXECUTOR_TEST_IMAGE) $(EXECUTOR_ROOT_PYTEST)

### The test stack (docker/compose.test.yaml): project workbench-test, with its
### own data volume, network and :test images, so a developer's are never
### touched. No --env-file: .env does not reach it. Always torn down, volumes
### included, whatever the tests did.
TEST_PROJECT := workbench-test
TEST_COMPOSE := docker compose -p $(TEST_PROJECT) -f $(COMPOSE_FILE) -f docker/compose.test.yaml

.PHONY: test-isolation
test-isolation:
	@status=0; \
	$(BUILD_ENV) $(TEST_COMPOSE) up -d --build --wait --wait-timeout 300 || status=$$?; \
	if [ $$status -eq 0 ]; then \
		WORKBENCH_TEST_PROJECT=$(TEST_PROJECT) uv run --locked pytest -m isolation -q -rfEsxX || status=$$?; \
	else \
		$(TEST_COMPOSE) ps --all; $(TEST_COMPOSE) logs --tail=30; \
	fi; \
	$(TEST_COMPOSE) down -v --remove-orphans; \
	exit $$status
