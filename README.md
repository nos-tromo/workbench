# workbench

The nos-tromo federation's shared tool tier: a FastAPI tool API plus a sandboxed
executor for free Python and office-document work. Open WebUI is its first
caller.

Design: `docs/superpowers/specs/2026-11-13-workbench-design.md`.

```bash
uv sync --group dev --extra executor
make verify   # ruff + pyrefly
make test     # pytest
```
