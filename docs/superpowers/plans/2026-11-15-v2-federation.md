# workbench v2 federation side Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give docint its own workbench key in the federation (env-sync rows, ADR 0006, tech-stack lines), make open-webui-service's example comment about workbench's key true for workbench v0.2.0, and update the workspace map.

**Architecture:** No new code. D1 and O1 are one branch and PR each, in that repo's own conventions; D1 has tests, O1 is checked by deploy's format checker. L1 is a local edit to `infra/CLAUDE.md`, which is not a git repository, so it gets no PR. D1 goes first, because workbench's and docint's examples carry `[shared: ...]` tags that rule T5 checks against D1's rows. O1 waits for workbench v0.2.0. L1 goes with docint's PR.

**Tech Stack:** Make, stdlib Python `unittest` (deploy's env-sync), Markdown, `.env.example`.

**Spec:** `docs/superpowers/specs/2026-11-15-v2-mcp-docint-design.md` (§1, §3.8, §4.5, §5, §6, §7); companion: `docs/superpowers/plans/2026-11-13-workbench-federation-integration.md`.

## Global Constraints

- **Rows**, verbatim (§5):
  ```
  workbench-docint-key  workbench  DOCINT_WORKBENCH_KEY  gen
  workbench-docint-key  docint     WORKBENCH_API_KEY     gen
  ```
- **Tags the members must carry** (what rule T5 checks): workbench `[secret] [shared: docint WORKBENCH_API_KEY]`; docint `[secret] [shared: workbench DOCINT_WORKBENCH_KEY]`. Both keys are optional and empty by default, so neither is `[required]` (§3.8, §4.5).
- **Keys:** at least 32 characters; each caller has its own; workbench refuses two callers with one key.
- **docint's backend** joins `workbench-net` (alias `docint-backend`); its frontend does not (§4.5).
- **Unchanged:** the tier order inference → data → obs → workbench → apps → edge; the audit emitter `workbench` / `api`; open-webui-service's connection, tools and key.
- **Versions:** deploy 0.9.0 → 0.10.0 (minor). open-webui-service 0.14.0 → 0.14.1 (patch: a comment). Bump `VERSION`, commit, and let the `release-tag` workflow mint the tag on merge; never tag by hand.
- **Each repo's rules:** branch plus PR; single non-compound `gh` commands; synthetic data only; no developer paths (`/Users/...`, `/home/...`) in anything git sees; `Edit`, never `Write`, on existing test files; confirm CI ran on the last push with `gh pr checks`. Approval of this plan is the approval their `CLAUDE.md` files ask for before editing.
- **Docs stay proportionate:** a few short additions to existing pages; ADR 0006 follows 0004's sections briefly.

## Review Focus

1. **deploy is newer than workbench or docint** (their examples lack the new key). Expected: `make env` warns `table`, skips the group, generates nothing, and does not block `make up`. Test: D1 `test_the_group_waits_until_both_examples_document_the_key`.
2. **`make env ENV_GENERATE=1` with all caller keys unset.** Expected: Open WebUI's key and docint's key get different values, each equal on both sides. Test: D1 `test_each_caller_gets_its_own_generated_key`.
3. **The tag text the other two plans must use drifts.** Expected: rule T5 fails in the member's CI. Test: D1 `test_t5_workbench_docint_key_tags`.
4. **workbench v0.2.0 drops `[required]` from its key and open-webui-service keeps it.** Expected: no such disagreement. Today it is rule X2, which fails deploy's own `test_migrated_siblings_follow_the_format` whenever both examples are on disk. Test: O1 Step 2 (fails first) and Step 5.
5. **A host that already holds an active `OPEN_WEBUI_WORKBENCH_KEY=` line in `.env`.** Expected: nothing changes; env-sync keeps the operator's line and Open WebUI's connection is untouched. Test: O1 Step 5 (`git diff` touches no file under `docker/`; the compose-defaults tests pass).

---

### Task D1: deploy: the docint key group, ADR 0006

**Files (deploy):**
- Modify: `env-sync.conf` (the two rows, after the `workbench-key` rows); `docs/decisions/0005-workbench-shared-tool-tier.md`; `docs/README.md` (one index row after 0005's); `docs/tech-stack.md`, `docs/tech-stack.de.md`; `docs/runbooks/bring-up.md` ("The caller key" paragraph); `VERSION`.
- Create: `docs/decisions/0006-docint-calls-workbench-over-mcp.md`.
- Test: `tests/test_envsync_shared.py`, `tests/test_envsync_conventions.py`.

**Interfaces:**
- Consumes: nothing.
- Produces: env-sync group `workbench-docint-key` (two rows, `gen`, no source). The workbench and docint plans use the tags in Global Constraints, which D1's third test pins.

- [ ] **Step 1: Branch** — `git switch main`, `git pull --ff-only`, `git switch -c feat/docint-workbench-mcp`.
- [ ] **Step 2: Write the failing tests** (Edit the existing files)

```python
# test_envsync_shared.py
# GROUP_NAMES: "workbench-docint-key" after "workbench-key"
# test_real_table_loads: len(rows) == 32
# test_shared_keys_for: docint gains "WORKBENCH_API_KEY", workbench gains "DOCINT_WORKBENCH_KEY"

WORKBENCH_KEY = (
    "workbench-key  workbench           OPEN_WEBUI_WORKBENCH_KEY  gen\n"
    "workbench-key  open-webui-service  OPEN_WEBUI_WORKBENCH_KEY  gen\n"
)
DOCINT_KEY = (
    "workbench-docint-key  workbench  DOCINT_WORKBENCH_KEY  gen\n"
    "workbench-docint-key  docint     WORKBENCH_API_KEY     gen\n"
)

class WorkbenchDocintKeyTests(SharedCase):
    def test_each_caller_gets_its_own_generated_key(self) -> None:
        # start(WORKBENCH_KEY + DOCINT_KEY); every key unset in all three members
        # apply(generate=True, token=Tokens("a" * 64, "b" * 64)) == []
        # OPEN_WEBUI_WORKBENCH_KEY is "a" * 64 in workbench and open-webui-service;
        # DOCINT_WORKBENCH_KEY and docint's WORKBENCH_API_KEY are "b" * 64; origin "generated"

    def test_the_group_waits_until_both_examples_document_the_key(self) -> None:
        # start(DOCINT_KEY); docint's example lacks WORKBENCH_API_KEY; before = snapshot()
        # apply(generate=True) == [Event("table", ["env-sync.conf: WORKBENCH_API_KEY is not in docint's example"], "warn")]
        # snapshot() == before
```

```python
# test_envsync_conventions.py, TagRuleTests
def test_t5_workbench_docint_key_tags(self) -> None:
    real = load_table((ROOT / "env-sync.conf").read_text())
    for member, key, tag in (
        ("workbench", "DOCINT_WORKBENCH_KEY", "[secret] [shared: docint WORKBENCH_API_KEY]"),
        ("docint", "WORKBENCH_API_KEY", "[secret] [shared: workbench DOCINT_WORKBENCH_KEY]"),
    ):
        block = f"# A synthetic caller key.\n# {tag}\n# {key}=\n"
        text = mutate(VALID, "# DATA_NET=data-net\n", "# DATA_NET=data-net\n\n" + block)
        self.assertNotIn(("T5", key), found(text, member, groups=real))
        wrong = mutate(text, tag, "[secret]")
        self.assertIn(("T5", key), found(wrong, member, groups=real))
```

- [ ] **Step 3: Run them to verify the pins fail** — `python3 -B -m unittest discover -s tests -p 'test_*.py'`. Expected: `FAILED (failures=3)`: `test_real_table_loads`, `test_shared_keys_for`, `test_t5_workbench_docint_key_tags`. The two `WorkbenchDocintKeyTests` pass already, as they carry their own table.
- [ ] **Step 4: Make the changes**
  - `env-sync.conf`: the two rows, verbatim. `VERSION`: `0.10.0`.
  - **ADR 0006**, `# 0006 — docint calls workbench over an MCP door`, with `Status: accepted (<date>)` and `Date: <date>` as 0004 has, then Context / Decision / Alternatives considered / Consequences. About 60 lines, no more than 0004's 67.
    - *Context:* 0005 left version 2 to a decision of its own. docint's chat model needs Python on what a turn retrieved (passages, keyword counts, topic listings), without a sandbox of its own (0005's invariant); later AI services should find workbench's tools in one place.
    - *Decision* (one bullet each):
      - The door: `POST /mcp` on the api, hand-written, stateless, JSON-only, built from the tool registry; revisions `2026-07-28`, `2025-11-25`, `2025-06-18`.
      - A key, a tool list and limits per caller. docint: key `DOCINT_WORKBENCH_KEY` (docint's `WORKBENCH_API_KEY`, env-sync group `workbench-docint-key`), tool `run_python` only, wall 60 s, 2 running jobs (fewer than the executor's slots, so one stays free). Open WebUI keeps the terminal door, its nine tools and its key. A key reaches only its own door; the other door answers `404`.
      - Inline data, 512 KiB at most per call, kept only for the job and deleted with it; docint has no workspace. docint records the hand-over as a `disclose` with purpose; workbench records `combine` / `tool.run_python.inline` with count, job, purpose and request id, never content or a digest. docint's user id at workbench is a keyed pseudonym of the owner name.
      - docint's backend joins `workbench-net` (alias `docint-backend`), its frontend does not: a new trust edge. The key is a bearer secret; its holder can run code as docint under any user id and reaches no workspace.
      - The `analysis` artifact: table, charts, code and provenance of a `run_python` in a chat turn, stored with the turn, addable to a Report Builder report, exportable as md, html, pdf, json and zip.
      - Unchanged: the tier order, obs-plane (same emitter), open-webui-service.
    - *Alternatives* (one clause of reason each): the official SDK (`mcp` 2.x, MIT; about 15 more packages in the api image, three native; it stays in the dev group for a conformance test); uploads into a workspace (docint's data would enter a 7-day workspace lifecycle, where inline data leaves nothing); a sandbox in docint (against 0005's invariant, and next to its collections on `data-net`); a network of its own for MCP callers (a fifth seam, where a key and door per caller already separate them); Open WebUI over MCP (it passes no files over MCP, as 0005 says).
    - *Consequences:* workbench v0.2.0 ships before docint's minor, and docint works without workbench (no key: no tool; unreachable: the call answers `unavailable`, later turns offer none). One more shared secret group; `make env ENV_GENERATE=1` writes it. The two callers' keys must differ. Residual risks (spec §6, keep four): the hand-over is recorded after the data has left, so a docint killed between send and settle loses its `disclose` events (workbench's own event still holds job, purpose and request id); an abandoned call runs to its wall time; docint's backend and Open WebUI can reach each other on `workbench-net`; workbench holds the key the pseudonyms are made with. A last line: the record states no legal conclusions, as 0005.
  - `0005`: one line under `Date:`, `Extended by [0006](0006-docint-calls-workbench-over-mcp.md).` `docs/README.md`: an index row for 0006 in 0005's style.
  - `docs/tech-stack.md`: the `workbench-net` bullet reads "tool callers (Open WebUI, docint's backend, alias `docint-backend`) ↔ `workbench` (alias `workbench-api`)", and the `api` row ends "one bearer key per caller (open-webui-service on the terminal door, docint's backend on an MCP door; ADR 0006)" where it now says "bearer key shared with open-webui-service". `docs/tech-stack.de.md`: the same two lines in German ("Werkzeug-Aufrufer (Open WebUI, Backend von docint, Alias `docint-backend`)"; "ein Bearer-Key je Aufrufer (open-webui-service am Terminal-Zugang, Backend von docint an einem MCP-Zugang; ADR 0006)"). Leave the `(v0.1.0)` labels alone.
  - `docs/runbooks/bring-up.md`, after the first sentence of "The caller key": from workbench v0.2.0 the api starts with either `OPEN_WEBUI_WORKBENCH_KEY` or `DOCINT_WORKBENCH_KEY` (at least 32 characters each), refuses to start with neither, and `make env ENV_GENERATE=1` also writes `DOCINT_WORKBENCH_KEY` into workbench's and docint's `.env` (group `workbench-docint-key`).
- [ ] **Step 5: Verify** — deploy has no `make test`; its gate is CI's commands:
  - `python3 -B -m unittest discover -s tests -p 'test_*.py'`. Expected on a workspace with the sibling repos beside it: exactly one failure, `test_table_keys_in_member_examples` (`workbench DOCINT_WORKBENCH_KEY`), until workbench and docint carry the keys. That test is skipped in CI, where no sibling is checked out.
  - `shellcheck scripts/*.sh`, then `make help >/dev/null`, then `make -n ps >/dev/null`. Expected: no output.
  - `git add -A`, then `git diff --cached | grep -n '/Users/\|/home/'`. Expected: no output.
- [ ] **Step 6: Commit, then run the suite as CI does** — `git commit -m "feat: docint calls workbench over MCP (ADR 0006); release v0.10.0"`, then in a clone without siblings: `git clone -q . "$(mktemp -d)/deploy"`; in it, `python3 -B -m unittest discover -s tests -p 'test_*.py'`. Expected: `OK`, the two sibling tests skipped.
- [ ] **Step 7: Open the PR** — `git push -u origin feat/docint-workbench-mcp`, then `gh pr create --title "feat: docint calls workbench over MCP (ADR 0006)" --body "<summary; quote Step 5 and 6 output; note the one workspace-only failure>"`, then `gh pr checks`.

### Task O1: open-webui-service: workbench's key in the example

Needs D1 merged and workbench v0.2.0 merged (its `.env.example` is what Step 2 checks against).

**Files (open-webui-service):**
- Modify: `.env.example` (the `OPEN_WEBUI_WORKBENCH_KEY` block), `VERSION` (0.14.0 → 0.14.1).
- Test: none to write. `retention/tests` pins no example text; deploy's checker is the gate.

**Interfaces:**
- Consumes: D1's rows; workbench v0.2.0's `.env.example`, where the key is commented, in no Required section, tagged `[secret] [shared: open-webui-service]`.
- Produces: nothing for later tasks.

- [ ] **Step 1: Branch** — `git switch main`, `git pull --ff-only`, `git switch -c fix/workbench-key-example`. Have workbench's v0.2.0 checkout beside this repo (`git -C ../workbench tag --list v0.2.0` lists it).
- [ ] **Step 2: Run the cross-check to see it fail** — `python3 -B ../deploy/scripts/check_env_examples.py ../workbench .`. Expected: exit 1, one line, `open-webui-service/.env.example:41: X2 OPEN_WEBUI_WORKBENCH_KEY rows of one env-sync.conf group agree on [required]`. This is why O1 changes the tag as well as the words: workbench's key is optional, and so is Open WebUI's, as the block's own last sentence says.
- [ ] **Step 3: Make the change** in `.env.example`: take the block out of "Required" and put it in "Auth & identity", after the `OPEN_WEBUI_FORWARD_JWT_SECRET` block, as a commented setting without `[required]`:

```
# The bearer key Open WebUI presents to workbench, the federation's tool tier
# (CLAUDE.md: Workbench). Same value as OPEN_WEBUI_WORKBENCH_KEY in
# workbench's .env; make env fills it. At least 32 characters: workbench
# refuses to start with a shorter one. It is written into a JSON value, so by
# hand use `openssl rand -hex 32`. Unset -> tool servers stay off and Open
# WebUI runs without workbench.
# [secret] [shared: workbench]
# OPEN_WEBUI_WORKBENCH_KEY=
```

  Check the 32-character sentence against workbench v0.2.0's `workbench/config.py` message before keeping it. Set `VERSION` to `0.14.1`.
- [ ] **Step 4: Check the claim has one home** — `grep -rn "refuses to start" .env.example CLAUDE.md README.md`. Expected: only the new sentence.
- [ ] **Step 5: Verify**
  - `python3 -B ../deploy/scripts/check_env_examples.py ../workbench .`. Expected: no output, exit 0. `python3 -B ../deploy/scripts/check_env_examples.py .` likewise.
  - `python3 -B -m unittest discover -s retention/tests -t retention`. Expected: `OK` (158 tests).
  - `git diff --stat main`. Expected: `.env.example` and `VERSION` only. `git diff main | grep -n '/Users/\|/home/'`. Expected: no output.
  - In deploy: `python3 -B -m unittest discover -s tests -p 'test_envsync_examples.py'`. Expected: `OK`.
- [ ] **Step 6: Commit and open the PR** — `git commit -m "docs(env): workbench's key is optional from workbench v0.2.0; release v0.14.1"`, `git push -u origin fix/workbench-key-example`, `gh pr create --title "docs(env): workbench's key is optional from workbench v0.2.0" --body "<why the tag moved; Step 2 and 5 output>"`, `gh pr checks`.

### Task L1: the workspace map: `infra/CLAUDE.md`

A local workspace file, not a git repository: no branch, no PR. Do it when docint's PR merges, so the map describes what is merged.

- [ ] **Step 1: Make the edits** (all in `infra/CLAUDE.md`)
  - The diagram's `workbench-net` block (the lines under "Tool calls cross the fourth seam"): `docint-backend` joins `workbench-net` next to `open-webui`, and the lead-in names both callers.
  - The invariant "Tool execution is shared, never embedded": replace "Open WebUI, the one caller so far, connects … (the same value in both repos)" with two callers. Open WebUI uses the terminal door with all nine tools and `OPEN_WEBUI_WORKBENCH_KEY`. docint's backend uses an MCP door with `run_python` only, data inline, and `DOCINT_WORKBENCH_KEY` (docint's `WORKBENCH_API_KEY`). A key reaches only its own door. The decision records are `0005-…` and `0006-docint-calls-workbench-over-mcp.md`.
  - The invariant "Four network seams": docint's backend also attaches to `workbench-net` (alias `docint-backend`; its frontend does not); `workbench`'s other members are "its callers (open-webui and docint's backend, which keep their other seams)".
  - "Where to look", `workbench/docs/` entry: "the caller key shared with open-webui-service" becomes "the caller keys shared with open-webui-service and docint".
- [ ] **Step 2: Verify** — `grep -n 'docint-backend\|DOCINT_WORKBENCH_KEY' CLAUDE.md` shows the diagram, the invariant and the seams bullet. `grep -n '/Users/\|/home/' CLAUDE.md` shows only the two placeholder lines of the confidentiality rule at the top (lines 16-17), no real path.
