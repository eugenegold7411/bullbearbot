# ChromaDB Test Policy

---

## Which tests require ChromaDB

All tests in `tests/test_scratchpad_memory.py` interact with the ChromaDB vector store
directly (the three-tier scratchpad memory: `scratchpad_scenarios_short`,
`_medium`, `_long` collections). They require `chromadb==1.5.7` to be installed
and importable in the test process.

These tests carry `@pytest.mark.requires_chromadb` and are the only tests with this marker.

---

## Why they are excluded from CI

`chromadb` is not installed in the CI environment (GitHub Actions / any environment
running `make test-ci`). Installing ChromaDB in CI would pull in `onnxruntime`,
`protobuf`, and several ML-stack packages that add significant install time and
introduce version-conflict risk with the rest of the test environment.

Additionally, the `protobuf` version conflict (`Descriptors cannot be created directly`)
is environment-sensitive and required a runtime workaround
(`PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`) that is applied via `.env` and the
systemd service file on the VPS — but is not present in CI.

---

## Current policy

| Make target | ChromaDB tests included? |
|-------------|--------------------------|
| `make test-ci` | No — runs `pytest tests/ -m "not requires_chromadb"` |
| `make test` | Yes — runs full suite; intended for VPS or local dev with chromadb installed |

The VPS has `chromadb==1.5.7` installed and `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python`
set in both `.env` and the systemd service `EnvironmentFile`. `make test` passes all
ChromaDB tests on the VPS.

These tests are **not listed as failures** — they are intentionally excluded from CI.
Do not add them to a failures list or suppress them with `xfail`; the marker-based
exclusion is the correct mechanism.

---

## Condition for re-enabling as blocking CI tests

ChromaDB tests may be promoted to blocking CI once **all three** conditions hold:

1. `chromadb` (and its transitive deps) can be installed in CI without version conflicts
   and without adding more than ~2 minutes to CI install time.
2. `PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python` is set in the CI environment
   (via a CI environment variable or `.env` equivalent).
3. The `make test-ci` run with ChromaDB enabled shows zero flakes across three
   consecutive CI runs (ChromaDB's SQLite backend can produce intermittent
   `OperationalError: database is locked` under parallel test workers).

Until all three hold, `make test-ci` keeps the `not requires_chromadb` exclusion.
