# workbench federation integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring workbench into the federation and connect Open WebUI to it. This takes one PR in each of vllm-service, deploy, obs-plane and open-webui-service, plus an edit to the local workspace `infra/CLAUDE.md`.

**Architecture:** No new code. Each task changes the member repo's configuration and documentation, in that repo's own conventions, with a test where the repo has a suite. Each task is its own branch and PR in its repo. B1 can run at any time. B2 and B3 can run once workbench Task 16 (operator surface) is merged. B4 needs workbench Task 17 (contract test green). B5 comes last.

**Tech Stack:** Docker Compose, Make, nginx (edge-guard), Prometheus, Python stdlib unittest/pytest per repo.

**Spec:** `docs/superpowers/specs/2026-11-13-workbench-design.md` (§4 network and bring-up, §9 limits and audit, §10 deliverables elsewhere); companion plan `docs/superpowers/plans/2026-11-13-workbench-v1.md`.

## Global Constraints

- **Network:** `workbench-net` is external and created by `make network` in every member that joins it. Members are workbench `api` (alias `workbench-api`), open-webui-service's `open-webui`, and obs-plane's Prometheus.
- **Shared secret:** `OPEN_WEBUI_WORKBENCH_KEY` (≥ 32 characters), tagged `[secret] [shared: workbench]` in open-webui-service and `[shared: open-webui-service]` in workbench.
- **Audit emitter:** `("workbench", "api")`.
- **Bring-up:** inference → data → obs → **workbench** → apps (with Open WebUI) → edge.
- **Chat context:** `CHAT_MAX_MODEL_LEN=32768` (owner decision), measured on the production GPU before it goes live.
- **Open WebUI tool-call backstop:** `AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER=240`. This exceeds workbench's `WORKBENCH_JOB_WALL_S` (120) plus `WORKBENCH_QUEUE_WAIT_S` (60).
- **Each repo's rules apply:** its own CLAUDE.md, branch plus PR, a version bump in the release PR, and synthetic data only.

## Review Focus

1. **workbench is down while Open WebUI is up.** Expected: chats still work without tools, and Open WebUI does not fail to start. Test: B4 `test_tool_servers_follow_the_key` and a manual check in B4 Step 5.
2. **An admin adds another tool or terminal server at runtime through the admin UI.** Expected: edge-guard refuses it, so only the `.env` connection exists. Test: B4 `test_tool_server_config_writes_are_refused`.
3. **The upload file name reaches the audit store through Open WebUI's audit log.** Expected: never; `/api/v1/terminals` is excluded. Test: B4 `test_terminal_proxy_is_excluded_from_open_webui_audit`, and the end-to-end check in workbench Task 17.
4. **A host without obs-plane** (the obs tier is optional). Expected: workbench and Open WebUI still come up; `workbench-net` is created by them, not by obs-plane. Test: B2 Step 4.
5. **The 32k context does not fit on the GPU** next to the other models. Expected: vLLM refuses to start, loudly. The measurement in B1 records the concurrency vLLM reports. Test: B1 Step 3.

---

### Task B1: vllm-service — a 32k chat context

**Files (vllm-service):**
- Modify: `docker/compose.yaml` (the chat service's `--max-model-len "$${CHAT_MAX_MODEL_LEN:-8192}"` becomes `:-32768`), `.env.example` (`# CHAT_MAX_MODEL_LEN=32768`, with a line on why), `docs/configuration.md` (the row and its trade-off), version file per repo convention.

- [ ] **Step 1: Branch** — `git switch -c feat/chat-context-32k`.
- [ ] **Step 2: Make the change** described above.
- [ ] **Step 3: Measure on the production GPU** (manual, operator)
  - Start the chat service with the new value. From its log, record the line `Maximum concurrency for 32768 tokens per request: <N>x`, along with GPU memory headroom.
  - If vLLM refuses to start, raise `CHAT_GPU_MEMORY_UTILIZATION` within what the other backends on that GPU allow. Otherwise stop and report back.
  - Put both numbers in the PR.
- [ ] **Step 4: Verify** — Run `make verify` (vllm-service has pre-commit) and `docker compose … config -q`. Expected: green, quoted in the PR.
- [ ] **Step 5: Commit and open the PR** — `git commit -m "feat(chat): a 32k context for agentic chats"`. workbench's release (Task 18) waits for this merge.

### Task B2: deploy — ADR 0005 and the workbench tier

**Files (deploy):**
- Create: `docs/decisions/0005-workbench-shared-tool-tier.md`
- Modify:
  - `Makefile`: adds `WORKBENCH_DIR ?= workbench`. It brings workbench up after obs and before the apps, health-gated on `workbench-api` `/healthz` via `wait-healthy.sh`. It adds `$(WORKBENCH_DIR)` to `ENV_DIRS` and to the `network volumes` loop. `help` reflects the order.
  - `docs/runbooks/bring-up.md` and `docs/runbooks/airgap-transfer.md` (workbench's bundle).
  - `docs/tech-stack.md` and `docs/tech-stack.de.md`.
  - `README.md`, and the deploy version.

**ADR 0005 content** (format as `0004-retire-chorus.md`: Status / Date / Context / Decision / Alternatives considered / Consequences):
- **Context:** Open WebUI's tools run unsandboxed in-process (switched off in open-webui-service v0.13.0); the assistant needs office tools and Python.
- **Decision:**
  - workbench is the shared tool tier.
  - New invariant: tool execution is shared, never embedded. Compute tools are central; data tools stay with their owner.
  - Fourth network seam `workbench-net`; new data store `workbench-data` (7 days).
  - Tier order: inference → data → obs → workbench → apps → edge.
  - The delineation table docint / open-webui-service + workbench.
- **Alternatives:**
  - tools inside Open WebUI
  - inside docint
  - upstream open-terminal (no user isolation, needs the separately licensed orchestrator)
  - MCP-only (Open WebUI passes no files over MCP)
  - `inference-net` with a key only
- **Consequences:**
  - one more member to release and bundle
  - the Open WebUI terminal contract pinned in workbench's CI
  - version 2 (docint over MCP) as a separate decision

- [ ] **Step 1: Branch** — `git switch -c feat/workbench-tier`.
- [ ] **Step 2: Write the ADR and the Makefile change.**
- [ ] **Step 3: Verify the order without starting anything** — Run `make -n up`. Expected: the workbench `up` comes after obs and before the first `APP_DIRS` member and Open WebUI.
- [ ] **Step 4: Verify a host without obs** — Run `make -n up OBS_DIR=`. Expected: workbench is still brought up before the apps. Then run `make verify` or deploy's CI-equivalent checks (shellcheck, Makefile parse, yamllint) and quote them.
- [ ] **Step 5: Commit and open the PR** — `git commit -m "feat: workbench, the shared tool tier (ADR 0005)"`.

### Task B3: obs-plane — emitter, scrape and disk alert

**Files (obs-plane):**
- Modify:
  - `audit/verifier.py`: adds `("workbench", "api")` to `DEFAULT_EMITTERS`.
  - `docs/audit.md`: the emitter table row.
  - `docker/compose.yaml`: Prometheus joins external `workbench-net`, with a `networks:` entry `name: ${WORKBENCH_NET:-workbench-net}`.
  - `Makefile`: `make network` also creates `workbench-net`.
  - `prometheus/prometheus.yml`: job `workbench` scraping `workbench-api:8000` at `/metrics`.
  - `prometheus/rules.yml`: `WorkbenchDiskHigh` when `workbench_disk_used_bytes / workbench_disk_capacity_bytes > 0.8` for 15m; `WorkbenchRefusing` when `rate(workbench_job_refusals_total[15m]) > 0` for 15m.
  - `docs/coverage.md`, and the version file.
- Test: the verifier's existing test module gets a case `test_workbench_api_is_a_listed_emitter`. The rules test (if obs-plane has `promtool check rules` in CI) passes.

- [ ] **Step 1: Branch** — `git switch -c feat/workbench`.
- [ ] **Step 2: Write the failing verifier test**

```python
def test_workbench_api_is_a_listed_emitter() -> None:
    assert ("workbench", "api") in DEFAULT_EMITTERS
```

- [ ] **Step 3: Run it to verify it fails**, then make the changes above.
- [ ] **Step 4: Verify** — Run obs-plane's test suite, `promtool check config` / `check rules` (via its make target or the pinned Prometheus image), and `docker compose … config -q`. Expected: green, quoted in the PR.
- [ ] **Step 5: Commit and open the PR** — `git commit -m "feat: observe workbench (audit emitter, metrics, disk alert)"`.

### Task B4: open-webui-service — connect Open WebUI to workbench

**Files (open-webui-service):**
- Modify:
  - `docker/compose.yaml`: the `open-webui` service. It joins `workbench-net`, external, `name: ${WORKBENCH_NET:-workbench-net}`.
  - `Makefile`: `WORKBENCH_NET` like `EDGE_NET`; `make network` creates it.
  - `nginx/edge-guard.conf.template`
  - `.env.example`, `CLAUDE.md`, `README.md`
  - `VERSION`: 0.13.0 → 0.14.0
- Test: `retention/tests/test_compose_defaults.py` and `retention/tests/test_edge_guard.py`. Use Edit, not Write; the banner test changes from one banner to two.

**The compose environment changes:**
- `ENABLE_TOOL_SERVERS: ${OPEN_WEBUI_WORKBENCH_KEY:+true}`. On only with the key, like the forwarded-JWT pattern; unset means off.
- `TERMINAL_SERVER_CONNECTIONS` is one JSON connection:

  ```json
  [{"id":"workbench","name":"Workbench","enabled":true,"url":"${WORKBENCH_URL:-http://workbench-api:8000}","path":"/openapi.json","key":"${OPEN_WEBUI_WORKBENCH_KEY:-}","auth_type":"bearer","config":{"chat_uploads":"filesystem","access_grants":[{"principal_type":"user","principal_id":"*","permission":"read"}]}}]
  ```

- `USER_PERMISSIONS_FEATURES_DIRECT_TOOL_SERVERS: false`, pinned.
- `AIOHTTP_CLIENT_TIMEOUT_TOOL_SERVER: "240"`.
- `AUDIT_EXCLUDED_PATHS: "/terminals"`. Upstream matches it as a prefix of `/api(/v1)/terminals`. workbench records those accesses itself, by file id, so file names stay out of the audit store.
- `WEBUI_BANNERS` gets a second info banner. Its id is `workspace-period-${WORKBENCH_RETENTION_DAYS:-7}`. Its English text is "Files in the workspace are deleted ${WORKBENCH_RETENTION_DAYS:-7} days after they were added."; its `de-DE` text is "Dateien im Arbeitsbereich werden ${WORKBENCH_RETENTION_DAYS:-7} Tage nach dem Hinzufügen gelöscht."

**The edge-guard change:** a map `$owui_toolconfig` refuses `"~*^(POST|PUT|PATCH|DELETE) /api/v1/configs/(tool_servers|terminal_servers)(/|$)"` for every account. That covers its `/verify` too, which would let an admin make the server call any URL. The internal hand-off code is `422`, the next one after `418`–`421`. The answer is a 403 with detail "Tool and terminal servers are configured in .env in this deployment.".

- [ ] **Step 1: Branch** — `git switch -c feat/workbench`.
- [ ] **Step 2: Write the failing tests**

```python
# test_compose_defaults.py
def test_tool_servers_follow_the_key(self) -> None:  # "ENABLE_TOOL_SERVERS: ${OPEN_WEBUI_WORKBENCH_KEY:+true}"; the old "ENABLE_TOOL_SERVERS: false" is gone
def test_one_workbench_connection_with_filesystem_uploads(self) -> None:  # parse TERMINAL_SERVER_CONNECTIONS with placeholders replaced: one entry, auth_type bearer, chat_uploads filesystem, grant "*"
def test_terminal_proxy_is_excluded_from_open_webui_audit(self) -> None:  # AUDIT_EXCLUDED_PATHS: "/terminals"
def test_tool_call_backstop_exceeds_workbench_limits(self) -> None:  # int(timeout) > 120 + 60
def test_direct_tool_servers_stay_off(self) -> None:
# test_no_code_runs_inside_open_webui: drop "ENABLE_TOOL_SERVERS: false" from its list
# test_the_banner_names_the_period_in_english_and_german: now two banners; the second names the workspace period

# test_edge_guard.py
class ToolConfigWritesTest(unittest.TestCase):
    def test_tool_server_config_writes_are_refused(self) -> None:  # POST/PUT/PATCH/DELETE /api/v1/configs/tool_servers, /terminal_servers, .../verify -> "1"
    def test_tool_server_config_reads_pass(self) -> None:  # GET -> "0"; POST /api/v1/configs/banners -> "0"
    def test_a_flagged_request_answers_403(self) -> None:
```

- [ ] **Step 3: Run them to verify they fail** — `python3 -m unittest discover -s retention/tests -t retention`
- [ ] **Step 4: Make the changes**
  - Compose, Makefile and edge-guard as above.
  - `.env.example`: `OPEN_WEBUI_WORKBENCH_KEY`, `WORKBENCH_URL`, `WORKBENCH_RETENTION_DAYS`, `WORKBENCH_NET`.
  - `CLAUDE.md`:
    - "No code runs inside Open WebUI" now says tool servers are on only for the `.env` workbench connection.
    - "No content intake" adds: chat attachments go to workbench through the terminal proxy, never into Open WebUI's stores.
    - A new section "Workbench" covers the connection, the key, the audit exclusion, the banner and the timeout.
  - `README.md`: one paragraph.
- [ ] **Step 5: Verify**
  - Run the tests and `docker compose … config -q`; render edge-guard with `nginx -t` in the pinned image; check the env example with `check_env_examples.py`; probe the guard as in v0.13.0. Expected: green.
  - Manual: run the whole set (`deploy` `make up-dev`, or workbench + open-webui-service `up-dev`) on a dev host with a synthetic user:
    - attach a synthetic CSV and a DOCX template in a chat;
    - ask for an analysis and a filled report;
    - download it;
    - stop workbench and confirm plain chat still works.
- [ ] **Step 6: Commit and open the PR** — `git commit -m "feat: connect Open WebUI to workbench; release v0.14.0"`. Confirm CI ran on the last push.

### Task B5: the workspace map — `infra/CLAUDE.md`

This is a local workspace file, not a git repository, so it gets no PR.

- [ ] **Step 1: Make the edits**
  - The project table gets a row for `workbench` (tier "tools", "Shared tool tier: typed office tools and a network-less Python sandbox for AI services", stack "FastAPI + sandboxed executor"). The text says "twelve" where it says "eleven".
  - The diagram and invariants get `workbench-net` as the fourth seam and the invariant "Tool execution is shared, never embedded".
  - The bring-up order gets the workbench tier.
  - "Where to look in each project" gets workbench's `docs/`.
  - The release-workflow list adds workbench to the image-bundling repos; the Python-apps conventions list adds workbench.
- [ ] **Step 2: Verify** — `grep -n workbench infra/CLAUDE.md` shows each of the places above.
