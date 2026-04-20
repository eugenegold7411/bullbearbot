# Known Test Failures

Tests that are currently failing and have been triaged. Do not fix without
updating this document.

---

## ChromaDB tests excluded from CI

All tests in `tests/test_scratchpad_memory.py` are marked
`@pytest.mark.requires_chromadb` and are **excluded from CI** via
`pytest tests/ -m "not requires_chromadb"`.

**Why:** chromadb is not installed in the CI environment. The VPS has
`chromadb==1.5.7` installed and runs `make test` (full suite) which includes
these tests. CI runs `make test-ci` which skips them.

**Status:** Resolved — no longer listed as failures. Run `make test` on the VPS
or locally with chromadb installed to exercise the full suite.
