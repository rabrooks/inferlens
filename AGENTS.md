# InferLens — agent and contributor quick reference

InferLens provides engine-neutral observability for LLM inference engines
(vLLM first, SGLang planned): collectors record scheduler, batching, and
KV-cache behavior to a portable trace file, and a local viewer renders it as
an interactive timeline.

The normative trace format lives in `docs/TRACE_SPEC.md` — any schema change
must update it in the same PR. Full contribution guidelines:
`CONTRIBUTING.md`.

## Commands

```bash
uv sync                          # env + dev deps (never system python/pip)
uv run pytest                    # tests
uv run ruff check --fix . && uv run ruff format .
uv run mypy inferlens
uv run pre-commit run --all-files
```

## Hard rules

- The core package (`inferlens.schema`, `inferlens.trace_io`,
  `inferlens.cli`) stays dependency-free. Engine collectors import their
  engine lazily, never at module import time — `import inferlens` must work
  without vLLM installed.
- Merely installing inferlens must never change an engine's behavior: the
  vLLM stat-logger plugin is inert unless `INFERLENS_TRACE_PATH` is set.
- Commits: `git commit -s` (DCO). PR titles: `[Schema]`, `[Collector]`,
  `[Viewer]`, `[CLI]`, `[Bugfix]`, `[Doc]`, `[CI]`, `[Misc]`.
- Google Python style, Google docstrings, full type hints; ruff + mypy must
  pass. Comments explain *why*, not *what*.
- Never commit trace files (`*.ilens*` are gitignored).
- Data we need but an engine doesn't expose goes in `docs/upstream-gaps.md`
  as an upstream-contribution candidate — never monkeypatch engine internals.
