#!/usr/bin/env python3
"""
validate_config.py — Pre-deploy config validation for BullBearBot.

Run before any deploy to catch configuration inconsistencies.
Exit 0 = all checks passed (or only warnings). Exit 1 = one or more failures.

Usage:
    cd /home/trading-bot
    python3 validate_config.py
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).parent

PASS = "\u2705 PASS"
WARN = "\u26a0\ufe0f  WARN"
FAIL = "\u274c FAIL"

results = []

def check(status, msg):
    results.append((status, msg))
    print(f"{status} — {msg}")


# ─────────────────────────────────────────────────────────────────────────────
# strategy_config.json
# ─────────────────────────────────────────────────────────────────────────────
cfg_path = BASE_DIR / "strategy_config.json"
cfg = {}
try:
    cfg_text = cfg_path.read_text()
    cfg = json.loads(cfg_text)
    check(PASS, "strategy_config.json: file parses OK")
except Exception as e:
    check(FAIL, f"strategy_config.json: cannot parse — {e}")
    cfg_text = ""

if cfg:
    # Placeholder check
    for placeholder in ("$0K", "TODO", "TBD", "PLACEHOLDER"):
        if placeholder in cfg_text:
            check(WARN, f"strategy_config.json: contains placeholder '{placeholder}'")
    # "0K" needs special handling — only flag if it looks like a dollar placeholder
    if re.search(r'\$\s*0K|\b0K\b', cfg_text):
        check(WARN, "strategy_config.json: contains '0K' dollar placeholder")
    else:
        check(PASS, "strategy_config.json: no dollar placeholders found")

    # max_single_position_pct must be absent (deprecated — unified under
    # max_single_name_pct=0.04 and max_position_pct_equity=0.07)
    ps_pct  = cfg.get("position_sizing", {}).get("max_single_position_pct")
    par_pct = cfg.get("parameters",      {}).get("max_single_position_pct")
    if ps_pct is not None or par_pct is not None:
        check(FAIL, (f"strategy_config.json: deprecated max_single_position_pct still present "
                     f"(position_sizing={ps_pct}, parameters={par_pct}) — "
                     f"remove both; use max_single_name_pct + max_position_pct_equity"))
    else:
        check(PASS, "strategy_config.json: max_single_position_pct correctly absent (deprecated)")

    # cash_reserve_pct
    crp = cfg.get("position_sizing", {}).get("cash_reserve_pct")
    if crp is None:
        check(FAIL, "strategy_config.json: cash_reserve_pct missing")
    elif 0.10 <= float(crp) <= 0.40:
        check(PASS, f"strategy_config.json: cash_reserve_pct={crp} (valid 0.10–0.40)")
    else:
        check(FAIL, f"strategy_config.json: cash_reserve_pct={crp} out of range (0.10–0.40)")

    # max_positions
    mp = cfg.get("parameters", {}).get("max_positions")
    if mp is None:
        check(FAIL, "strategy_config.json: max_positions missing")
    elif 5 <= int(mp) <= 25:
        check(PASS, f"strategy_config.json: max_positions={mp} (valid 5–25)")
    else:
        check(FAIL, f"strategy_config.json: max_positions={mp} out of range (5–25)")

    # stop_loss_pct_core
    slp = cfg.get("parameters", {}).get("stop_loss_pct_core")
    if slp is None:
        check(FAIL, "strategy_config.json: stop_loss_pct_core missing")
    elif 0.01 <= float(slp) <= 0.10:
        check(PASS, f"strategy_config.json: stop_loss_pct_core={slp} (valid 0.01–0.10)")
    else:
        check(FAIL, f"strategy_config.json: stop_loss_pct_core={slp} out of range (0.01–0.10)")

    # take_profit_multiple
    tpm = cfg.get("parameters", {}).get("take_profit_multiple")
    if tpm is None:
        check(FAIL, "strategy_config.json: take_profit_multiple missing")
    elif 1.5 <= float(tpm) <= 5.0:
        check(PASS, f"strategy_config.json: take_profit_multiple={tpm} (valid 1.5–5.0)")
    else:
        check(FAIL, f"strategy_config.json: take_profit_multiple={tpm} out of range (1.5–5.0)")

    # vix_threshold_caution
    vtc = cfg.get("parameters", {}).get("vix_threshold_caution")
    if vtc is None:
        check(FAIL, "strategy_config.json: vix_threshold_caution missing")
    elif 15 <= int(vtc) <= 35:
        check(PASS, f"strategy_config.json: vix_threshold_caution={vtc} (valid 15–35)")
    else:
        check(FAIL, f"strategy_config.json: vix_threshold_caution={vtc} out of range (15–35)")

    # time_bound_actions
    now_naive = datetime.now()
    for tba in cfg.get("time_bound_actions", []):
        sym          = tba.get("symbol", "?")
        deadline_str = tba.get("deadline_et", "")
        # Warn if Agent 5 wrote exit_by but forgot deadline_et (schema mismatch)
        if tba.get("exit_by") and not deadline_str:
            check(WARN, (f"strategy_config.json: time_bound_action {sym} has exit_by but "
                         f"no deadline_et — reconciliation reads deadline_et; add it"))
        # Warn on empty deadline_et string even when no exit_by is present
        if deadline_str == "" and not tba.get("exit_by"):
            check(WARN, f"strategy_config.json: time_bound_action {sym} has empty deadline_et with no exit_by — entry is unactionable")
        try:
            deadline = datetime.strptime(deadline_str, "%Y-%m-%d %H:%M")
            if deadline < now_naive:
                check(WARN, f"strategy_config.json: time_bound_action {sym} deadline PAST ({deadline_str}) — remove or update")
            else:
                check(PASS, f"strategy_config.json: time_bound_action {sym} deadline valid ({deadline_str})")
        except Exception:
            if deadline_str:
                check(FAIL, f"strategy_config.json: time_bound_action {sym} invalid deadline: {deadline_str!r}")

    # director_notes age
    notes = cfg.get("director_notes", "")
    m = re.search(r"UPDATED (\d{4}-\d{2}-\d{2})", notes)
    if m:
        try:
            updated  = datetime.strptime(m.group(1), "%Y-%m-%d")
            age_days = (datetime.now() - updated).days
            if age_days > 14:
                check(WARN, f"strategy_config.json: director_notes last updated {age_days} days ago ({m.group(1)}) — consider refreshing")
            else:
                check(PASS, f"strategy_config.json: director_notes updated {age_days} day(s) ago ({m.group(1)})")
        except Exception:
            check(WARN, "strategy_config.json: UPDATED date in director_notes could not be parsed")
    else:
        check(WARN, "strategy_config.json: director_notes has no 'UPDATED YYYY-MM-DD' date")

    # account2 checks
    a2 = cfg.get("account2", {})
    eq_floor = a2.get("equity_floor")
    if eq_floor is None:
        check(FAIL, "strategy_config.json: account2.equity_floor missing")
    elif float(eq_floor) >= 25000:
        check(PASS, f"strategy_config.json: account2.equity_floor=${float(eq_floor):,.0f} (≥$25,000)")
    else:
        check(FAIL, f"strategy_config.json: account2.equity_floor=${eq_floor} below $25,000")

    dcf = a2.get("debate_confidence_floor")
    if dcf is None:
        check(FAIL, "strategy_config.json: account2.debate_confidence_floor missing")
    elif 0.70 <= float(dcf) <= 0.95:
        check(PASS, f"strategy_config.json: account2.debate_confidence_floor={dcf} (valid 0.70–0.95)")
    else:
        check(FAIL, f"strategy_config.json: account2.debate_confidence_floor={dcf} out of range (0.70–0.95)")


# ─────────────────────────────────────────────────────────────────────────────
# Cross-file: prompts/system_v1.txt
# ─────────────────────────────────────────────────────────────────────────────
sys_path = BASE_DIR / "prompts" / "system_v1.txt"
try:
    sys_text = sys_path.read_text()

    # No $30,000 cap
    if "$30,000" in sys_text:
        check(FAIL, "system_v1.txt: still contains '$30,000' hard cap — should be percentage-based")
    else:
        check(PASS, "system_v1.txt: no '$30,000' hardcoded cap found")

    # Scan RISK RULES section for unexpected dollar amounts
    risk_m = re.search(r"RISK RULES.*?(?=\n[A-Z][A-Z])", sys_text, re.S)
    if risk_m:
        risk_text = risk_m.group(0)
        # $5,000 options max and $26,000 PDT floor are expected
        unexpected = [
            d for d in re.findall(r'\$[\d,]+', risk_text)
            if d not in ("$5,000", "$26,000")
        ]
        if unexpected:
            check(WARN, f"system_v1.txt: unexpected dollar amounts in RISK RULES: {unexpected} — verify these should not be percentages")
        else:
            check(PASS, "system_v1.txt: RISK RULES contains only expected dollar amounts ($5,000 options, $26,000 PDT)")

    # VIX threshold cross-check
    if cfg:
        vtc = cfg.get("parameters", {}).get("vix_threshold_caution")
        if vtc is not None:
            if str(int(vtc)) in sys_text:
                check(PASS, f"system_v1.txt: vix_threshold_caution={vtc} from strategy_config appears in system prompt")
            else:
                check(WARN, (f"system_v1.txt: vix_threshold_caution={vtc} from strategy_config not "
                             f"found verbatim in system prompt VIX rules — verify thresholds are aligned"))

except FileNotFoundError:
    check(FAIL, "prompts/system_v1.txt: file not found")


# ─────────────────────────────────────────────────────────────────────────────
# Cross-file: order_executor.py
# ─────────────────────────────────────────────────────────────────────────────
oe_path = BASE_DIR / "order_executor.py"
try:
    oe_text = oe_path.read_text()

    for const in ("MARGIN_HIGH_CONVICTION", "MARGIN_MEDIUM_CONVICTION", "MARGIN_LOW_CONVICTION"):
        if re.search(rf"^{const}\s*=", oe_text, re.M):
            check(PASS, f"order_executor.py: {const} constant present")
        else:
            check(FAIL, f"order_executor.py: {const} constant MISSING — margin tiers not configured")

    m = re.search(r"^PDT_FLOOR\s*=\s*([\d_]+)", oe_text, re.M)
    if m:
        pdt = int(m.group(1).replace("_", ""))
        if pdt >= 25000:
            check(PASS, f"order_executor.py: PDT_FLOOR={pdt:,} (≥$25,000)")
        else:
            check(FAIL, f"order_executor.py: PDT_FLOOR={pdt:,} is below $25,000")
    else:
        check(FAIL, "order_executor.py: PDT_FLOOR constant not found")

    if re.search(r"^MAX_TOTAL_EXPOSURE\s*=\s*[\d_]+", oe_text, re.M):
        check(FAIL, "order_executor.py: legacy MAX_TOTAL_EXPOSURE dollar constant still present — should be replaced by MARGIN_* pct constants")
    else:
        check(PASS, "order_executor.py: no legacy MAX_TOTAL_EXPOSURE dollar constant (correctly replaced)")

except FileNotFoundError:
    check(FAIL, "order_executor.py: file not found")


# ─────────────────────────────────────────────────────────────────────────────
# Cross-file: prompts/user_template_v1.txt
# ─────────────────────────────────────────────────────────────────────────────
ut_path = BASE_DIR / "prompts" / "user_template_v1.txt"
try:
    ut_text = ut_path.read_text()
    if "$30,000" in ut_text:
        check(FAIL, "prompts/user_template_v1.txt: still contains '$30,000' hard cap")
    else:
        check(PASS, "prompts/user_template_v1.txt: no '$30,000' hardcoded cap found")
except FileNotFoundError:
    check(FAIL, "prompts/user_template_v1.txt: file not found")


# ─────────────────────────────────────────────────────────────────────────────
# Environment checks
# ─────────────────────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass  # dotenv may not be in path when running standalone

REQUIRED_VARS = [
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL",
    "ALPACA_API_KEY_OPTIONS", "ALPACA_SECRET_KEY_OPTIONS",
    "ANTHROPIC_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
    "SENDGRID_API_KEY",
]
OPTIONAL_VARS = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET"]

for var in REQUIRED_VARS:
    if os.getenv(var):
        check(PASS, f".env: {var} present")
    else:
        check(FAIL, f".env: {var} missing or empty — required for bot operation")

for var in OPTIONAL_VARS:
    if os.getenv(var):
        check(PASS, f".env: {var} present (optional)")
    else:
        check(WARN, f".env: {var} missing — Reddit sentiment feed will be disabled")


# ─────────────────────────────────────────────────────────────────────────────
# Data file checks
# ─────────────────────────────────────────────────────────────────────────────
today    = datetime.now().strftime("%Y-%m-%d")
now_ts   = datetime.now(timezone.utc)

# morning_brief.json — exists and updated today
brief_path = BASE_DIR / "data" / "market" / "morning_brief.json"
if not brief_path.exists():
    check(FAIL, "data/market/morning_brief.json: file not found — pre-market scan may not have run")
else:
    try:
        brief  = json.loads(brief_path.read_text())
        gen_at = brief.get("generated_at", "")
        if today in gen_at:
            check(PASS, f"data/market/morning_brief.json: updated today ({gen_at[:16]})")
        else:
            check(WARN, f"data/market/morning_brief.json: last updated {gen_at[:10] or 'unknown'} (not today)")
    except Exception as e:
        check(FAIL, f"data/market/morning_brief.json: cannot parse — {e}")

# signal_scores.json — freshness warning only
ss_path = BASE_DIR / "data" / "market" / "signal_scores.json"
if not ss_path.exists():
    check(WARN, "data/market/signal_scores.json: file not found (normal if market closed)")
else:
    age_min = (now_ts.timestamp() - ss_path.stat().st_mtime) / 60
    if age_min <= 30:
        check(PASS, f"data/market/signal_scores.json: fresh ({age_min:.0f} min old)")
    else:
        check(WARN, f"data/market/signal_scores.json: stale ({age_min:.0f} min old — normal if market closed)")

# obs_mode_state.json
obs_path = BASE_DIR / "data" / "account2" / "obs_mode_state.json"
if obs_path.exists():
    check(PASS, "data/account2/obs_mode_state.json: exists")
else:
    check(FAIL, "data/account2/obs_mode_state.json: file not found — account2 observation mode state missing")

# memory/decisions.json
dec_path = BASE_DIR / "memory" / "decisions.json"
if not dec_path.exists():
    check(FAIL, "memory/decisions.json: file not found")
else:
    try:
        decisions = json.loads(dec_path.read_text())
        if len(decisions) >= 1:
            check(PASS, f"memory/decisions.json: exists with {len(decisions)} entries")
        else:
            check(FAIL, "memory/decisions.json: exists but is empty (0 entries)")
    except Exception as e:
        check(FAIL, f"memory/decisions.json: cannot parse — {e}")

# memory/performance.json
perf_path = BASE_DIR / "memory" / "performance.json"
if perf_path.exists():
    check(PASS, "memory/performance.json: exists")
else:
    check(FAIL, "memory/performance.json: file not found — performance tracking data missing")


# ─────────────────────────────────────────────────────────────────────────────
# C5: Overnight gate checks
# ─────────────────────────────────────────────────────────────────────────────

# Verify scratchpad session gate: scratchpad should not run overnight.
# Checks strategy_config.json scratchpad.session_gates if present.
if cfg:
    scratchpad_cfg = cfg.get("scratchpad", {})
    gates = scratchpad_cfg.get("session_gates")
    if gates is not None:
        if "market" in gates and "overnight" not in gates:
            check(PASS, "strategy_config.json: scratchpad.session_gates contains 'market' and not 'overnight'")
        elif "overnight" in gates:
            check(FAIL, "strategy_config.json: scratchpad.session_gates contains 'overnight' — scratchpad must not run overnight")
        else:
            check(WARN, "strategy_config.json: scratchpad.session_gates does not contain 'market' — verify session gate config")
    else:
        check(WARN, "strategy_config.json: scratchpad.session_gates not set — session gate enforced in bot.py code only")

# Verify _ask_claude_overnight function exists in bot.py
bot_path = BASE_DIR / "bot.py"
if bot_path.exists():
    bot_text = bot_path.read_text()
    if "def _ask_claude_overnight(" in bot_text:
        check(PASS, "bot.py: _ask_claude_overnight function present (C3 overnight gate)")
    else:
        check(FAIL, "bot.py: _ask_claude_overnight function not found — C3 overnight gate missing")
    if "session_tier == \"overnight\"" in bot_text and "_ask_claude_overnight(" in bot_text:
        check(PASS, "bot.py: overnight gate wired into run_cycle()")
    else:
        check(FAIL, "bot.py: overnight gate not wired — ask_claude() called for all sessions")
else:
    check(FAIL, "bot.py: file not found")


# ─────────────────────────────────────────────────────────────────────────────
# sonnet_gate config
# ─────────────────────────────────────────────────────────────────────────────
if cfg:
    sg = cfg.get("sonnet_gate", {})
    if not sg:
        check(FAIL, "strategy_config.json: sonnet_gate block missing")
    else:
        cm = sg.get("cooldown_minutes")
        if cm is None:
            check(FAIL, "strategy_config.json: sonnet_gate.cooldown_minutes missing")
        elif 5 <= float(cm) <= 60:
            check(PASS, f"strategy_config.json: sonnet_gate.cooldown_minutes={cm} (valid 5–60)")
        else:
            check(FAIL, f"strategy_config.json: sonnet_gate.cooldown_minutes={cm} out of range (5–60)")

        mcs = sg.get("max_consecutive_skips")
        if mcs is None:
            check(FAIL, "strategy_config.json: sonnet_gate.max_consecutive_skips missing")
        elif 3 <= int(mcs) <= 30:
            check(PASS, f"strategy_config.json: sonnet_gate.max_consecutive_skips={mcs} (valid 3–30)")
        else:
            check(FAIL, f"strategy_config.json: sonnet_gate.max_consecutive_skips={mcs} out of range (3–30)")

        sst = sg.get("signal_score_threshold")
        if sst is None:
            check(FAIL, "strategy_config.json: sonnet_gate.signal_score_threshold missing")
        elif 5 <= float(sst) <= 40:
            check(PASS, f"strategy_config.json: sonnet_gate.signal_score_threshold={sst} (valid 5–40)")
        else:
            check(FAIL, f"strategy_config.json: sonnet_gate.signal_score_threshold={sst} out of range (5–40)")

        dwm = sg.get("deadline_warning_minutes")
        if dwm is None:
            check(FAIL, "strategy_config.json: sonnet_gate.deadline_warning_minutes missing")
        elif 15 <= float(dwm) <= 90:
            check(PASS, f"strategy_config.json: sonnet_gate.deadline_warning_minutes={dwm} (valid 15–90)")
        else:
            check(FAIL, f"strategy_config.json: sonnet_gate.deadline_warning_minutes={dwm} out of range (15–90)")

    # Verify bot.py has gate wiring
    if bot_path.exists():
        bot_text = bot_path.read_text()
        if "import sonnet_gate as _gate" in bot_text:
            check(PASS, "bot.py: sonnet_gate imported as _gate")
        else:
            check(FAIL, "bot.py: sonnet_gate import missing — gate not wired")
        if "_gate.should_run_sonnet(" in bot_text:
            check(PASS, "bot.py: _gate.should_run_sonnet() call present")
        else:
            check(FAIL, "bot.py: _gate.should_run_sonnet() call missing")
        if "_log_skip_cycle(" in bot_text:
            check(PASS, "bot.py: _log_skip_cycle() helper present")
        else:
            check(FAIL, "bot.py: _log_skip_cycle() helper missing")


# ─────────────────────────────────────────────────────────────────────────────
# Git repo check
# ─────────────────────────────────────────────────────────────────────────────
git_dir = BASE_DIR / ".git"
if git_dir.exists():
    check(PASS, "git: .git directory present — repo initialised")
else:
    check(WARN, "git: no .git directory — run 'git init' to initialise repo")

# ─────────────────────────────────────────────────────────────────────────────
# attribution.py importable
# ─────────────────────────────────────────────────────────────────────────────
attr_path = BASE_DIR / "attribution.py"
if not attr_path.exists():
    check(FAIL, "attribution.py: file missing — attribution system unavailable")
else:
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("attribution", attr_path)
        mod  = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Verify the four public symbols are present
        missing = [
            fn for fn in (
                "generate_decision_id", "build_module_tags",
                "build_trigger_flags",  "log_attribution_event",
                "get_attribution_summary",
            )
            if not hasattr(mod, fn)
        ]
        if missing:
            check(FAIL, f"attribution.py: missing symbols: {missing}")
        else:
            check(PASS, "attribution.py: importable, all public symbols present")
    except Exception as _attr_err:
        check(FAIL, f"attribution.py: import failed — {_attr_err}")

# ─────────────────────────────────────────────────────────────────────────────
# data/analytics/ directory
# ─────────────────────────────────────────────────────────────────────────────
analytics_dir = BASE_DIR / "data" / "analytics"
if analytics_dir.exists():
    check(PASS, "data/analytics/: directory present")
else:
    check(WARN, "data/analytics/: directory missing — will be auto-created on first attribution event")

# ─────────────────────────────────────────────────────────────────────────────
# data/runtime/ directory (divergence mode state files)
# ─────────────────────────────────────────────────────────────────────────────
runtime_dir = BASE_DIR / "data" / "runtime"
if runtime_dir.exists():
    check(PASS, "data/runtime/: directory present")
else:
    check(WARN, "data/runtime/: directory missing — will be auto-created by divergence.py on first mode write")

# ─────────────────────────────────────────────────────────────────────────────
# divergence.py importable
# ─────────────────────────────────────────────────────────────────────────────
div_path = BASE_DIR / "divergence.py"
if not div_path.exists():
    check(FAIL, "divergence.py: file missing — divergence tracking unavailable")
else:
    try:
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location("divergence_vc", div_path)
        _mod  = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _missing_div = [
            fn for fn in (
                "load_account_mode", "save_account_mode",
                "transition_mode", "is_action_allowed",
                "detect_protection_divergence", "detect_fill_divergence",
                "respond_to_divergence", "check_clean_cycle",
                "get_divergence_summary",
            )
            if not hasattr(_mod, fn)
        ]
        if _missing_div:
            check(FAIL, f"divergence.py: missing symbols: {_missing_div}")
        else:
            check(PASS, "divergence.py: importable, all public symbols present")
    except Exception as _div_err:
        check(FAIL, f"divergence.py: import failed — {_div_err}")

# ─────────────────────────────────────────────────────────────────────────────
# account2.liquidity_gates section
# ─────────────────────────────────────────────────────────────────────────────
if cfg:
    _liq_gates = cfg.get("account2", {}).get("liquidity_gates")
    if _liq_gates is None:
        check(FAIL, "strategy_config.json: account2.liquidity_gates section missing")
    else:
        _required_liq = [
            "min_open_interest", "min_volume", "max_spread_pct",
            "min_mid_price", "pre_debate_oi_floor", "pre_debate_volume_floor",
        ]
        _missing_liq = [k for k in _required_liq if k not in _liq_gates]
        if _missing_liq:
            check(FAIL, f"strategy_config.json: account2.liquidity_gates missing keys: {_missing_liq}")
        else:
            check(PASS, "strategy_config.json: account2.liquidity_gates present with all required keys")


# ─────────────────────────────────────────────────────────────────────────────
# reddit_sentiment_public.py importable (Phase 3 — public JSON fallback)
# ─────────────────────────────────────────────────────────────────────────────
rsp_path = BASE_DIR / "reddit_sentiment_public.py"
if not rsp_path.exists():
    check(FAIL, "reddit_sentiment_public.py: file missing — public Reddit fallback unavailable")
else:
    try:
        import importlib.util as _ilu2
        _spec2 = _ilu2.spec_from_file_location("reddit_sentiment_public_vc", rsp_path)
        _mod2  = _ilu2.module_from_spec(_spec2)
        _spec2.loader.exec_module(_mod2)
        if hasattr(_mod2, "RedditPublicProvider"):
            check(PASS, "reddit_sentiment_public.py: importable, RedditPublicProvider present")
        else:
            check(FAIL, "reddit_sentiment_public.py: RedditPublicProvider class missing")
    except Exception as _rsp_err:
        check(FAIL, f"reddit_sentiment_public.py: import failed — {_rsp_err}")

# ─────────────────────────────────────────────────────────────────────────────
# account2.iv_monitoring section (Phase 3 — IV crush detection)
# ─────────────────────────────────────────────────────────────────────────────
if cfg:
    _iv_mon = cfg.get("account2", {}).get("iv_monitoring")
    if _iv_mon is None:
        check(FAIL, "strategy_config.json: account2.iv_monitoring section missing")
    else:
        _required_iv = ["enabled", "auto_close_on_crush", "crush_threshold"]
        _missing_iv  = [k for k in _required_iv if k not in _iv_mon]
        if _missing_iv:
            check(FAIL, f"strategy_config.json: account2.iv_monitoring missing keys: {_missing_iv}")
        else:
            _crush = _iv_mon.get("crush_threshold", 0)
            if 0.10 <= float(_crush) <= 0.60:
                check(PASS, f"strategy_config.json: account2.iv_monitoring present, crush_threshold={_crush} (valid 0.10–0.60)")
            else:
                check(WARN, f"strategy_config.json: account2.iv_monitoring.crush_threshold={_crush} unusual (expected 0.10–0.60)")


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
passes   = sum(1 for s, _ in results if s == PASS)
warnings = sum(1 for s, _ in results if s == WARN)
failures = sum(1 for s, _ in results if s == FAIL)

print()
print(f"{passes} checks passed, {warnings} warnings, {failures} failures")

sys.exit(1 if failures else 0)
