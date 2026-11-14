### workbench — build-host helpers.
###
### workbench is the federation's shared tool tier: a FastAPI tool API (`api`)
### plus a sandboxed code and document executor (`executor`), joined on the
### external `workbench-net`. Its state (workspaces, job records) lives on the
### external `workbench-data` volume, so `docker compose down -v` is safe; only
### `make nuke` removes it, after you type `workbench` to confirm.
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
	@echo "Setup (first time):"
	@echo "  cp .env.example .env   # then set OPEN_WEBUI_WORKBENCH_KEY (at least 32 characters)"
	@echo
	@echo "Lifecycle:"
	@echo "  make help       this list"
	@echo "  make network    create the external workbench-net"
	@echo "  make volumes    create the external workbench-data volume"
	@echo "  make build      build the api + executor images"
	@echo "  make bundle     ship both images as a versioned .tar.gz (latest annotated release tag)"
	@echo "  make bundle-dev like 'bundle', but from the current working tree (dev/soak)"
	@echo "  make up         start api + executor, detached; no build (production shape, no host ports)"
	@echo "  make up-dev     like 'up' + the api's host port; detached, no build (run 'make build' first)"
	@echo "  make dev        build, then up-dev"
	@echo "  make stop       stop containers (keep them)"
	@echo "  make down       stop + remove containers (the data volume stays)"
	@echo "  make nuke       DESTROY the workbench-data volume, all workbench state (type 'workbench' to confirm)"
	@echo
	@echo "Observability:"
	@echo "  make health     container status, then the api's /healthz from inside its container"
	@echo "  make logs       tail combined logs"
	@echo
	@echo "Retention (the api must be up):"
	@echo "  make retention-dry-run [DAYS=n]"
	@echo "                  what retention would delete now, in the running api: counts and ages only;"
	@echo "                  nothing is recorded or deleted (DAYS=n previews another period)"
	@echo
	@echo "Checks:"
	@echo "  make pre-commit run pre-commit hooks (ruff + pyrefly)"
	@echo "  make verify     pre-push gate: pre-commit (ruff + pyrefly); mirrors CI's lint gate"
	@echo "  make test       run both suites: test-backend, then test-frontend"
	@echo "  make test-backend   run pytest"
	@echo "  make test-frontend  run the frontend's vitest (workbench has no frontend/)"
	@echo "  make test-executor-root  executor tests as root in the executor image: production shape, then Docker's default /dev/shm"
	@echo "  make test-isolation      build + start the test stack (own project, volume, network), run pytest -m isolation, remove it"

### Container status, then the api's /healthz asked from inside its container:
### production publishes no port. Both services report (healthy) in the status
### once their healthchecks pass (the executor's: a socket listens on its path).
.PHONY: health
health:
	@$(COMPOSE) ps --format '{{.Name}}\t{{.State}}\t{{.Status}}'
	@echo
	@$(COMPOSE) exec -T api python -c "import urllib.request as u; print('api /healthz:', u.build_opener(u.ProxyHandler({})).open('http://localhost:8000/healthz', timeout=4).read().decode())"

### Destructive: stop + remove this project's containers, then delete the
### external data volume: every workspace file, job record and the database,
### all workbench state. Interactive confirm. The volume is external, so
### `down -v` does NOT remove it; it is deleted explicitly once its containers
### are gone. The only target that removes it.
.PHONY: nuke
nuke:
	@echo "This will DESTROY all workbench state (volume: $(VOLUMES))."
	@read -p "Type 'workbench' to confirm: " confirm && [ "$$confirm" = "workbench" ] \
	  || (echo "aborted"; exit 1)
	$(COMPOSE) down
	docker volume rm $(VOLUMES)

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
### slot uids must reach it. docker/ is mounted too, read-only: the image leaves
### it out, and the healthcheck's test runs the probe written in compose.yaml.
EXECUTOR_TEST_IMAGE  := workbench-executor:test-root
EXECUTOR_ROOT_RUN    := docker run --rm --init --read-only --network none --user 0:0 \
	--security-opt no-new-privileges:true --cap-drop ALL \
	$(foreach cap,SETUID SETGID CHOWN FOWNER DAC_OVERRIDE KILL,--cap-add $(cap)) \
	--tmpfs /tmp:mode=0700 --tmpfs /scratch:mode=0755 -e WORKBENCH_TEST_PUBLIC_TMP=/scratch \
	-v "$(CURDIR)/tests:/app/tests:ro" -v "$(CURDIR)/docker:/app/docker:ro" -w /app
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
### included, whatever the tests did: the EXIT trap runs on success, on failure
### and, through the INT/TERM/HUP trap, on Ctrl-C or a cancelled CI job. It
### names the test project alone.
TEST_PROJECT := workbench-test
TEST_COMPOSE := docker compose -p $(TEST_PROJECT) -f $(COMPOSE_FILE) -f docker/compose.test.yaml

.PHONY: test-isolation
test-isolation:
	@trap '$(TEST_COMPOSE) down -v --remove-orphans' EXIT; trap 'exit 130' INT TERM HUP; \
	status=0; \
	$(BUILD_ENV) $(TEST_COMPOSE) up -d --build --wait --wait-timeout 300 || status=$$?; \
	if [ $$status -eq 0 ]; then \
		WORKBENCH_TEST_PROJECT=$(TEST_PROJECT) uv run --locked pytest -m isolation -q -rfEsxX || status=$$?; \
	else \
		$(TEST_COMPOSE) ps --all; $(TEST_COMPOSE) logs --tail=30; \
	fi; \
	exit $$status
