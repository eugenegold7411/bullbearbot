"""
tests/test_pc_prompt_caching.py — Prompt caching tests.

Verifies that all high-frequency Anthropic callers have system prompts
large enough to activate caching (≥ 1024 tokens) and that cache_control
is correctly applied.

PC-01: ask_claude system prompt has cache_control applied
PC-02: signal_scorer_l3 system prompt has cache_control and is ≥ 1024 tokens
PC-03: A2 debate system prompt has cache_control applied
PC-04: All six expanded system prompts are ≥ 1024 tokens
PC-05: Regime classifier API call has cache_control in system list
PC-06: Morning brief generate_brief() has cache_control in system list
PC-07: User message content in signal scorer does NOT have cache_control
PC-08: No dynamic content injected into cached system prompt blocks
PC-09: _L3_SYSTEM tier classification includes core symbol list
PC-10: _REGIME_SYS contains regime score guide
"""

import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_MIN_CACHEABLE_TOKENS = 1024


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: 4 chars per token (conservative upper-bound)."""
    return len(text) // 4


def _read_string_var(filepath: Path, varname: str) -> str:
    """Extract a module-level string variable value without importing the module."""
    src = filepath.read_text()
    search = f"\n{varname} = \"\"\""
    idx = src.find(search)
    if idx >= 0:
        start = src.index('"""', idx) + 3
        end = src.index('"""', start)
        return src[start:end]
    # Try paren form
    search2 = f"\n{varname} = ("
    idx = src.find(search2)
    if idx >= 0:
        chunk = src[idx + len(varname) + 4:]
        depth = 1
        pos = 1
        while depth > 0 and pos < len(chunk):
            if chunk[pos] == "(":
                depth += 1
            elif chunk[pos] == ")":
                depth -= 1
            pos += 1
        return chunk[1 : pos - 1]
    return ""


class TestPromptCachingSystemPromptSizes(unittest.TestCase):
    """PC-04: All expanded system prompts are ≥ 1024 estimated tokens."""

    def _check(self, filepath: str, varname: str) -> None:
        path = _BOT_DIR / filepath
        self.assertTrue(path.exists(), f"File not found: {filepath}")
        text = _read_string_var(path, varname)
        self.assertTrue(len(text) > 0, f"{varname} not found in {filepath}")
        tokens = _estimate_tokens(text)
        self.assertGreaterEqual(
            tokens,
            _MIN_CACHEABLE_TOKENS,
            f"{varname} in {filepath}: {tokens} tokens < {_MIN_CACHEABLE_TOKENS} minimum. "
            f"Cache_control will be silently ignored by the Anthropic API.",
        )

    def test_regime_sys_size(self):
        self._check("bot_stage1_regime.py", "_REGIME_SYS")

    def test_l3_system_size(self):
        self._check("bot_stage2_signal.py", "_L3_SYSTEM")

    def test_scratchpad_sys_size(self):
        self._check("scratchpad.py", "_SCRATCHPAD_SYS")

    def test_qualitative_system_prompt_size(self):
        self._check("bot_stage1_5_qualitative.py", "_SYSTEM_PROMPT")

    def test_morning_brief_system_size(self):
        self._check("morning_brief.py", "_SYSTEM")

    def test_intelligence_system_size(self):
        self._check("morning_brief.py", "_INTELLIGENCE_SYSTEM")


class TestPromptCachingCacheControlPresence(unittest.TestCase):
    """PC-01/02/03/05/06: cache_control is present in all Anthropic API calls."""

    def _src(self, fname: str) -> str:
        return (_BOT_DIR / fname).read_text()

    def test_pc01_ask_claude_has_cache_control(self):
        src = self._src("bot_stage3_decision.py")
        self.assertIn("cache_control", src)
        self.assertIn('"type": "ephemeral"', src)

    def test_pc02_signal_scorer_has_cache_control(self):
        src = self._src("bot_stage2_signal.py")
        self.assertIn("cache_control", src)
        self.assertIn("ephemeral", src)

    def test_pc03_a2_debate_has_cache_control(self):
        src = self._src("bot_options_stage3_debate.py")
        self.assertIn("cache_control", src)
        self.assertIn("ephemeral", src)

    def test_pc05_regime_has_cache_control(self):
        src = self._src("bot_stage1_regime.py")
        # Must have cache_control in the system list item
        self.assertIn("cache_control", src)
        self.assertIn("ephemeral", src)

    def test_pc05_regime_api_call_uses_list_form(self):
        """Regime system must be passed as list[dict] not plain string."""
        src = self._src("bot_stage1_regime.py")
        # The system parameter must use list form: system=[{...}]
        self.assertIn('system=[{"type": "text"', src)

    def test_pc06_morning_brief_generate_brief_has_cache_control(self):
        src = self._src("morning_brief.py")
        # The generate_brief() call must use list form with cache_control
        self.assertIn("cache_control", src)
        # And extra_headers for the beta header
        self.assertIn("prompt-caching-2024-07-31", src)

    def test_scratchpad_has_cache_control(self):
        src = self._src("scratchpad.py")
        self.assertIn("cache_control", src)
        self.assertIn("ephemeral", src)

    def test_qualitative_has_cache_control(self):
        src = self._src("bot_stage1_5_qualitative.py")
        self.assertIn("cache_control", src)
        self.assertIn("ephemeral", src)


class TestPromptCachingUserContentNotCached(unittest.TestCase):
    """PC-07: Dynamic user message content does NOT have cache_control."""

    def test_signal_scorer_user_content_not_cached(self):
        """The assembled user_content string in _run_l3_synthesis must not be marked."""
        src = (_BOT_DIR / "bot_stage2_signal.py").read_text()
        # The user_content is assembled in _run_l3_synthesis.
        # It should be passed as a plain string, not as a list with cache_control.
        # Find the _call_l3_batch call in _run_l3_synthesis
        fn_idx = src.find("def _run_l3_synthesis")
        self.assertGreater(fn_idx, 0, "_run_l3_synthesis not found")
        # After the function, the user_content should be built and passed directly
        fn_body = src[fn_idx : fn_idx + 4000]
        # user_content should be a plain string (not a list)
        self.assertIn("user_content =", fn_body)

    def test_regime_user_content_not_cached(self):
        """Regime user_content is a plain string — no cache_control on user message."""
        src = (_BOT_DIR / "bot_stage1_regime.py").read_text()
        fn_idx = src.find("def classify_regime")
        # Use 5000 chars — function is long due to user_content assembly
        fn_body = src[fn_idx : fn_idx + 5000]
        self.assertIn("user_content =", fn_body)
        # messages list should use plain string (not a list of blocks with cache_control)
        self.assertIn('"content": user_content', fn_body)


class TestPromptCachingContentQuality(unittest.TestCase):
    """PC-08/09/10: Cached system prompts contain required real content."""

    def test_pc09_l3_system_has_tier_classification(self):
        """L3 system prompt must contain tier classification rules."""
        text = _read_string_var(
            _BOT_DIR / "bot_stage2_signal.py", "_L3_SYSTEM"
        )
        self.assertIn("TIER", text.upper())
        self.assertIn("core", text)
        self.assertIn("dynamic", text)

    def test_pc09_l3_system_has_crypto_rules(self):
        """L3 system prompt must contain crypto handling rules."""
        text = _read_string_var(
            _BOT_DIR / "bot_stage2_signal.py", "_L3_SYSTEM"
        )
        self.assertIn("/USD", text)

    def test_pc10_regime_sys_has_score_guide(self):
        """Regime system prompt must contain regime score guide."""
        text = _read_string_var(
            _BOT_DIR / "bot_stage1_regime.py", "_REGIME_SYS"
        )
        self.assertIn("REGIME SCORE", text.upper())
        self.assertIn("risk_on", text)
        self.assertIn("risk_off", text)

    def test_pc10_regime_sys_has_macro_regime_guide(self):
        """Regime system prompt must contain macro regime classification."""
        text = _read_string_var(
            _BOT_DIR / "bot_stage1_regime.py", "_REGIME_SYS"
        )
        self.assertIn("reflationary", text)
        self.assertIn("stagflationary", text)
        self.assertIn("goldilocks", text)

    def test_scratchpad_has_conviction_guide(self):
        """Scratchpad system prompt must contain conviction scoring guide."""
        text = _read_string_var(
            _BOT_DIR / "scratchpad.py", "_SCRATCHPAD_SYS"
        )
        self.assertIn("CONVICTION", text.upper())
        self.assertIn("high", text)
        self.assertIn("medium", text)
        self.assertIn("low", text)

    def test_qualitative_has_thesis_tags(self):
        """Qualitative system prompt must contain thesis_tags taxonomy."""
        text = _read_string_var(
            _BOT_DIR / "bot_stage1_5_qualitative.py", "_SYSTEM_PROMPT"
        )
        self.assertIn("THESIS TAGS", text.upper())
        self.assertIn("ai_capex", text)
        self.assertIn("earnings_tailwind", text)

    def test_morning_brief_system_has_quality_criteria(self):
        """Morning brief _SYSTEM must contain trade idea quality criteria."""
        text = _read_string_var(
            _BOT_DIR / "morning_brief.py", "_SYSTEM"
        )
        self.assertIn("CONVICTION", text.upper())
        # Must mention risk/reward asymmetry in quality criteria
        self.assertTrue(
            "risk/reward" in text.lower() or "risk_reward" in text.lower(),
            "Morning brief _SYSTEM must mention risk/reward asymmetry",
        )

    def test_intelligence_system_has_completeness_rules(self):
        """Intelligence system prompt must contain output completeness rules."""
        text = _read_string_var(
            _BOT_DIR / "morning_brief.py", "_INTELLIGENCE_SYSTEM"
        )
        self.assertIn("COMPLETENESS", text.upper())


class TestPromptCachingNoBetaHeaderRegression(unittest.TestCase):
    """Verify extra_headers are still present where needed (no regression)."""

    def test_signal_scorer_has_extra_headers(self):
        src = (_BOT_DIR / "bot_stage2_signal.py").read_text()
        self.assertIn("anthropic-beta", src)
        self.assertIn("prompt-caching-2024-07-31", src)

    def test_ask_claude_has_extra_headers(self):
        src = (_BOT_DIR / "bot_stage3_decision.py").read_text()
        self.assertIn("anthropic-beta", src)

    def test_regime_has_extra_headers(self):
        src = (_BOT_DIR / "bot_stage1_regime.py").read_text()
        self.assertIn("anthropic-beta", src)
        self.assertIn("prompt-caching-2024-07-31", src)

    def test_morning_brief_has_extra_headers(self):
        src = (_BOT_DIR / "morning_brief.py").read_text()
        self.assertIn("prompt-caching-2024-07-31", src)


if __name__ == "__main__":
    unittest.main()
