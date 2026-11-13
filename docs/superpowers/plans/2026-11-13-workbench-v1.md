# workbench version 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build `workbench`, the federation's shared tool tier: a FastAPI `api` that speaks Open WebUI v0.13.3's terminal-server contract over a per-user, 7-day workspace, and a network-less `executor` that runs every file-touching tool and free Python in per-slot sandboxes, with the federation audit chain and retention.

**Architecture:** One Python package `workbench`, two images. `workbench.api` (one async process, uid 10001) owns authentication, virtual paths, the SQLite metadata store, the audit chain, the job queue, retention and `/metrics`; it never parses user files. `workbench.executor` (supervisor as root with a few capabilities, `network_mode: none`) receives jobs over a Unix socket on a shared volume, copies the chat folder into a per-job directory, runs `python -I -m workbench.executor.job` under the slot's uid with rlimits, and stages validated outputs back for the API to register.

**Tech Stack:** Python 3.12, uv, FastAPI, uvicorn, pydantic v2, prometheus-client, SQLite (stdlib); executor extra: pandas, numpy, scipy, matplotlib, openpyxl, xlsxwriter, python-docx, docxtpl, python-pptx, pypdf, odfpy, lxml, beautifulsoup4, charset-normalizer, markdown-it-py; dev: pytest, httpx, ruff, pyrefly, pre-commit, reportlab (PDF fixtures only).

**Spec:** `docs/superpowers/specs/2026-11-13-workbench-design.md`

## Global Constraints

- Python `>=3.12,<3.13`; base image `docker.io/library/python:3.12-slim-trixie`, pinned by tag **and** digest (pattern: translator `docker/Dockerfile.backend`).
- Every dependency's licence must allow distributing an Apache-2.0 image: no GPL, no AGPL (so no `extract-msg`, no PyMuPDF). `.msg` is out of version 1: `read_document` answers `unsupported_format` with "save it as .eml or PDF".
- Airgap: nothing installs or fetches at runtime (no `pip`, no `micropip`, no model download); `make bundle` ships both images.
- Hardening (deploy ADR 0001): every container `read_only`, `no-new-privileges`, `cap_drop: [ALL]`, tmpfs `/tmp`. The one exception is the executor supervisor: uid 0 with `cap_add: [SETUID, SETGID, CHOWN, FOWNER, DAC_OVERRIDE, KILL]`, justified in `docs/sandbox.md`. Jobs run as slot uids `20001…20016`, with no capabilities.
- State: external volume `workbench-data`, mounted at `/data`; removed only by `make nuke` behind a confirm.
- Network: external `workbench-net`; `api` joins it with alias `workbench-api`, port `8000`; `executor` has `network_mode: none`; nothing joins `inference-net` or `data-net`.
- Audit: vendored `workbench/utils/audit_event.py` and `workbench/utils/audit_runtime.py`, verbatim from `nos-tromo/.github` `configs/audit/` at `2715bedabe594a449e411182393e68da59c791f3` (v3.22); emitter `service = "workbench"`, compose service `api`; `AUDIT_OUTPUT=container`; never content, never a file name, only file ids.
- Defaults (spec §9, env names exact): `WORKBENCH_RETENTION_DAYS=7`, `WORKBENCH_RETENTION_INTERVAL_S=300`, `WORKBENCH_EXECUTOR_SLOTS=4`, `WORKBENCH_QUEUE_MAX=16`, `WORKBENCH_QUEUE_WAIT_S=60`, `WORKBENCH_JOBS_PER_USER=1`, `WORKBENCH_JOB_WALL_S=120`, `WORKBENCH_JOB_MEMORY_MB=1536`, `WORKBENCH_RESULT_MAX_KB=16`, `WORKBENCH_UPLOAD_MAX_MB=50`, `WORKBENCH_WORKSPACE_MAX_MB=500`; caller key `OPEN_WEBUI_WORKBENCH_KEY` (≥ 32 characters).
- Virtual namespace: `/workspace/<chat-id>/<name>`; `home` = `/workspace`; a request without `X-Session-Id` uses chat `default`.
- Conventions: ruff + pyrefly configs mirrored from `nos-tromo/.github/configs/python-strict/` (copy translator's `[tool.ruff]` / `[tool.pyrefly]` blocks, `target-version = "py312"`); `make/common.mk` and `scripts/bundle-lib.sh` vendored verbatim; every workflow `uses:` pinned to a full SHA with the version in a comment; `.env.example` in the federation format (`python3 -B ../deploy/scripts/check_env_examples.py .` passes).
- Data confidentiality: tests, fixtures, docs and commit messages use synthetic data only (e.g. `quarterly-figures.xlsx`, `user-a`), and no developer-machine paths.
- Before each push: `make verify` and `make test` green, output quoted in the PR.

## Review Focus

1. **File names in real life:** umlauts, spaces, very long names, names differing only by case (`Übersicht März.xlsx`, `übersicht märz.xlsx`). Expected: upload, list, view and display round-trip the exact name; `Content-Disposition` uses RFC 5987 `filename*=`; nothing lands in an audit line. Test: Task 7 `test_unicode_names_round_trip`.
2. **Hostile or broken documents:** password-protected PDF/DOCX/XLSX, truncated files, a ZIP bomb disguised as `.docx`. Expected: a short `{"error": ...}` naming the problem, no hang, no crash of the supervisor. Test: Task 11 `test_broken_and_encrypted_files_fail_cleanly`.
3. **Large spreadsheets:** 100 000 rows, formulas, merged cells. Expected: `read_document` pages by range and caps output; formulas show cached values (`data_only=True`), with the formula text when no value is cached. Test: Task 11 `test_large_sheet_pages_and_caps`.
4. **The model uses absolute virtual paths inside Python** (`pd.read_excel("/workspace/<chat>/q.xlsx")`). Expected: it works, because `/workspace` in the executor resolves to the job's own copy. Test: Task 12 `test_absolute_virtual_paths_work_in_python`.
5. **Retention while a job runs:** a file expires between copy-in and output registration. Expected: the job finishes on its copy, the output registers, the expired input is deleted with its record. Test: Task 15 `test_sweep_during_running_job`.

## Branches and pull requests

One short-lived branch and PR per group: `feat/scaffold` (Tasks 1–2), `feat/api-core` (3–7), `feat/executor` (8–10), `feat/tools` (11–14), `feat/retention-ops` (15–16), `test/contract-load` (17), `release/v0.1.0` (18). Task 18 merges only after vllm-service's 32k context change (integration plan, Task B1).

---

### Task 1: Scaffold and toolchain

**Files:**
- Create: `pyproject.toml`, `uv.lock`, `.pre-commit-config.yaml`, `.gitignore`, `.dockerignore`, `Makefile`, `make/common.mk` (vendored), `scripts/bundle-lib.sh` (vendored), `scripts/bundle_images.sh`, `.github/workflows/ci.yml`, `.github/workflows/claude.yml`, `.github/dependabot.yml`, `workbench/__init__.py`, `workbench/utils/__init__.py`, `workbench/utils/audit_event.py` (vendored), `workbench/utils/audit_runtime.py` (vendored), `CLAUDE.md`, `README.md`
- Test: `tests/__init__.py`, `tests/test_version.py`

**Interfaces:**
- Produces: `workbench.__version__: str` (from `importlib.metadata.version("workbench")`); `make verify`, `make test`; dependency extra `executor`.

- [ ] **Step 1: Vendor the hub files verbatim**

Copy from `nos-tromo/.github` at `2715bedabe594a449e411182393e68da59c791f3`: `configs/make-common/common.mk` → `make/common.mk`, `configs/bundle/bundle-lib.sh` → `scripts/bundle-lib.sh`, `configs/audit/audit_event.py` and `configs/audit/audit_runtime.py` → `workbench/utils/` (e.g. `gh api "repos/nos-tromo/.github/contents/<path>?ref=<sha>" -H "Accept: application/vnd.github.raw"`). Do not edit them.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_version.py
import tomllib
from pathlib import Path

import workbench


def test_version_matches_pyproject() -> None:
    data = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert workbench.__version__ == data["project"]["version"]
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/test_version.py -v`. Expected: FAIL (no `workbench` package / no `pyproject.toml`).

- [ ] **Step 4: Write the scaffold**

- `pyproject.toml`: `name = "workbench"`, `version = "0.0.1"`, `requires-python = ">=3.12,<3.13"`, `license = "Apache-2.0"`; dependencies `fastapi`, `uvicorn`, `pydantic`, `prometheus-client`, `python-multipart`; `[project.optional-dependencies] executor = [...]` (the executor list in Tech Stack); `[dependency-groups] dev = ["pre-commit", "pyrefly==<canonical>", "pytest", "httpx", "ruff==<canonical>", "reportlab"]`. Copy translator's ruff and pyrefly blocks with `target-version = "py312"`. Use setuptools with `include = ["workbench*"]` and `[tool.setuptools.package-data] workbench = ["api/system_prompt.md"]`.
- `Makefile`: `REPO := workbench`, `NETWORKS := workbench-net`, `VOLUMES := workbench-data`, `include make/common.mk`, plus `help`. Tasks 9, 15 and 16 add their targets.
- `.github/workflows/ci.yml`: translator's `ci` job calling `python-app-ci.yml@2715bedabe594a449e411182393e68da59c791f3 # v3.22`, with `python-versions: '["3.12"]'`, `uv-sync-args: '--locked --group dev --extra executor'`, `docker-build: false`. Task 9 sets it to `true`.
- `dependabot.yml`: uv, docker (`/docker`) and github-actions, with translator's cooldown rules. The Python minor-bump ignore names `3.12`.
- `CLAUDE.md`: the data-confidentiality and local-path rules from open-webui-service's CLAUDE.md, a one-paragraph orientation and a link to the spec. Task 18 completes it.
- **No `release-tag.yml` yet.** It arrives in Task 18, so no tag is minted before the release.

- [ ] **Step 5: Verify**

Run: `uv sync --group dev --extra executor && make verify && make test`. Expected: pre-commit passes; `1 passed`.

- [ ] **Step 6: Commit** — `git commit -m "chore: scaffold workbench (uv, ruff, pyrefly, common.mk, CI, vendored audit library)"`

### Task 2: Settings

**Files:**
- Create: `workbench/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `class ConfigError(ValueError)`; `@dataclass(frozen=True) class Settings` with the fields `data_dir: Path` (`WORKBENCH_DATA_DIR`, default `/data`), `open_webui_key: str | None`, `retention_days: int`, `retention_interval_s: int`, `executor_slots: int`, `queue_max: int`, `queue_wait_s: int`, `jobs_per_user: int`, `job_wall_s: int`, `job_memory_mb: int`, `result_max_kb: int`, `upload_max_mb: int`, `workspace_max_mb: int`, `executor_socket: Path` (`WORKBENCH_EXECUTOR_SOCKET`, default `/run/workbench/executor.sock`), `slot_uid_base: int` (`WORKBENCH_SLOT_UID_BASE`, default `20001`), and `audit_output: str | None` (`AUDIT_OUTPUT`); `def load_settings(env: Mapping[str, str], *, role: Literal["api", "executor"]) -> Settings`.

- [ ] **Step 1: Write the failing tests**

```python
def test_defaults_match_the_spec() -> None:
    s = load_settings({"OPEN_WEBUI_WORKBENCH_KEY": "k" * 32}, role="api")
    assert (s.retention_days, s.executor_slots, s.queue_max, s.queue_wait_s) == (7, 4, 16, 60)
    assert (s.jobs_per_user, s.job_wall_s, s.job_memory_mb) == (1, 120, 1536)
    assert (s.result_max_kb, s.upload_max_mb, s.workspace_max_mb) == (16, 50, 500)

def test_api_requires_a_long_key() -> None:  # missing, and 31 characters
    with pytest.raises(ConfigError): load_settings({}, role="api")
    with pytest.raises(ConfigError): load_settings({"OPEN_WEBUI_WORKBENCH_KEY": "k" * 31}, role="api")

def test_executor_needs_no_key() -> None:
    assert load_settings({}, role="executor").open_webui_key is None

@pytest.mark.parametrize("name", ["WORKBENCH_EXECUTOR_SLOTS", "WORKBENCH_RETENTION_DAYS", "WORKBENCH_JOB_WALL_S"])
@pytest.mark.parametrize("value", ["0", "-1", "abc", ""])
def test_non_positive_or_non_numeric_values_are_refused(name: str, value: str) -> None: ...

def test_slots_are_capped_at_the_uid_pool() -> None:  # WORKBENCH_EXECUTOR_SLOTS=17 -> ConfigError (pool has 16)
```

- [ ] **Step 2: Run them to verify they fail** — `uv run pytest tests/test_config.py -v`: FAIL (import error).
- [ ] **Step 3: Implement `load_settings`** in `workbench/config.py`. Each `WORKBENCH_*` variable is an `int > 0`; slots is ≤ 16; an empty value means the default.
- [ ] **Step 4: Run them to verify they pass** — Expected: all PASS.
- [ ] **Step 5: Commit** — `git commit -m "feat(config): settings from the environment with the spec's defaults"`

### Task 3: Principals and virtual paths

**Files:**
- Create: `workbench/models.py`, `workbench/paths.py`
- Test: `tests/test_paths.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Principal: caller: str; user_id: str; chat_id: str`
  - `class PathError(ValueError)`
  - `VIRTUAL_ROOT = "/workspace"` and `DEFAULT_CHAT = "default"`
  - `def check_id(value: str, *, what: str) -> str`, which accepts `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$` and refuses `..`
  - `def check_filename(name: str) -> str`: basename only, 1–200 characters, no NUL or `/`, not `.` or `..`
  - `class Workspaces` with:
    - `__init__(self, data_dir: Path)`
    - `user_root(self, p: Principal) -> Path`, which is `data_dir/"workspaces"/p.caller/p.user_id`
    - `chat_dir(self, p: Principal) -> Path`, created with mode 0700
    - `resolve(self, p: Principal, virtual: str) -> Path`
    - `to_virtual(self, p: Principal, real: Path) -> str`

- [ ] **Step 1: Write the failing tests**

```python
def test_virtual_path_maps_into_the_callers_own_root(tmp_path) -> None:
    ws = Workspaces(tmp_path); p = Principal("open-webui", "user-a", "chat-1")
    assert ws.resolve(p, "/workspace/chat-1/q.xlsx") == tmp_path / "workspaces/open-webui/user-a/chat-1/q.xlsx"

@pytest.mark.parametrize("bad", ["/etc/passwd", "/workspace/../x", "/workspace/chat-1/../../user-b/x",
                                 "workspace/chat-1/x", "/workspace/chat-1/a\x00b", "/workspace//chat-1/../.."])
def test_paths_outside_the_root_are_refused(tmp_path, bad: str) -> None:  # raises PathError

def test_symlinks_are_refused_even_inside_the_root(tmp_path) -> None:  # a symlinked chat dir or file -> PathError

def test_two_users_never_share_a_path(tmp_path) -> None:  # same virtual path, different user_id -> different real paths

@pytest.mark.parametrize("bad", ["", "..", "a/b", "../x", "x" * 129, "-x"])
def test_bad_ids_are_refused(bad: str) -> None:  # check_id raises PathError

def test_round_trip_to_virtual(tmp_path) -> None:  # to_virtual(resolve(v)) == v
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement it**
  - `resolve` does `posixpath.normpath`, requires the `VIRTUAL_ROOT` prefix, and checks every segment.
  - It walks the existing components with `lstat`, refusing any symlink.
  - It asserts the result is relative to `user_root`.
  - The first segment after `/workspace` is a chat id (`check_id`); the rest are file names (`check_filename`).
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(paths): virtual /workspace paths confined to the caller's user root"`

### Task 4: Metadata store and file operations

**Files:**
- Create: `workbench/api/__init__.py`, `workbench/api/store.py`
- Modify: `workbench/models.py` (add `FileRecord`, `DirEntry`, `JobRecord`)
- Test: `tests/api/test_store.py`

**Interfaces:**
- Consumes: `Principal`, `Workspaces`, `check_filename` (Task 3).
- Produces:
  - Models:
    - `FileRecord(id: str, caller: str, user_id: str, chat_id: str, name: str, size: int, mime: str, kind: Literal["upload","output"], created_at: float, job_id: str | None)`, with `virtual_path -> str`
    - `DirEntry(name: str, type: Literal["file","directory"], size: int, modified: float)`
    - `JobRecord(id: str, caller: str, user_id: str, chat_id: str, tool: str, created_at: float, status: str)`
  - Errors: `StoreError`, `NotFound`, `TooLarge`, `QuotaExceeded`, `StorageFull`, `Conflict`
  - `class Store(db_path: Path, workspaces: Workspaces, clock: Callable[[], float] = time.time)` with:
    - `add_upload(p, directory: str, filename: str, src: BinaryIO, *, max_bytes: int, quota_bytes: int) -> FileRecord`
    - `register_output(p, staged: Path, name: str, job_id: str) -> FileRecord`
    - `list_dir(p, directory: str) -> list[DirEntry]`
    - `get(p, path: str) -> FileRecord`
    - `files_in_chat(p) -> list[FileRecord]`
    - `delete(p, path: str) -> FileRecord`
    - `mkdir(p, path: str) -> str`
    - `move(p, source: str, destination: str) -> FileRecord`
    - `usage(p) -> int`
    - `due(cutoff: float, limit: int = 200) -> list[FileRecord]`
    - `remove(record: FileRecord) -> None`
    - `add_job(p, tool: str) -> JobRecord`
    - `finish_job(job_id: str, status: str) -> None`
    - `get_job(p, job_id: str) -> JobRecord`
    - `due_jobs(cutoff: float) -> list[str]`
    - `forget_jobs(ids: Sequence[str]) -> None`

- [ ] **Step 1: Write the failing tests**

```python
def test_upload_streams_to_disk_and_records_metadata(store, alice) -> None:
    rec = store.add_upload(alice, "/workspace/chat-1", "q.xlsx", io.BytesIO(b"x" * 10), max_bytes=100, quota_bytes=1000)
    assert (rec.kind, rec.size, rec.virtual_path) == ("upload", 10, "/workspace/chat-1/q.xlsx")

def test_name_clash_gets_a_suffix(store, alice) -> None:  # second "q.xlsx" -> "q (1).xlsx", third -> "q (2).xlsx"
def test_upload_over_the_limit_leaves_nothing(store, alice) -> None:  # TooLarge, no file, no row
def test_quota_counts_all_chats_of_the_user(store, alice) -> None:  # QuotaExceeded across chat-1 + chat-2
def test_disk_full_maps_to_storage_full(store, alice, monkeypatch) -> None:  # OSError(ENOSPC) -> StorageFull, partial file removed
def test_other_users_files_are_not_found(store, alice, bob) -> None:  # get/delete/list by bob -> NotFound / empty
def test_delete_removes_file_and_row(store, alice) -> None:
def test_move_updates_the_path(store, alice) -> None:  # rename within the user root; Conflict when target exists
def test_due_returns_files_older_than_the_cutoff(store, alice, clock) -> None:
def test_get_job_is_scoped_to_caller_and_user(store, alice, bob) -> None:  # bob's lookup of alice's job -> NotFound
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement `Store`**
  - Storage: stdlib `sqlite3` in WAL mode, `PRAGMA secure_delete=ON`, with two tables, `files` and `jobs`, whose columns are the model fields.
  - Uploads: a file id is `uuid4().hex`. An upload is written to `<name>.part`, then renamed with `os.replace`.
  - Modes: files 0600, directories 0700, the DB 0600.
  - Types: `mime` comes from `mimetypes.guess_type`, with fallback `application/octet-stream`.
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(store): workspace files and job records in SQLite, with limits and quota"`

### Task 5: Audit glue

**Files:**
- Create: `workbench/api/audit.py`
- Test: `tests/api/test_audit.py`

**Interfaces:**
- Consumes: `AuditEmitter`, `Actor`, `Target`, `Purpose`, `verify_chain` (vendored), and `open_audit_stream` (vendored).
- Produces:
  - `SERVICE = "workbench"`
  - `OPERATIONS: dict[str, tuple[str, str]]`, mapping each event key to `(action, operation)`:
    - `"upload": ("collect", "file.upload")`
    - `"view": ("disclose", "file.view")`
    - `"read": ("disclose", "file.read")`
    - `"display": ("disclose", "file.display")`
    - `"delete": ("delete", "file.delete")`
    - `"read_document": ("query", "tool.read_document")`
    - `"search_files": ("query", "tool.search_files")`
    - `"run_python": ("combine", "tool.run_python")`
    - `"inspect_template": ("query", "tool.inspect_template")`
    - `"fill_template": ("modify", "tool.fill_template")`
    - `"write_table": ("collect", "tool.write_table")`
    - `"write_document": ("collect", "tool.write_document")`
  - `class AuditLog` with:
    - `__init__(self, emitter: AuditEmitter)`
    - `@classmethod open(cls, settings: Settings, version: str) -> AuditLog`
    - `start(self, config: Mapping[str, object]) -> None`, which writes the chain start plus `admin` / `config.audit`
    - `record(self, key: str, p: Principal, *, file_ids: Sequence[str] = (), outcome: str = "ok", criteria: Mapping[str, object] | None = None, purpose: Purpose | None = None) -> str`, returning the event id
    - `record_system(self, action: str, operation: str, *, file_ids: Sequence[str] = (), criteria: Mapping[str, object] | None = None, outcome: str = "ok") -> str`
    - `close(self) -> None`
  - Event content:
    - `actor = Actor(user=p.user_id, via="service")`
    - `target = Target(kind="file", ids=file_ids, count=len(file_ids), container=f"chat:{p.chat_id}")`
    - `criteria` always includes `{"caller": p.caller}`
  - Failure: an `AuditWriteError` propagates.
  - `parent_event_id` is not set in version 1. Open WebUI's tool calls carry no message id (verified), so the events of one agent turn are tied together only by `container` and their order in the chain.

- [ ] **Step 1: Write the failing tests**

```python
def test_record_writes_a_verifiable_chain(stream) -> None:
    log = AuditLog(AuditEmitter("workbench", "0.0.1", stream=stream, heartbeat_interval_s=0)); log.start({"retention_days": 7})
    log.record("upload", alice, file_ids=["f1"])
    lines = [json.loads(x) for x in stream.getvalue().splitlines()]
    assert verify_chain(lines).ok and lines[-1]["action"] == "collect" and lines[-1]["actor"]["user"] == "user-a"
    assert lines[-1]["target"] == {"kind": "file", "ids": ["f1"], "count": 1, "container": "chat:chat-1"}

def test_config_event_carries_no_secret(stream) -> None:  # the key value never appears in any line
def test_every_tool_key_maps_to_an_audit_action() -> None:  # set(OPERATIONS) == expected keys; actions within ACTIONS
def test_write_failure_propagates(failing_stream) -> None:  # AuditWriteError raised
```

(Use whatever `verify_chain` returns per the vendored module's docstring; adapt the `.ok` access to it.)

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement `AuditLog`**, mirroring translator's `translator/audit.py` for opening the stream.
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(audit): the api's audit chain, by file id and never by name"`

### Task 6: API skeleton, authentication and the handshake

**Files:**
- Create: `workbench/api/main.py`, `workbench/api/auth.py`, `workbench/api/contract.py`, `workbench/api/openapi.py`, `workbench/api/system_prompt.md`
- Test: `tests/api/conftest.py` (with the `client` fixture), `tests/api/test_contract.py`

**Interfaces:**
- Consumes: `Settings`, `Store`, `AuditLog`, `Workspaces`, `Principal`.
- Produces:
  - `create_app(settings: Settings, *, store: Store, audit: AuditLog, jobs: "JobQueue | None" = None) -> FastAPI`, created with `openapi_url=None, docs_url=None, redoc_url=None`
  - `app_factory() -> FastAPI`, used as `uvicorn --factory workbench.api.main:app_factory`
  - `@dataclass(frozen=True) class Caller: name: str; key: str; tools: frozenset[str]`
  - `CallerRegistry.from_settings(settings) -> CallerRegistry`, with one caller `"open-webui"` and all tools
  - The `require_caller` dependency: `Authorization: Bearer <key>`, compared with `hmac.compare_digest`. Anything else gets 401 `{"detail": "missing or invalid key"}`.
  - The `require_principal` dependency: `X-User-Id` is required and goes through `check_id`; 400 `{"detail": "missing or invalid user"}`. `X-Session-Id` goes through `check_id` and defaults to `"default"`.
  - `TOOL_TAG = "tool"`. `tool_openapi(app) -> dict` is the OpenAPI document of routes tagged `TOOL_TAG` only, cached after the first call.
  - `def system_prompt(settings) -> str`, loaded from package data with `{retention_days}` filled in.
  - `GET /healthz`: unauthenticated, not in the schema, returns `{"ok": true}`. The compose healthcheck uses it.

- [ ] **Step 1: Write the failing tests** (FastAPI `TestClient`; `KEY = "k" * 32`; `H = {"Authorization": f"Bearer {KEY}", "X-User-Id": "user-a", "X-Session-Id": "chat-1"}`)

```python
def test_policies_is_not_found_so_open_webui_sees_a_plain_terminal(client) -> None:
    assert client.get("/api/v1/policies", headers=H).status_code == 404
def test_config_announces_a_system_prompt(client) -> None:
    assert client.get("/api/config", headers=H).json()["features"]["system"] is True
def test_system_prompt_names_the_retention_period(client) -> None:  # "7" in prompt; mentions docint
def test_openapi_needs_only_the_key(client) -> None:  # 200 with {"Authorization": ...} alone; 401 without
def test_openapi_lists_only_tools(client) -> None:  # every operation has tag "tool" and an operationId; no /files/*, /api/*, /system, /skills, /metrics
def test_wrong_or_missing_key_is_refused(client) -> None:  # 401 on /api/config, /files/cwd
def test_user_header_is_required_and_checked(client) -> None:  # missing -> 400; "../x" -> 400
def test_cwd_is_the_chats_folder(client) -> None:  # {"cwd": "/workspace/chat-1", "home": "/workspace"}
def test_requests_without_a_session_use_the_default_folder(client) -> None:  # cwd /workspace/default
def test_local_chat_ids_are_accepted(client) -> None:  # X-Session-Id "local:abc123" -> 200
def test_post_cwd_stays_inside_the_root(client) -> None:  # /workspace/chat-2 ok; /etc -> 400
def test_agents_md_is_absent(client) -> None:  # GET /files/read?path=/workspace/AGENTS.md -> 404
def test_skills_answer_empty(client) -> None:  # GET /skills -> []; GET /skills/read?name=x -> 404
def test_unknown_paths_are_not_found(client) -> None:  # GET /api/terminals/user/output, /files/archive, /system/info -> 404
def test_healthz_needs_no_key(client) -> None:  # GET /healthz without headers -> 200 {"ok": true}
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement it**
  - Handshake routes are `include_in_schema=False`. `/openapi.json` is a plain route returning `tool_openapi(app)` behind `require_caller`.
  - The lifespan opens the store and starts audit with `{"retention_days", "executor_slots", "queue_max", "job_wall_s", "upload_max_mb", "workspace_max_mb"}`.
  - The system prompt (`workbench/api/system_prompt.md`, English) carries the behaviour in spec §8, verbatim in intent:
    - plan first and confirm
    - cite sources
    - never invent
    - say when a PDF needs OCR
    - point to docint for projects
    - files are deleted after `{retention_days}` days
    - what a template needs
    - in Python, files are in the current folder and also at their `/workspace/...` paths
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(api): terminal handshake, bearer key, user and chat headers, tool-only OpenAPI"`

### Task 7: File browser, `list_files` and `display_file`

**Files:**
- Create: `workbench/api/files.py`, `workbench/api/tools.py` (with `list_files` only for now)
- Test: `tests/api/test_files.py`

**Interfaces:**
- Consumes: `Store`, `AuditLog.record`, `require_principal`, `TOOL_TAG`.
- Produces: file-browser routes, all `include_in_schema=False`:
  - `GET /files/list?directory=` returns `{"dir", "writable": true, "entries": [{name, type, size, modified}]}`
  - `POST /files/upload?directory=` (multipart field `file`) returns `{"path", "size"}`
  - `GET /files/view?path=[&preview=…]` returns raw bytes with the record's `Content-Type` and `Content-Disposition: inline; filename*=UTF-8''…`, ignoring `preview`
  - `GET /files/read?path=` returns `{"content"}` for `text/*`, `.md` and `.csv` up to `result_max_kb`; anything else gets 415
  - `DELETE /files/delete?path=`
  - `POST /files/mkdir {"path"}`
  - `POST /files/move {"source","destination"}`

  Tool routes, tag `tool`:
  - `POST /tools/list_files` (`operationId` `list_files`, empty body) returns `{"files": [{path, size, modified, kind}]}`
  - `GET /files/display?path=` (`operationId` `display_file`) returns `{"path", "exists", "name", "mime_type"}`

  Store errors map to statuses: `NotFound` 404, `TooLarge` 413, `QuotaExceeded` 507, `StorageFull` 507, `Conflict` 409, `PathError` 400. Each answers `{"detail": <short text without file content>}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_upload_then_list_view_display(client, audit_lines) -> None:
    r = client.post("/files/upload", params={"directory": "/workspace/chat-1"}, headers=H, files={"file": ("q.csv", b"a,b\n1,2\n")})
    assert r.json() == {"path": "/workspace/chat-1/q.csv", "size": 8}
    assert [e["name"] for e in client.get("/files/list", params={"directory": "/workspace/chat-1"}, headers=H).json()["entries"]] == ["q.csv"]
    assert client.get("/files/view", params={"path": "/workspace/chat-1/q.csv"}, headers=H).content == b"a,b\n1,2\n"
    assert client.get("/files/display", params={"path": "/workspace/chat-1/q.csv"}, headers=H).json()["exists"] is True
    assert [line["operation"] for line in audit_lines()][-3:] == ["file.upload", "file.view", "file.display"]

def test_no_audit_line_contains_a_file_name(client, audit_lines) -> None:  # upload/view/display/delete "secret-name.xlsx"; name absent from all lines
def test_unicode_names_round_trip(client) -> None:  # "Übersicht März.xlsx" and "übersicht märz.xlsx": both kept, exact names listed; filename* header
def test_other_user_cannot_list_view_or_delete(client) -> None:  # X-User-Id user-b -> empty list / 404 / 404
def test_delete_is_recorded_before_the_file_goes(client, broken_audit) -> None:  # audit raises -> 500 and the file still exists
def test_list_files_tool_is_in_the_schema(client) -> None:  # operationIds include "list_files", "display_file"
def test_upload_limits(client) -> None:  # > upload_max -> 413; over quota -> 507
def test_traversal_in_query_parameters_is_refused(client) -> None:  # path=/workspace/chat-1/../../x -> 400
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement the routes**
  - Uploads stream via `UploadFile.file` in `run_in_threadpool`.
  - Audit keys:
    - upload: `"upload"`
    - view: `"view"`
    - read: `"read"`
    - display: `"display"`
    - delete: `"delete"`, recorded **before** `Store.delete`
    - list, mkdir and move: not recorded (spec §7)
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(api): file browser, list_files and display_file, recorded by file id"`

### Task 8: Job protocol, job runner and sandbox spawn

**Files:**
- Create: `workbench/protocol.py`, `workbench/executor/__init__.py`, `workbench/executor/sandbox.py`, `workbench/executor/job.py`, `workbench/executor/runners/__init__.py`
- Test: `tests/executor/test_protocol.py`, `tests/executor/test_job.py`, `tests/executor/test_sandbox.py`

**Interfaces:**
- Produces:
  - In `workbench/protocol.py`, all pydantic models:
    - `Limits(wall_s: int, cpu_s: int, memory_mb: int, file_mb: int, nproc: int = 64, nofile: int = 256)`
    - `JobRequest(job_id: str, tool: str, args: dict[str, Any], chat_id: str, input_files: list[str], limits: Limits, max_chars: int)`. `input_files` holds paths relative to the chat folder; the executor copies only these. `max_chars` is `result_max_kb * 1024`.
    - `OutputFile(name: str, staged_path: str, size: int)`
    - `JobResult(job_id: str, status: Literal["ok","error","timeout","killed","busy"], result: dict[str, Any], outputs: list[OutputFile], message: str = "")`
    - `encode(model: BaseModel) -> bytes`, producing one JSON line with `\n`
    - `decode_request(line: bytes) -> JobRequest` and `decode_result(line: bytes) -> JobResult`, each raising `ValueError` above `MAX_LINE = 1_048_576`
  - `RUNNERS: dict[str, Callable[[dict[str, Any], JobContext], dict[str, Any]]]` in `workbench/executor/runners/__init__.py`, holding only `"echo"` for now; tool tasks register theirs
  - `@dataclass(frozen=True) class JobContext: work_dir: Path; out_dir: Path; chat_id: str; max_chars: int`
  - The job entry is `python -I -m workbench.executor.job <spec.json>`. It reads `{"tool", "args", "chat_id", "max_chars"}`, uses `cwd` as `work_dir`, runs `RUNNERS[tool]`, and writes `result.json` next to the spec. A raised exception becomes `{"error": "<Type>: <message>"}`.
  - In `workbench/executor/sandbox.py`:
    - `@dataclass(frozen=True) class Slot: index: int; uid: int; gid: int`
    - `def preexec(slot: Slot, limits: Limits) -> Callable[[], None]`, which does `os.setsid`, `setgroups([])`, `setgid`, `setuid` and sets `RLIMIT_AS`, `RLIMIT_CPU`, `RLIMIT_FSIZE`, `RLIMIT_NPROC`, `RLIMIT_NOFILE` and `RLIMIT_CORE=0`
    - `async def run_in_slot(argv: list[str], cwd: Path, slot: Slot | None, limits: Limits) -> tuple[Literal["ok","error","timeout","killed"], int]`. With `slot=None` no uid switch happens (tests and dev); the wall timeout kills the process group.
    - `CLEAN_ENV`: `{"PATH", "HOME": cwd, "LANG": "C.UTF-8", "MPLBACKEND": "Agg", "PYTHONDONTWRITEBYTECODE": "1", "OPENBLAS_NUM_THREADS": "1", "OMP_NUM_THREADS": "1"}`; nothing else is inherited

- [ ] **Step 1: Write the failing tests**

```python
def test_protocol_round_trip() -> None:  # decode_request(encode(req)) == req; same for JobResult
def test_oversized_line_is_refused() -> None:  # ValueError
def test_job_runs_a_registered_runner(tmp_path) -> None:  # spec tool "echo" args {"x": 1} -> result.json {"x": 1}
def test_job_turns_exceptions_into_errors(tmp_path) -> None:  # unknown tool -> {"error": "KeyError: ..."}
async def test_wall_timeout_kills_the_process_group(tmp_path) -> None:  # child sleeps 10 s, wall 1 s -> ("timeout", _), grandchild gone too
async def test_memory_limit_kills_a_hog(tmp_path) -> None:  # bytearray(2 GB) with memory_mb=256 -> "killed" or "error" with MemoryError (Linux only; skip on darwin)
def test_child_environment_is_clean(tmp_path, monkeypatch) -> None:  # WORKBENCH_* / OPEN_WEBUI_* set in parent are absent in child
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement** `protocol.py`, `job.py`, `sandbox.py` and the `echo` runner. `run_in_slot` uses `asyncio.create_subprocess_exec` with `preexec_fn` and `start_new_session=False` (setsid happens in `preexec`), then `os.killpg` on timeout. A negative return code `-SIGKILL` or `-SIGXCPU` maps to `"killed"`.
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(executor): job protocol, job entry point and the slot sandbox"`

### Task 9: Executor supervisor, images, compose and isolation tests

**Files:**
- Create:
  - `workbench/executor/supervisor.py`, `workbench/executor/prepare_volume.py`
  - `docker/Dockerfile.api`, `docker/Dockerfile.executor`
  - `docker/compose.yaml`, `docker/compose.override.yaml`, `docker/compose.test.yaml`
  - `tests/isolation/conftest.py`, `tests/isolation/test_isolation.py`
- Modify: `Makefile` (adds `test-isolation`), `.github/workflows/ci.yml` (`docker-build: true`, plus an `isolation` job running `make test-isolation`), `pyproject.toml` (registers the `isolation` marker; `addopts = "-m 'not isolation and not contract and not load'"`)
- Test: `tests/executor/test_supervisor.py` (unit, `slot=None`)

**Interfaces:**
- Consumes: Task 8 everything; `Settings` (role `executor`).
- Produces:
  - `class Supervisor` with:
    - `__init__(self, settings: Settings, *, use_slots: bool = True)`
    - `async def serve(self) -> None`, which listens on `settings.executor_socket` (mode 0660, group 10001)
    - `async def run_job(self, req: JobRequest) -> JobResult`
  - Volume layout, created by `prepare_volume` as root:
    - `/data/workspaces` owned 10001, mode 0700
    - `/data/staging` owned 10001, mode 0700
    - `/data/jobs` owned root, mode 0711
    - `/data/workbench.db` owned 10001, mode 0600
  - Per-job layout:
    - `/data/jobs/<job-id>/` owned by the slot uid, mode 0700
    - `…/workspace/<chat-id>/` owned root, mode 0755. It holds copies of `input_files`, each root-owned, mode 0444.
    - `…/workspace/<chat-id>/out/` owned by the slot uid, mode 0700
    - The job's `cwd` is `…/workspace/<chat-id>/`
  - In the executor image, `/workspace` is a symlink to `/proc/self/cwd/..`. So `/workspace/<chat>/x` resolves to the job's own copy.
  - Outputs: after the job, files in `out/` are validated (regular files only, at most 20, total ≤ `upload_max_mb`). They are copied to `/data/staging/<job-id>/<name>`, owned 10001, mode 0600, and listed in `JobResult.outputs`. The job directory is removed.
  - When every slot is busy, the reply is `JobResult(status="busy")`.

- [ ] **Step 1: Write the failing unit tests**

```python
async def test_run_job_copies_inputs_and_stages_outputs(tmp_path) -> None:  # use_slots=False; runner writes out/r.csv -> outputs[0].name == "r.csv", staged file exists, job dir removed
async def test_outputs_that_are_symlinks_or_too_many_are_dropped(tmp_path) -> None:  # symlink in out/ ignored; 21 files -> status error "too many outputs"
async def test_only_requested_inputs_are_copied(tmp_path) -> None:  # input_files ["a.csv"]; b.csv in the chat folder is not in the job dir
async def test_busy_when_all_slots_run(tmp_path) -> None:  # slots=1, two concurrent long jobs -> one "busy"
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement the supervisor, `prepare_volume`, both Dockerfiles and compose**
  - **Dockerfiles**, following translator's builder/runtime pattern:
    - The api image runs `uv sync --no-dev`, as user 10001.
    - The executor image runs `uv sync --no-dev --extra executor`, creates groups and users 20001–20016 with nologin, does `RUN ln -s /proc/self/cwd/.. /workspace`, and runs as `USER 0:0`.
  - **compose services** (`name: workbench`), all on the federation log driver (`local`, 50m × 5):
    - `volume-permissions`: a one-shot on the executor image running `python -m workbench.executor.prepare_volume`, as `user: "0:0"`, with `network_mode: none`.
    - `api`: on `workbench-net` with alias `workbench-api`; mounts `workbench-data:/data` and `workbench-run:/run/workbench`; `AUDIT_OUTPUT: container`. The healthcheck is Python `urllib` against `/healthz`, which is unauthenticated, not in the schema, and returns `{"ok": true}`.
    - `executor`: `network_mode: none`, `user: "0:0"`, `cap_drop: [ALL]`, `cap_add: [SETUID, SETGID, CHOWN, FOWNER, DAC_OVERRIDE, KILL]`, `read_only`, `pids_limit: 256`, `mem_limit: ${WORKBENCH_EXECUTOR_MEM_LIMIT:-6656m}`. That default is 4 × 1536 + 512 MB. It mounts the same two volumes. At start the supervisor refuses to run when cgroup `memory.max` is below slots × job memory.
  - **Volumes and networks:** `workbench-data` external; `workbench-run` project-local; `workbench-net` external.
  - **`compose.test.yaml`:** adds a test key and publishes the api on `127.0.0.1:18100` for the isolation, contract and load tests.
- [ ] **Step 4: Write the isolation tests** (`@pytest.mark.isolation`, against the stack from `compose.test.yaml`)
  - **Order:** Task 9 lands the harness with `test_executor_is_isolated_by_configuration`, which runs green now. The Python-based tests below call `POST /tools/run_python` and so need Task 12's runner; they land here marked `xfail(strict=True)`, and Task 12 removes the marks.
  - **`test_executor_is_isolated_by_configuration`:** `docker inspect` of the running executor shows `NetworkMode` `none`, `CapAdd` exactly the six listed, `ReadonlyRootfs` true, `PidsLimit` 256, and a memory limit ≥ slots × job memory.
  - **The Python-based tests** each submit code through `run_python` and check:
    - `test_no_network`: `socket.create_connection(("1.1.1.1", 53), 2)` raises `OSError`.
    - `test_other_workspaces_are_unreadable`: listing `/data/workspaces` raises `PermissionError`.
    - `test_other_jobs_are_unreadable`: two concurrent jobs; each tries to list the other's `/data/jobs/<id>` and gets `PermissionError`.
    - `test_inputs_are_read_only`: writing `./in.csv` raises `PermissionError`; writing `./out/x` succeeds.
    - `test_no_secrets_in_environment`: `os.environ` holds no `OPEN_WEBUI_WORKBENCH_KEY`.
    - `test_fork_bomb_is_contained`: the job ends as `killed`, `error` or `timeout`, and the next job runs normally.
    - `test_memory_hog_is_killed_with_a_clear_message`.
    - `test_wall_time_limit`.
- [ ] **Step 5: Run everything**
  - Run `make test`: unit tests PASS.
  - Run `make test-isolation`. It does `docker compose -f docker/compose.yaml -f docker/compose.test.yaml up -d --build`, then `pytest -m isolation`, then `down`. Expected: `test_executor_is_isolated_by_configuration` PASS, and the Python-based ones XFAIL.
- [ ] **Step 6: Commit** — `git commit -m "feat(executor): supervisor, images, compose and isolation tests"`

### Task 10: Job queue, executor client and metrics

**Files:**
- Create: `workbench/api/jobs.py`, `workbench/api/metrics.py`
- Modify: `workbench/api/main.py` (wires in `JobQueue` and `/metrics`)
- Test: `tests/api/test_jobs.py`, `tests/api/test_metrics.py`

**Interfaces:**
- Consumes: `protocol`, `Store.add_job/finish_job/register_output/files_in_chat`, `Settings`.
- Produces:
  - `class Busy(Exception)`, with attribute `reason: Literal["queue_full","wait_timeout","executor_busy"]`.
  - `class ExecutorClient(socket_path: Path)` with `async run(self, req: JobRequest, timeout_s: float) -> JobResult`.
  - `class JobQueue` with:
    - `__init__(self, settings: Settings, client: ExecutorClient, store: Store, metrics: Metrics)`
    - `async submit(self, p: Principal, tool: str, args: dict[str, Any], input_files: Sequence[str]) -> tuple[JobRecord, JobResult, list[FileRecord]]`

    `submit` does, in order:
    - A global slot semaphore with `executor_slots`.
    - A per-user semaphore with `jobs_per_user`.
    - Waiting, counted against `queue_max`. Overflow raises `Busy("queue_full")`.
    - At most `queue_wait_s` of waiting, else `Busy("wait_timeout")`.
    - Limits come from settings: `wall_s=job_wall_s`, `cpu_s=job_wall_s`, `memory_mb=job_memory_mb`, `file_mb=upload_max_mb`.
    - Outputs from the result are registered.
    - A `JobResult` with `status == "busy"` raises `Busy("executor_busy")`.
  - `class Metrics` (prometheus-client, its own `CollectorRegistry`):
    - `queue_length` (Gauge)
    - `queue_wait_seconds` (Histogram)
    - `job_duration_seconds` (Histogram, label `tool`)
    - `job_refusals_total` (Counter, label `reason`)
    - `disk_used_bytes` and `disk_capacity_bytes` (Gauges, from `shutil.disk_usage(data_dir)`)
    - `files_total` (Gauge)
  - `GET /metrics`: `include_in_schema=False`, no key.
  - A tool route maps `Busy` to HTTP 503 `{"detail": "busy, retry later"}`.

- [ ] **Step 1: Write the failing tests** (a `FakeClient` that sleeps and returns `ok`)

```python
async def test_never_more_than_slots_run_at_once() -> None:  # 10 users, slots=4 -> max observed concurrency == 4
async def test_full_queue_refuses_at_once() -> None:  # slots=1, queue_max=1, 3 submits -> one Busy("queue_full") immediately
async def test_wait_timeout() -> None:  # queue_wait_s=1, long job ahead -> Busy("wait_timeout")
async def test_one_job_per_user_others_proceed() -> None:  # user-a's 2nd job waits while user-b's runs
async def test_outputs_are_registered_as_files() -> None:  # result.outputs -> FileRecord kind "output", job_id set
def test_metrics_expose_counts_only(client) -> None:  # 200, contains "workbench_queue_length", no "user-a"
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement `jobs.py` and `metrics.py`.** Update the disk gauges and `files_total` on each sweep (Task 15) and on start.
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(api): bounded job queue with per-user fairness, executor client, metrics"`

### Task 11: `read_document` and `search_files`

**Files:**
- Create:
  - `workbench/executor/readers/__init__.py`, plus `pdf.py`, `docx.py`, `sheet.py`, `pptx.py`, `odf.py`, `email.py`, `html.py`, `text.py`
  - `workbench/executor/runners/documents.py`
- Modify: `workbench/executor/runners/__init__.py` (registers both tools), `workbench/api/tools.py` (adds both routes)
- Test: `tests/executor/test_readers.py`, `tests/executor/test_search.py`, `tests/api/test_tools_documents.py`, `tests/fixtures/make_fixtures.py` (builds synthetic files at test time; nothing binary is committed)

**Interfaces:**
- Produces:
  - Shared reader types:
    - `@dataclass class Part: label: str; text: str` (label is e.g. `"page 3"`, `"Sheet1!A1:F200"`, `"slide 2"`)
    - `@dataclass class DocumentText: kind: str; parts: list[Part]; pages_without_text: list[int]; total_parts: int`
    - `class UnsupportedFormat(Exception)` and `class UnreadableFile(Exception)`
  - Each reader has `read(path: Path, *, part: str | None, max_chars: int) -> DocumentText`. Dispatch is by extension in `readers.read`:
    - `.pdf` uses pypdf. A page whose stripped text is empty goes into `pages_without_text`.
    - `.docx` / `.dotx` read paragraphs and tables.
    - `.xlsx` / `.xlsm` use openpyxl with `read_only=True, data_only=True`. Without a cached value the formula text is shown.
    - `.csv` / `.tsv` read via `text`.
    - `.pptx`, `.odt` / `.ods`, `.eml` (stdlib `email`, body plus attachment names), `.html` / `.htm` (bs4), `.txt` / `.md` (charset-normalizer).
    - `.msg` and anything else raise `UnsupportedFormat`.
  - Tool `read_document`: body `{"path": str, "part": str | None}`. It returns `{"path", "kind", "parts": [{"label","text"}], "pages_without_text": [...], "hint"?: "...needs OCR elsewhere...", "truncated": bool, "next_part"?: str}`.
  - Tool `search_files`: body `{"query": str, "regex": bool = False, "max_hits": int = 50}`, with `max_hits` capped at 200. It returns `{"hits": [{"path","location","snippet"}], "truncated": bool}`. Snippets are ≤ 200 chars around the match. A regex runs with a 2 s timeout inside the job.
  - Both API routes resolve paths through `Store.get` (404 if not the caller's). They send the chat's files as `input_files`, record `"read_document"` / `"search_files"` with the file ids, and cap the result at `result_max_kb`.

- [ ] **Step 1: Write the failing tests** (fixtures built with python-docx, openpyxl and reportlab: one PDF page with text and one image-only page; a DOCX with a table; an XLSX with a formula and 100 000 rows; a PPTX; an EML; an HTML file; a Latin-1 TXT)

```python
def test_pdf_reports_pages_without_text() -> None:  # pages_without_text == [2]; hint present; page 2 never returned as empty text
def test_docx_reads_paragraphs_and_tables() -> None:
def test_large_sheet_pages_and_caps() -> None:  # part None -> first range only, truncated True, next_part "Sheet1!A201:..."; formula cell shows cached value or formula
def test_latin1_text_is_decoded() -> None:
def test_msg_is_unsupported_with_a_hint() -> None:  # error mentions ".eml or PDF"
def test_broken_and_encrypted_files_fail_cleanly() -> None:  # encrypted PDF, truncated DOCX, zip-bomb .docx (1 KB -> 2 GB) -> UnreadableFile within 5 s
def test_search_finds_text_across_formats() -> None:  # hits in pdf page 1, xlsx cell, docx paragraph with labels
def test_tool_route_records_query_with_file_ids(client, fake_executor, audit_lines) -> None:
def test_tool_route_refuses_other_users_files(client) -> None:  # 404
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement the readers and runners**
  - The zip-bomb guard: before opening any OOXML or ODF file, sum `ZipInfo.file_size` and refuse above 20 × `upload_max_mb`.
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(tools): read_document and search_files, text PDFs only, with paging and caps"`

### Task 12: `run_python`

**Files:**
- Create: `workbench/executor/runners/python.py`
- Modify: `workbench/executor/runners/__init__.py`, `workbench/api/tools.py`, `tests/isolation/test_isolation.py` (drops the `xfail` marks)
- Test: `tests/executor/test_run_python.py`, `tests/api/test_tools_python.py`

**Interfaces:**
- Produces: tool `run_python`, body `{"code": str}` (≤ 50 000 chars). It returns `{"stdout", "stderr", "files": ["/workspace/<chat>/<name>", ...], "truncated": bool}`.
  - stdout and stderr are each capped at half of `result_max_kb`.
  - Every file written to `out/` becomes an output file (images included), named as written. Inline images are never returned.
  - The runner `exec`s the code in a fresh `__main__`-like namespace, inside `cwd`, with `matplotlib.use("Agg")` applied.
  - A `FileNotFoundError` whose path starts with `/workspace/` and points to another chat gets the hint "only this chat's files are available".
  - The API records `"run_python"` with the ids of every input file and output, plus `criteria={"outputs": n}`.

- [ ] **Step 1: Write the failing tests**

```python
def test_pandas_reads_an_input_and_writes_an_output(tmp_job) -> None:  # read_csv("q.csv"); to_excel("out/r.xlsx") -> files == [".../r.xlsx"]
def test_absolute_virtual_paths_work_in_python(isolation_client) -> None:  # @isolation: pd.read_csv("/workspace/chat-1/q.csv") works in the real executor
def test_plot_is_saved_as_a_file(tmp_job) -> None:  # plt.savefig("out/p.png") -> files contains p.png; stdout has no base64
def test_output_is_capped(tmp_job) -> None:  # print("x" * 100_000) -> truncated True, len(stdout) <= 8 KB
def test_exceptions_return_a_traceback_tail(tmp_job) -> None:  # 1/0 -> stderr ends with "ZeroDivisionError: division by zero"
def test_route_records_combine_with_inputs_and_outputs(client, fake_executor, audit_lines) -> None:
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement the runner and the route**
- [ ] **Step 4: Verify** — Run `make test`, then `make test-isolation`. Expected: both PASS with no xfails left.
- [ ] **Step 5: Commit** — `git commit -m "feat(tools): run_python in the slot sandbox, outputs as workspace files"`

### Task 13: Templates — `inspect_template` and `fill_template`

**Files:**
- Create: `workbench/executor/runners/templates.py`, `workbench/executor/templates/docx_controls.py`, `workbench/executor/templates/docx_jinja.py`, `workbench/executor/templates/xlsx_placeholders.py`, `workbench/executor/templates/sources.py`
- Modify: `workbench/executor/runners/__init__.py`, `workbench/api/tools.py`
- Test: `tests/executor/test_templates.py`, `tests/api/test_tools_templates.py`, `tests/fixtures/make_templates.py`

**Interfaces:**
- Produces:
  - **`Field` type:** `Field(name: str, kind: Literal["content_control","form_field","placeholder","table"], type: Literal["text","rich_text","date","dropdown","checkbox","table"], options: list[str] | None, location: str)`.
  - **`inspect_template`:** body `{"path"}`. Returns `{"path", "template_kind": "content_controls"|"form_fields"|"placeholders"|"mixed"|"none", "fields": [Field...], "hint"?: str}`. When `template_kind` is `"none"`, the hint says what version 1 needs.
  - **`fill_template`:**
    - Body: `{"path", "values": {name: {"value": Any, "source": Source}}, "output_name": str | None, "include_sources": bool = True}`.
    - `Source` takes one of four shapes: `{"kind":"file","path": str, "page"?: int, "sheet"?: str, "cell"?: str}`, `{"kind":"job","job_id": str}`, `{"kind":"user","note"?: str}`, or `{"kind":"assistant"}`.
    - Returns `{"path": <new file>, "filled": int, "unfilled": [names], "warnings": [str]}`.
  - **Rules:**
    - **Field names:** a content control is named by `w:tag@w:val`, falling back to `w:alias@w:val`. A legacy form field is named by `w:ffData/w:name@w:val`.
    - **Value checks:** a drop-down accepts only one of its `w:listItem` display texts, otherwise 422 lists the options. A checkbox accepts only a bool. A date takes an ISO `YYYY-MM-DD` and sets `w:fullDate`.
    - **`.dotx` output:** a `.dotx` becomes `.docx`, with the main part's content type rewritten to `application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml`.
    - **What is kept:** styles, headers and document protection.
    - **DOCX `{{ }}` placeholders:** handled by docxtpl.
    - **XLSX placeholders:** a `{{ field }}` cell is replaced by its value. A `{{ table:<name> }}` cell writes `value` (a list of rows) downwards from that cell, and refuses to overwrite non-empty cells.
    - **Source checks:** the API checks that `file` paths exist in the caller's store and that `job` ids are the caller's (`Store.get_job`). The executor checks that `page`, `sheet` and `cell` exist in the copied file. A failed check means 422 naming the field.
    - **Sources appendix:** a DOCX gets a final table "Sources" (columns field, value, source kind, reference); an XLSX gets a sheet "Sources" with the same columns.
    - **Audit:** record `"inspect_template"` / `"fill_template"` with the ids of the template, the source files and the output.

- [ ] **Step 1: Write the failing tests** (synthetic templates: a DOCX whose content controls are a plain text one `customer`, a date one `report_date`, a drop-down `status` with "open"/"closed", and a checkbox `approved`; a legacy-form-field DOCX; a `.dotx`; a docxtpl DOCX with a loop; an XLSX with `{{ total }}` and `{{ table:rows }}`; a DOCX without fields)

```python
def test_inspect_lists_content_controls_with_types_and_options() -> None:
def test_inspect_lists_legacy_form_fields() -> None:
def test_inspect_none_explains_what_a_template_needs() -> None:
def test_fill_content_controls_keeps_styles_and_writes_sources() -> None:  # read back with python-docx: values present, last table header == ["field","value","source kind","reference"]
def test_dropdown_refuses_values_outside_its_list() -> None:  # 422 lists "open", "closed"
def test_dotx_becomes_docx() -> None:  # [Content_Types].xml main part type is document.main+xml; opens with python-docx
def test_jinja_loop_fills_rows() -> None:
def test_xlsx_table_anchor_writes_rows_and_refuses_overwrite() -> None:
def test_sources_must_exist(client, fake_executor) -> None:  # missing file / foreign job / page 99 of a 2-page PDF -> 422 naming the field
def test_assistant_and_user_sources_are_labelled() -> None:  # appendix kind column shows "assistant" / "user"
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement the template modules** (lxml on the python-docx part XML for SDT and form fields; docxtpl for Jinja)
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(tools): inspect and fill Word content controls, form fields and {{ }} templates, with sources"`

### Task 14: `write_table` and `write_document`

**Files:**
- Create: `workbench/executor/runners/outputs.py`
- Modify: `workbench/executor/runners/__init__.py`, `workbench/api/tools.py`
- Test: `tests/executor/test_outputs.py`, `tests/api/test_tools_outputs.py`

**Interfaces:**
- Produces:
  - **`write_table`:** body `{"name": str, "format": Literal["xlsx","csv"], "columns": list[str], "rows": list[list[str|int|float|bool|None]], "sheet": str = "Sheet1"}`. Rows are capped at 100 000. Returns `{"path", "rows": int}`. XLSX output uses xlsxwriter with a bold header row and frozen panes; CSV output uses UTF-8 with a BOM, so Excel opens umlauts correctly.
  - **`write_document`:** body `{"name": str, "format": Literal["docx","md"], "markdown": str}` (≤ 200 000 chars). Returns `{"path"}`. DOCX output uses markdown-it-py tokens and supports headings 1–4, paragraphs, bold and italic, bullet and numbered lists, tables and code blocks. Other syntax becomes plain text.
  - **Naming:** a missing extension is added; a name clash gets a suffix (via `Store.register_output`).
  - **Audit:** both record `"write_table"` / `"write_document"` with the output id.

- [ ] **Step 1: Write the failing tests**

```python
def test_xlsx_round_trip() -> None:  # openpyxl reads header + rows back; header bold
def test_csv_has_bom_and_umlauts() -> None:
def test_docx_from_markdown_has_headings_lists_and_tables() -> None:  # python-docx: heading styles, list paragraphs, one table
def test_clashing_names_get_a_suffix(client, fake_executor) -> None:
def test_routes_record_collect_with_the_output_id(client, fake_executor, audit_lines) -> None:
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement the runners and routes**
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(tools): write_table and write_document"`

### Task 15: Retention

**Files:**
- Create: `workbench/api/retention.py`
- Modify: `workbench/api/main.py` (starts the sweep in the lifespan), `Makefile` (adds `retention-dry-run`)
- Test: `tests/api/test_retention.py`

**Interfaces:**
- Consumes: `Store.due/remove/due_jobs/forget_jobs`, `AuditLog.record_system`, `Metrics`.
- Produces:
  - `@dataclass class SweepReport: deleted: int; errors: int`
  - `class Sweeper(store: Store, audit: AuditLog, settings: Settings, metrics: Metrics, clock: Callable[[], float] = time.time)`, with:
    - `sweep_once(self) -> SweepReport`
    - `async run_forever(self) -> None`, which runs at start and then every `retention_interval_s`
  - The command `python -m workbench.api.retention dry-run [--days N]` prints counts and age ranges per kind (`upload` / `output`). It never prints names, ids or users.
  - `make retention-dry-run` runs that command in the `api` container through `docker compose exec`.
  - **Rules:**
    - A file is due when `created_at < now - retention_days * 86400`.
    - Batches hold ≤ 200 files and are grouped by `(caller, user_id)`.
    - Each batch first writes `record_system("delete", "retention.sweep", file_ids=..., criteria={"period_days", "cutoff", "caller", "owner": user_id, "count"})`. Only then are its files removed (file plus row).
    - A file whose removal fails is recorded again with `outcome="error"`.
    - An `AuditWriteError` stops the process: log the count to stderr and `sys.exit(1)`. `restart: unless-stopped` brings it back.
    - Job rows older than the period are forgotten without a record, since they hold no personal data.
    - Empty chat folders and staging directories older than 1 h are removed.

- [ ] **Step 1: Write the failing tests**

```python
def test_due_files_are_recorded_then_deleted(sweeper, clock, audit_lines) -> None:
def test_nothing_is_deleted_without_its_record(sweeper, broken_audit) -> None:  # SystemExit(1); files still there
def test_failed_removal_is_recorded_as_error(sweeper, monkeypatch, audit_lines) -> None:
def test_batches_never_mix_users(sweeper, audit_lines) -> None:
def test_sweep_during_running_job(sweeper, store, clock) -> None:  # input expires after copy-in; register_output still works; input deleted with record
def test_dry_run_prints_counts_and_ages_only(capsys) -> None:  # no file name, id or user in output
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement the sweeper and the dry-run command**
- [ ] **Step 4: Run them to verify they pass**
- [ ] **Step 5: Commit** — `git commit -m "feat(retention): delete workspace files after the period, recorded first"`

### Task 16: Operator surface

**Files:**
- Create: `.env.example`, `scripts/bundle_images.sh`
- Modify: `Makefile` (adds `nuke`, `health`, and `help` entries for every target), `docker/compose.yaml` (passes every setting through `${VAR:-default}`)
- Test: `tests/test_compose_defaults.py`

**Interfaces:**
- Produces:
  - `.env.example` in the federation format:
    - `OPEN_WEBUI_WORKBENCH_KEY` tagged `[required] [secret] [shared: open-webui-service]`
    - every `WORKBENCH_*` default commented out
    - `WORKBENCH_EXECUTOR_MEM_LIMIT` with its formula
  - `make nuke`: `down` plus `docker volume rm workbench-data`, behind an interactive "type workbench to confirm" (pattern: open-webui-service's `nuke`).
  - `make health`: `ps` plus the api's `/healthz`.
  - `scripts/bundle_images.sh`: translator's script with `translator` replaced by `workbench`.

- [ ] **Step 1: Write the failing tests**

```python
def test_compose_defaults_match_the_spec() -> None:  # each WORKBENCH_* default in docker/compose.yaml equals the Global Constraints value
def test_executor_has_no_network_and_only_the_listed_capabilities() -> None:  # parse YAML: network_mode none; cap_add exact set
def test_api_joins_only_workbench_net() -> None:
def test_workspace_volume_is_external() -> None:
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Implement it**
- [ ] **Step 4: Verify** — Run `make test` and `python3 -B ../deploy/scripts/check_env_examples.py .`, then `docker compose --env-file .env.example -f docker/compose.yaml -f docker/compose.override.yaml config -q`. Expected: all pass.
- [ ] **Step 5: Commit** — `git commit -m "feat(ops): .env.example, nuke, health, bundle"`

### Task 17: Contract test against Open WebUI, and the load test

**Files:**
- Create: `contract/compose.yaml`, `tests/contract/test_open_webui_contract.py`, `tests/load/test_load.py`
- Modify: `Makefile` (adds `test-contract` and `test-load`), `.github/workflows/ci.yml` (adds a `contract` job), `.github/dependabot.yml` (adds a `docker-compose` entry for `/contract`)

**Interfaces:**
- Consumes: the whole service.
- Produces:
  - `contract/compose.yaml` pins `ghcr.io/open-webui/open-webui:v0.13.3@sha256:46b59158e6558eeb980bc828e0f1b30238ad24d82ed84d5e5eda1dab2a082f14`, the same tag and digest as open-webui-service.
  - It runs that image with `ENABLE_TOOL_SERVERS=true` and `TERMINAL_SERVER_CONNECTIONS` set to one bearer connection to `http://workbench-api:8000`, with `config.chat_uploads: "filesystem"` and an access grant for `*`.
  - The network is a private `workbench-net` for the test, plus workbench's own services via compose `include`.

- [ ] **Step 1: Write the contract tests** (`@pytest.mark.contract`; sign up a synthetic admin, then use its token)
  - `test_verify_sees_a_plain_terminal`: `POST /api/v1/configs/terminal_servers/verify` with the connection reports a terminal, not an orchestrator.
  - `test_spec_through_the_proxy_lists_exactly_the_tools`: `GET /api/v1/terminals/<id>/openapi.json` has operationIds exactly `{list_files, read_document, search_files, run_python, inspect_template, fill_template, write_table, write_document, display_file}`.
  - `test_upload_list_view_through_the_proxy`: the `/files/*` routes through `/api/v1/terminals/<id>/…` round-trip a synthetic CSV. workbench receives Open WebUI's user id as `X-User-Id`, which the test confirms with `GET /files/cwd` (it returns the chat folder).
  - `test_tool_call_through_the_proxy`: `POST /api/v1/terminals/<id>/tools/read_document` returns text.
  - `test_display_file_through_the_proxy`.
  - `test_open_webui_audit_skips_the_terminal_proxy`: with `AUDIT_EXCLUDED_PATHS=/terminals`, Open WebUI's stdout has no audit record whose `request_uri` contains the uploaded file's name.
- [ ] **Step 2: Write the load test** (`@pytest.mark.load`, `make test-load`; manual and not in CI)
  - 10 synthetic users each submit 3 `run_python` jobs that sleep 2 s and allocate 200 MB.
  - Assert: max concurrency equals `executor_slots`; refusals are `503` with `busy`; the executor's memory as read from `docker stats` stays below its limit.
  - Print p50 and p95 for queue wait and job duration.
- [ ] **Step 3: Run them**
  - `make test-contract`: expected PASS.
  - `make test-load`: expected PASS, with the numbers quoted in the PR.
- [ ] **Step 4: Commit** — `git commit -m "test: Open WebUI v0.13.3 contract and a 10-user load test"`

### Task 18: Documentation and release v0.1.0

**Files:**
- Create: `docs/README.md`, `docs/configuration.md`, `docs/permissions.md`, `docs/data-flow.md`, `docs/audit.md`, `docs/retention.md`, `docs/sandbox.md`, `.github/workflows/release-tag.yml`
- Modify: `CLAUDE.md`, `README.md`, `pyproject.toml` (`version = "0.1.0"`), `uv.lock`
- Test: `tests/test_docs.py`

**Interfaces:**
- Produces:
  - **`docs/permissions.md`:** per tool, what it reads, writes, records and keeps. It states the boundaries: the user is the boundary, the chat a folder, no network, no deletion by the agent.
  - **`docs/data-flow.md`:** what enters, where it is stored, for how long, and the copies in Open WebUI's chat history (365 days). It also has the section "Facts for the record of processing and the impact assessment": categories of data, recipients (none beyond the caller), storage period, technical measures, and the fact that the legal assessment stays with the operator (deploy ADR 0003).
  - **`docs/audit.md`:** in the federation shape (see translator `docs/audit.md`). It covers every event key from Task 5, that no purpose is recorded in version 1, and that `AUDIT_EXCLUDED_PATHS=/terminals` is set on the Open WebUI side.
  - **`docs/sandbox.md`:** the capabilities and why each one, the uid pool, the limits, the volume modes, the residual risk of a shared kernel, and gVisor as an option.
  - **`docs/retention.md`** and **`docs/configuration.md`** (every variable, matching `.env.example`).
  - **`release-tag.yml`:** translator's file with `version-file: pyproject.toml`.

- [ ] **Step 1: Write the failing tests**

```python
def test_every_env_variable_is_documented() -> None:  # each name in .env.example appears in docs/configuration.md
def test_every_tool_is_in_the_permission_matrix() -> None:  # each operationId from tool_openapi appears in docs/permissions.md
def test_every_audit_key_is_documented() -> None:  # each OPERATIONS key's operation string appears in docs/audit.md
```

- [ ] **Step 2: Run them to verify they fail**
- [ ] **Step 3: Write the docs and bump the version**
- [ ] **Step 4: Verify** — Run `make verify && make test && make test-isolation && make test-contract`. Expected: all green, quoted in the PR.
- [ ] **Step 5: Commit** — `git commit -m "docs: permissions, data flow, audit, sandbox, retention; release v0.1.0"`. Open the PR and **merge only after integration Task B1 (vllm-service 32k context) has merged.**
