# Contributing to InferLens

Thank you for your interest in InferLens! We welcome issues, docs, and code.
These conventions are deliberately modeled on
[vLLM's contribution process](https://docs.vllm.ai/en/latest/contributing/) so
they feel familiar to inference-engine contributors.

## Developer setup

We use [uv](https://docs.astral.sh/uv/) exclusively — never the system
`python`/`pip`.

```bash
git clone https://github.com/rabrooks/inferlens
cd inferlens
uv sync                      # creates .venv with dev dependencies
uv run pre-commit install    # installs git hooks (lint runs on commit)
uv run pytest                # run the test suite
```

## Coding standards

- **Python:** [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html),
  enforced by `ruff` (lint + format, 88-column lines, Google-style docstrings).
  Public functions and classes need docstrings and type hints; `mypy` must pass.
- **TypeScript (viewer, planned):** [Google TypeScript Style Guide](https://google.github.io/styleguide/tsguide.html),
  enforced by ESLint + Prettier once the viewer lands.
- **C++/CUDA (if ever needed):** [Google C++ Style Guide](https://google.github.io/styleguide/cppguide.html),
  enforced by `clang-format`.
- Comments explain *why*, not *what*. No commented-out code.
- The core package (`inferlens.schema`, `inferlens.trace_io`, `inferlens.cli`)
  must stay dependency-free and importable without any engine installed.
  Engine collectors import their engine lazily, never at module import time.
- Adding support for a new engine? The collector contract lives in
  [docs/collectors.md](docs/collectors.md).

Run everything locally before pushing:

```bash
uv run pre-commit run --all-files
uv run pytest
```

## Pull requests

- **Sign your work (DCO).** Every commit needs a `Signed-off-by:` line
  (`git commit -s`), certifying the [Developer Certificate of Origin](DCO).
- **Title prefix**, vLLM-style: `[Schema]`, `[Collector]`, `[Viewer]`, `[CLI]`,
  `[Bugfix]`, `[Doc]`, `[CI]`, or `[Misc]` — e.g.
  `[Collector] Emit KV eviction events from vLLM stat logger`.
- **Check for duplicate work** before starting: search open issues and PRs.
- Keep PRs focused; bundle mechanical cleanups rather than sending them one by
  one. New behavior needs tests.
- Trace-format changes must update `docs/TRACE_SPEC.md` in the same PR and
  follow its versioning rules.

## AI-assisted contributions

AI assistance is welcome; AI *ownership* is not. If you used an AI tool
substantially, disclose it in the PR description and with a
`Co-authored-by:` commit trailer. You must understand and be able to defend
every line you submit — "the model wrote it" is not an answer in review.
Fully auto-generated PRs will be closed.

## Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). Be kind;
assume good faith.
