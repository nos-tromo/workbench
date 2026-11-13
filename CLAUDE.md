# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Data confidentiality — hard rule

**NEVER expose actual production or testing data in any file committed or
pushed to git.** This covers not only file contents but also metadata that
references real data: filenames, file descriptions, social-media account
names or handles, user identifications, sample records, log excerpts, and
screenshots. It applies everywhere git sees — source code, tests, fixtures,
docs, examples, configs, commit messages, and CI files. Use fully synthetic,
invented placeholders instead.

**Likewise, NEVER expose local filepaths from development machines** —
absolute paths or home directories such as `/Users/<name>/...`,
`/home/<name>/...`, or `C:\Users\...` — anywhere git sees. The only
permitted paths are relative project paths starting from the project's
root (e.g. `docker/compose.yaml`).

## What this repo is

`workbench` is the nos-tromo federation's shared tool tier: a FastAPI `api` that
holds a short-lived workspace per user and runs typed tools over it, plus a
sandboxed `executor` that runs free Python and office-document work with no
network. Its first caller is Open WebUI. The design, decisions and boundaries
are in `docs/superpowers/specs/2026-11-13-workbench-design.md`; the build plan
is `docs/superpowers/plans/2026-11-13-workbench-v1.md`. This file is a stub
until the release task completes it.

Toolchain: `uv` (`pyproject.toml` + `uv.lock`), `ruff` + `pyrefly` via
`pre-commit`, `pytest`. `make verify` before pushing, `make test` for the suite.
`make/common.mk`, `scripts/bundle-lib.sh` and the two files under
`workbench/utils/` (`audit_event.py`, `audit_runtime.py`) are vendored verbatim
from `nos-tromo/.github`; never edit them, CI fails on drift.
