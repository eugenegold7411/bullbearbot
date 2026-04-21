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

    # schema version
    cfg_version = cfg.get("version")
    if cfg_version == 2:
        check(PASS, "strategy_config.json: version=2 (Phase 6 schema)")
    elif cfg_version == 1:
        check(WARN, "strategy_config.json: version=1 — Phase 6 migration pending")
    else:
        check(WARN, f"strategy_config.json: version={cfg_version!r} unexpected")

    # Duplicate keys — must not appear in parameters (canonical location shown)
    _DUP_CHECKS = [
        ("core_tier_pct",          "position_sizing"),
        ("dynamic_tier_pct",       "position_sizing"),
        ("intraday_tier_pct",      "position_sizing"),
        ("max_total_exposure_pct", "position_sizing"),
        ("cash_reserve_pct",       "position_sizing"),
        ("momentum_weight",        "signal_weights"),
        ("mean_reversion_weight",  "signal_weights"),
        ("news_sentiment_weight",  "signal_weights"),
        ("cross_sector_weight",    "signal_weights"),
    ]
    _params_block = cfg.get("parameters", {})
    _dup_present = [k for k, _ in _DUP_CHECKS if k in _params_block]
    if _dup_present:
        check(FAIL, ("strategy_config.json: duplicate keys in parameters "
                     "(canonical location in parentheses): "
                     + ", ".join(f"{k} ({next(c for kk,c in _DUP_CHECKS if kk==k)})" for k in _dup_present)))
    else:
        check(PASS, "strategy_config.json: no duplicate keys in parameters block")

    # _DEPRECATED string-marker fields
    _deprecated_present = [k for k in _params_block if k.endswith("_DEPRECATED")]
    if _deprecated_present:
        check(FAIL, f"strategy_config.json: _DEPRECATED marker field(s) in parameters: {_deprecated_present}")
    else:
        check(PASS, "strategy_config.json: no _DEPRECATED marker fields in parameters")

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

    # T-014: gross exposure consistency — max_positions × max_position_pct_equity must not exceed 100%
    _mp  = cfg.get("parameters", {}).get("max_positions")
    _mpe = cfg.get("parameters", {}).get("max_position_pct_equity")
    if _mp is not None and _mpe is not None:
        _gross = int(_mp) * float(_mpe)
        if _gross > 1.0:
            check(FAIL, (f"strategy_config.json: T-014 gross exposure inconsistency — "
                         f"max_positions ({_mp}) × max_position_pct_equity ({_mpe}) "
                         f"= {_gross:.0%}, which implies potential over-allocation (must be ≤ 100%)"))
        else:
            check(PASS, (f"strategy_config.json: T-014 gross exposure OK — "
                         f"max_positions ({_mp}) × max_position_pct_equity ({_mpe}) "
                         f"= {_gross:.0%} (≤ 100%)"))

    # T-016: PDT day-trade limit gate (regulatory ceiling for any account; 3 is the PDT threshold)
    # ENFORCEMENT DISCREPANCY: max_day_trades_rolling_5day from config is NOT enforced in code.
    # bot_stage3_decision.py hardcodes `pdt_remaining = max(0, 3 - pdt_used)` and never reads
    # this config key. This caused 3 day trades to execute when the config said 2 — the config
    # value was informational-only. Enforcement must be added to bot_stage3_decision.py
    # (compute pdt_remaining using config value) and risk_kernel.py/order_executor.py.
    _mdt = cfg.get("parameters", {}).get("max_day_trades_rolling_5day")
    if _mdt is not None:
        if int(_mdt) <= 3:
            check(PASS, (f"strategy_config.json: T-016 max_day_trades_rolling_5day={_mdt} "
                         f"(≤ 3 regulatory ceiling)"))
        else:
            check(FAIL, (f"strategy_config.json: T-016 max_day_trades_rolling_5day={_mdt} "
                         f"exceeds 3 — regulatory ceiling for standard PDT accounts"))
    else:
        check(FAIL, "strategy_config.json: T-016 max_day_trades_rolling_5day missing")

    # T-017: sector_rotation_bias_expiry — warn if expiry date is in the past
    _bias_expiry_str = cfg.get("parameters", {}).get("sector_rotation_bias_expiry")
    if _bias_expiry_str:
        try:
            _bias_expiry = datetime.fromisoformat(_bias_expiry_str).date()
            _today_date  = datetime.now().date()
            if _today_date > _bias_expiry:
                _days_past = (_today_date - _bias_expiry).days
                check(WARN, (f"strategy_config.json: T-017 sector_rotation_bias_expiry "
                             f"{_bias_expiry_str} has passed ({_days_past} day(s) ago) — "
                             f"bias will auto-revert to neutral at runtime; "
                             f"Strategy Director should formally reset"))
            else:
                _days_left = (_bias_expiry - _today_date).days
                check(PASS, (f"strategy_config.json: T-017 sector_rotation_bias_expiry "
                             f"{_bias_expiry_str} still active ({_days_left} day(s) remaining)"))
        except Exception as _be:
            check(WARN, f"strategy_config.json: T-017 sector_rotation_bias_expiry could not be parsed — {_be}")

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

    # director_notes check (dict format since Phase 6)
    notes = cfg.get("director_notes", {})
    if isinstance(notes, dict):
        _expiry_str = notes.get("expiry", "")
        if _expiry_str:
            try:
                _expiry  = datetime.strptime(_expiry_str, "%Y-%m-%d")
                age_days = (datetime.now() - _expiry).days
                if age_days > 0:
                    check(WARN, f"strategy_config.json: director_notes expired {age_days} day(s) ago ({_expiry_str}) — Agent 6 should refresh")
                else:
                    check(PASS, f"strategy_config.json: director_notes expires {_expiry_str} ({-age_days} day(s) away)")
            except Exception:
                check(WARN, "strategy_config.json: director_notes.expiry could not be parsed")
        else:
            check(WARN, "strategy_config.json: director_notes.expiry not set")
        if not notes.get("active_context"):
            check(WARN, "strategy_config.json: director_notes.active_context is empty")
        else:
            check(PASS, "strategy_config.json: director_notes has active_context")
    elif isinstance(notes, str) and notes:
        check(WARN, "strategy_config.json: director_notes is a plain string — Agent 6 should emit dict {active_context, expiry, priority}")
    else:
        check(WARN, "strategy_config.json: director_notes missing or empty")

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
    try:
        _obs_state = json.loads(obs_path.read_text())
        _obs_ver = _obs_state.get("version", 1)
        if _obs_ver >= 2:
            check(PASS, f"data/account2/obs_mode_state.json: version={_obs_ver} (Phase B migration complete)")
        else:
            check(WARN, f"data/account2/obs_mode_state.json: version={_obs_ver} — run migration script to upgrade to v2")
    except Exception as _e:
        check(WARN, f"data/account2/obs_mode_state.json: cannot parse for version check — {_e}")
else:
    check(FAIL, "data/account2/obs_mode_state.json: file not found — account2 observation mode state missing")

# decision_outcomes.py importable
try:
    import decision_outcomes as _do_check  # noqa: F401
    check(PASS, "decision_outcomes.py: importable")
except ImportError as _e:
    check(WARN, f"decision_outcomes.py: import failed — {_e}")

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
    if "_ask_claude_overnight" in bot_text:
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
# a2_router section (S3-C)
# ─────────────────────────────────────────────────────────────────────────────
if cfg:
    _a2r = cfg.get("a2_router")
    if _a2r is None:
        check(FAIL, "strategy_config.json: a2_router section missing")
    else:
        _edb = _a2r.get("earnings_dte_blackout")
        if _edb is None:
            check(FAIL, "strategy_config.json: a2_router.earnings_dte_blackout missing")
        elif 0 <= int(_edb) <= 21:
            check(PASS, f"strategy_config.json: a2_router.earnings_dte_blackout={_edb} (valid 0–21)")
        else:
            check(FAIL, f"strategy_config.json: a2_router.earnings_dte_blackout={_edb} out of range (0–21)")

        _mls = _a2r.get("min_liquidity_score")
        if _mls is None:
            check(FAIL, "strategy_config.json: a2_router.min_liquidity_score missing")
        elif 0.0 <= float(_mls) <= 1.0:
            check(PASS, f"strategy_config.json: a2_router.min_liquidity_score={_mls} (valid 0.0–1.0)")
        else:
            check(FAIL, f"strategy_config.json: a2_router.min_liquidity_score={_mls} out of range (0.0–1.0)")

        _migr = _a2r.get("macro_iv_gate_rank")
        if _migr is None:
            check(FAIL, "strategy_config.json: a2_router.macro_iv_gate_rank missing")
        elif 0 <= float(_migr) <= 100:
            check(PASS, f"strategy_config.json: a2_router.macro_iv_gate_rank={_migr} (valid 0–100)")
        else:
            check(FAIL, f"strategy_config.json: a2_router.macro_iv_gate_rank={_migr} out of range (0–100)")

        _ieb = _a2r.get("iv_env_blackout")
        if _ieb is None:
            check(FAIL, "strategy_config.json: a2_router.iv_env_blackout missing")
        elif isinstance(_ieb, list) and len(_ieb) >= 1:
            check(PASS, f"strategy_config.json: a2_router.iv_env_blackout={_ieb} (non-empty list)")
        else:
            check(FAIL, f"strategy_config.json: a2_router.iv_env_blackout={_ieb!r} must be a non-empty list")


# ─────────────────────────────────────────────────────────────────────────────
# a2_veto_thresholds section (S4-A) — calibrated veto gate values
# ─────────────────────────────────────────────────────────────────────────────
if cfg:
    _a2vt = cfg.get("a2_veto_thresholds")
    if _a2vt is None:
        check(FAIL, "strategy_config.json: a2_veto_thresholds section missing")
    else:
        _vt_required = [
            "max_bid_ask_spread_pct", "min_open_interest",
            "max_theta_decay_pct", "min_dte", "min_expected_value",
        ]
        _vt_missing = [k for k in _vt_required if k not in _a2vt]
        if _vt_missing:
            check(FAIL, f"strategy_config.json: a2_veto_thresholds missing keys: {_vt_missing}")
        else:
            _vt_spread = float(_a2vt["max_bid_ask_spread_pct"])
            _vt_oi     = int(_a2vt["min_open_interest"])
            _vt_theta  = float(_a2vt["max_theta_decay_pct"])
            _vt_dte    = int(_a2vt["min_dte"])

            _vt_errors = []
            if not (0.01 <= _vt_spread <= 0.50):
                _vt_errors.append(f"max_bid_ask_spread_pct={_vt_spread} out of range (0.01–0.50)")
            if not (10 <= _vt_oi <= 10000):
                _vt_errors.append(f"min_open_interest={_vt_oi} out of range (10–10000)")
            if not (0.001 <= _vt_theta <= 0.50):
                _vt_errors.append(f"max_theta_decay_pct={_vt_theta} out of range (0.001–0.50)")
            if not (1 <= _vt_dte <= 30):
                _vt_errors.append(f"min_dte={_vt_dte} out of range (1–30)")

            if _vt_errors:
                for _ve in _vt_errors:
                    check(FAIL, f"strategy_config.json: a2_veto_thresholds.{_ve}")
            else:
                check(PASS, (
                    f"strategy_config.json: a2_veto_thresholds valid "
                    f"(spread≤{_vt_spread:.2f} oi≥{_vt_oi} theta≤{_vt_theta:.3f} dte≥{_vt_dte})"
                ))


# ─────────────────────────────────────────────────────────────────────────────
# a2_rollback section (S3-C) — emergency flags, must all default false
# ─────────────────────────────────────────────────────────────────────────────
if cfg:
    _a2rb = cfg.get("a2_rollback")
    if _a2rb is None:
        check(FAIL, "strategy_config.json: a2_rollback section missing")
    else:
        _rb_fields = ["disable_candidate_generation", "disable_bounded_debate", "force_no_trade"]
        _rb_missing = [f for f in _rb_fields if f not in _a2rb]
        if _rb_missing:
            check(FAIL, f"strategy_config.json: a2_rollback missing keys: {_rb_missing}")
        else:
            _rb_active = [f for f in _rb_fields if _a2rb.get(f)]
            if _rb_active:
                check(WARN, (f"strategy_config.json: a2_rollback flags ACTIVE: {_rb_active} "
                             f"— these are emergency switches; disable before normal operation"))
            else:
                check(PASS, "strategy_config.json: a2_rollback all flags false (normal operation)")


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
# Sev-1 clean days counter (consecutive days with zero CRITICAL/HALT in bot.log)
# State: data/runtime/sev1_clean_days.json
# Keywords are tightened to avoid false positives from inline log-message text:
#   "  CRITICAL  " matches the log-level field (padded), NOT "0 CRITICAL, 0 HIGH"
#   "[HALT]" matches an explicit halt marker, NOT "halt mode" VIX rejection text
# ─────────────────────────────────────────────────────────────────────────────
_BOT_LOG_PATH    = BASE_DIR / "logs" / "bot.log"
_SEV1_STATE_FILE = BASE_DIR / "data" / "runtime" / "sev1_clean_days.json"
# Tightened keywords — positional CRITICAL (log-level field) + explicit halt markers only
_SEV1_KEYWORDS   = ("  CRITICAL  ", "[HALT]", "regime=halt", "mode=halted", "DRAWDOWN GUARD")

def _count_sev1_today(log_path: Path) -> int:
    """Count lines containing a Sev-1 keyword in bot.log for today (UTC date)."""
    if not log_path.exists():
        return 0
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    count = 0
    try:
        with log_path.open(errors="replace") as _f:
            for _line in _f:
                if today in _line and any(kw in _line for kw in _SEV1_KEYWORDS):
                    count += 1
    except Exception:
        pass
    return count

_sev1_today      = _count_sev1_today(_BOT_LOG_PATH)
_today_str_sev1  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
try:
    _sev1_state = json.loads(_SEV1_STATE_FILE.read_text()) if _SEV1_STATE_FILE.exists() else {}
except Exception:
    _sev1_state = {}

_last_check_date = _sev1_state.get("last_check_date", "")
_clean_days      = int(_sev1_state.get("consecutive_clean_days", 0))
_sev1_history    = _sev1_state.get("history", [])

if _last_check_date != _today_str_sev1:
    # New day — update state
    if _sev1_today == 0:
        _clean_days += 1
    else:
        _clean_days = 0
    _sev1_history.append({"date": _today_str_sev1, "sev1_count": _sev1_today})
    _new_sev1_state = {
        "last_check_date":        _today_str_sev1,
        "consecutive_clean_days": _clean_days,
        "history":                _sev1_history[-30:],
    }
    try:
        _SEV1_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SEV1_STATE_FILE.write_text(json.dumps(_new_sev1_state, indent=2))
    except Exception:
        pass

if _sev1_today > 0:
    check(WARN, f"Sev-1 clean days: {_clean_days} (TODAY has {_sev1_today} CRITICAL/HALT line(s) — counter reset)")
elif _clean_days >= 7:
    check(PASS, f"Sev-1 clean days: {_clean_days} consecutive (≥7 — go-live gate CLEAR)")
else:
    check(WARN, f"Sev-1 clean days: {_clean_days} consecutive (need 7 for go-live gate)")


# ─────────────────────────────────────────────────────────────────────────────
# Director memo history (weekly_review.py rolling continuity — Phase 4)
# ─────────────────────────────────────────────────────────────────────────────
_memo_hist_file = BASE_DIR / "data" / "reports" / "director_memo_history.json"
if not _memo_hist_file.exists():
    check(WARN, "data/reports/director_memo_history.json: not yet created — OK for first week")
else:
    try:
        _memo_history = json.loads(_memo_hist_file.read_text())
        if isinstance(_memo_history, list) and len(_memo_history) >= 1:
            check(PASS, f"director_memo_history.json: present, {len(_memo_history)} week(s) stored")
        else:
            check(WARN, "director_memo_history.json: exists but contains no entries")
    except Exception as _mh_err:
        check(FAIL, f"director_memo_history.json: parse error — {_mh_err}")


# ─────────────────────────────────────────────────────────────────────────────
# Session 1 — executor policy consolidation verification
# ─────────────────────────────────────────────────────────────────────────────
try:
    import inspect as _inspect

    import order_executor as _oe
    _oe_src = _inspect.getsource(_oe)
    if "TIER_MAX_PCT" in _oe_src:
        check(WARN, "order_executor.py still defines TIER_MAX_PCT — "
              "policy consolidation incomplete (Session 1 change may not be deployed)")
    else:
        check(PASS, "order_executor.py: TIER_MAX_PCT removed — "
              "kernel (_TIER_MAX_PCT) is sole authoritative definition")
except Exception as _oe_chk_err:
    check(WARN, f"executor policy consolidation check failed (non-fatal): {_oe_chk_err}")


# ─────────────────────────────────────────────────────────────────────────────
# Phase 4 go-live gate checklist (13 gates — informational only, never blocks)
# ─────────────────────────────────────────────────────────────────────────────
print()
print("─" * 65)
print("  PHASE 4 GO-LIVE GATE CHECKLIST")
print("─" * 65)

_gate_results: list[tuple[bool, str]] = []

def _gate(passed: bool, label: str) -> None:
    _gate_results.append((passed, label))
    _icon = "\u2705" if passed else "\u2b1c"
    print(f"  {_icon}  {label}")

import importlib.util as _ilu_g  # noqa: E402

# Gate 01 — schemas.py present and compiles cleanly
# (exec_module skipped: schemas.py has deep cross-imports that require full venv context)
import py_compile as _pyc_g

try:
    _schemas_path = BASE_DIR / "schemas.py"
    if not _schemas_path.exists():
        raise FileNotFoundError("schemas.py missing")
    _pyc_g.compile(str(_schemas_path), doraise=True)
    _gate(True, "Gate 01 — schemas.py present and compiles cleanly")
except Exception as _ge:
    _gate(False, f"Gate 01 — schemas.py check failed: {_ge}")

# Gate 02 — risk_kernel.py present (heavy Alpaca imports skip compile check)
_gate(
    (BASE_DIR / "risk_kernel.py").exists(),
    "Gate 02 — risk_kernel.py present" if (BASE_DIR / "risk_kernel.py").exists()
    else "Gate 02 — risk_kernel.py MISSING",
)

# Gate 03 — sonnet_gate.py present and compiles cleanly
try:
    _sg_path = BASE_DIR / "sonnet_gate.py"
    if not _sg_path.exists():
        raise FileNotFoundError("sonnet_gate.py missing")
    _pyc_g.compile(str(_sg_path), doraise=True)
    _gate(True, "Gate 03 — sonnet_gate.py present and compiles cleanly")
except Exception as _ge:
    _gate(False, f"Gate 03 — sonnet_gate.py check failed: {_ge}")

# Gate 04 — reconciliation.py present
_gate(
    (BASE_DIR / "reconciliation.py").exists(),
    "Gate 04 — reconciliation.py present" if (BASE_DIR / "reconciliation.py").exists()
    else "Gate 04 — reconciliation.py MISSING",
)

# Gate 05 — divergence.py importable
try:
    _gs = _ilu_g.spec_from_file_location("divergence_g", BASE_DIR / "divergence.py")
    _gm = _ilu_g.module_from_spec(_gs); _gs.loader.exec_module(_gm)
    _gate(True, "Gate 05 — divergence.py importable")
except Exception as _ge:
    _gate(False, f"Gate 05 — divergence.py import failed: {_ge}")

# Gate 06 — attribution.py importable
try:
    _gs = _ilu_g.spec_from_file_location("attribution_g", BASE_DIR / "attribution.py")
    _gm = _ilu_g.module_from_spec(_gs); _gs.loader.exec_module(_gm)
    _gate(True, "Gate 06 — attribution.py importable")
except Exception as _ge:
    _gate(False, f"Gate 06 — attribution.py import failed: {_ge}")

# Gate 07a — signal_backtest.py importable
try:
    _gs = _ilu_g.spec_from_file_location("signal_backtest_g", BASE_DIR / "signal_backtest.py")
    _gm = _ilu_g.module_from_spec(_gs); _gs.loader.exec_module(_gm)
    _gate(True, "Gate 07a — signal_backtest.py importable")
except Exception as _ge:
    _gate(False, f"Gate 07a — signal_backtest.py import failed: {_ge}")

# Gate 07b — backtest_latest.json exists (weekly_review has run at least once)
_bt_latest = BASE_DIR / "data" / "reports" / "backtest_latest.json"
_gate(
    _bt_latest.exists(),
    "Gate 07b — backtest_latest.json present (weekly review run)" if _bt_latest.exists()
    else "Gate 07b — backtest_latest.json MISSING (run weekly_review.py once)",
)

# Gate 08 — git remote configured
try:
    import subprocess as _gsp
    _git_out = _gsp.run(
        ["git", "remote", "-v"], capture_output=True, text=True,
        cwd=str(BASE_DIR), timeout=5,
    )
    _has_remote = bool(_git_out.stdout.strip())
    _gate(
        _has_remote,
        "Gate 08 — git remote configured" if _has_remote else "Gate 08 — git remote NOT configured",
    )
except Exception:
    _gate(False, "Gate 08 — git remote check failed")

# Gate 09 — Sev-1 clean days >= 7 (reuses _clean_days from counter above)
_gate(
    _clean_days >= 7,
    f"Gate 09 — Sev-1 clean days={_clean_days} "
    f"({'≥7 CLEAR' if _clean_days >= 7 else f'need {7 - _clean_days} more day(s)'})",
)

# Gate 10 — attribution_log.jsonl exists
_attr_log = BASE_DIR / "data" / "analytics" / "attribution_log.jsonl"
_gate(
    _attr_log.exists(),
    "Gate 10 — attribution_log.jsonl present" if _attr_log.exists()
    else "Gate 10 — attribution_log.jsonl MISSING (bot has not submitted an order yet)",
)

# Gate 11 — near_miss_log.jsonl exists
_nm_log = BASE_DIR / "data" / "analytics" / "near_miss_log.jsonl"
_gate(
    _nm_log.exists(),
    "Gate 11 — near_miss_log.jsonl present" if _nm_log.exists()
    else "Gate 11 — near_miss_log.jsonl MISSING (shadow lane not yet populated)",
)

# Gate 12 — shadow_lane.py present
_gate(
    (BASE_DIR / "shadow_lane.py").exists(),
    "Gate 12 — shadow_lane.py present" if (BASE_DIR / "shadow_lane.py").exists()
    else "Gate 12 — shadow_lane.py MISSING",
)

# Gate 13 — strategy_config.json has shadow_lane section
_cfg_has_shadow = isinstance(cfg, dict) and "shadow_lane" in cfg
_gate(
    _cfg_has_shadow,
    "Gate 13 — strategy_config.json has shadow_lane section" if _cfg_has_shadow
    else "Gate 13 — strategy_config.json missing shadow_lane section",
)

# Gate 18 — options_universe_manager.py importable and universe.json has ≥1 tradeable symbol
try:
    import importlib.util as _ilu_u
    _oum_path = BASE_DIR / "options_universe_manager.py"
    if not _oum_path.exists():
        raise FileNotFoundError("options_universe_manager.py missing")
    _oum_spec = _ilu_u.spec_from_file_location("options_universe_manager_vc", _oum_path)
    _oum_mod  = _ilu_u.module_from_spec(_oum_spec)
    _oum_spec.loader.exec_module(_oum_mod)
    _universe_path = BASE_DIR / "data" / "options" / "universe.json"
    if _universe_path.exists():
        try:
            _uni_data = json.loads(_universe_path.read_text())
            _tradeable = [
                sym for sym, entry in _uni_data.get("symbols", {}).items()
                if entry.get("bootstrap_complete")
            ]
            if len(_tradeable) >= 1:
                check(PASS, f"options_universe_manager.py: importable; universe.json has {len(_tradeable)} tradeable symbol(s)")
            else:
                check(WARN, "options_universe_manager.py: importable; universe.json exists but has 0 tradeable symbols — run initialize_universe_from_existing_iv_history()")
        except Exception as _uni_err:
            check(WARN, f"universe.json: parse error — {_uni_err}")
    else:
        check(WARN, "universe.json: absent — will be created on first A2 cycle")
except Exception as _oum_err:
    check(FAIL, f"options_universe_manager.py: import failed — {_oum_err}")

# Gate 14 — A2 IV history seeded (full universe from _OBS_IV_SYMBOLS, not hardcoded Phase 1)
try:
    import sys as _sys_vc
    _sys_vc.path.insert(0, str(BASE_DIR))
    from bot_options_stage0_preflight import (
        _OBS_IV_SYMBOLS as _A2_UNIVERSE,  # noqa: E402
    )
except Exception:
    # Fallback: original 16 Phase 1 symbols if preflight module cannot be imported
    _A2_UNIVERSE = [
        "SPY", "QQQ", "NVDA", "AAPL", "MSFT", "AMZN",
        "META", "GOOGL", "TSM", "AMD", "XLE", "GLD",
        "TLT", "IWM", "XLF", "XBI",
    ]
_IV_HIST_DIR    = BASE_DIR / "data" / "options" / "iv_history"
_iv_ready_count = 0
for _sym in _A2_UNIVERSE:
    _hist_path = _IV_HIST_DIR / f"{_sym}_iv_history.json"
    if _hist_path.exists():
        try:
            _hist  = json.loads(_hist_path.read_text())
            _valid = [e for e in _hist if e.get("iv", 0) >= 0.05]
            if len(_valid) >= 20:
                _iv_ready_count += 1
        except Exception:
            pass
_universe_total = len(_A2_UNIVERSE)
_gate(
    _iv_ready_count >= _universe_total,
    f"Gate 14 — A2 IV history seeded: {_iv_ready_count}/{_universe_total} symbols have ≥20 valid entries"
    if _iv_ready_count >= _universe_total
    else f"Gate 14 — A2 IV history seeded: only {_iv_ready_count}/{_universe_total} ready "
         f"(run iv_history_seeder.py for missing symbols)",
)

# Gate 15 — data/analytics/ directory present (decision_outcomes.jsonl auto-creates there)
_outcomes_log = BASE_DIR / "data" / "analytics" / "decision_outcomes.jsonl"
_gate(
    _outcomes_log.parent.exists(),
    "Gate 15 — data/analytics/ dir present (decision_outcomes.jsonl will auto-create)"
    if _outcomes_log.parent.exists()
    else "Gate 15 — data/analytics/ dir MISSING — create it before first run",
)

# Gate 17 — T-005: regime label normalization present in bot_stage1_regime.py
_regime_src = BASE_DIR / "bot_stage1_regime.py"
_has_normalize = (
    _regime_src.exists() and
    "_normalize_regime_labels" in _regime_src.read_text()
)
_gate(
    _has_normalize,
    "Gate 17 — T-005 regime label normalizer present in bot_stage1_regime.py"
    if _has_normalize
    else "Gate 17 — T-005 regime label normalizer MISSING from bot_stage1_regime.py",
)

# Gate 16 — strategy_config.json version=2 (Phase 6 schema hygiene)
_cfg_version = cfg.get("version", 0) if isinstance(cfg, dict) else 0
_gate(
    _cfg_version >= 2,
    f"Gate 16 — strategy_config.json version={_cfg_version} (Phase 6 clean schema)"
    if _cfg_version >= 2
    else f"Gate 16 — strategy_config.json version={_cfg_version} — Phase 6 migration pending",
)

_gates_passed = sum(1 for ok, _ in _gate_results if ok)
print("─" * 65)
print(f"  Go-live gates: {_gates_passed}/18 passing")
print("─" * 65)
print()


# ─────────────────────────────────────────────────────────────────────────────
# Readiness status snapshot (E15)
# ─────────────────────────────────────────────────────────────────────────────
def _write_readiness_status() -> None:
    """Write readiness_status_latest.json for CTO weekly review injection. Non-fatal."""
    try:
        _status = {
            "overall_status":  "ready" if _gates_passed >= 16 else "not_ready",
            "a1_live_ready":   _gates_passed >= 16 and _clean_days >= 7,
            "gates_passed":    _gates_passed,
            "gates_total":     18,
            "sev1_clean_days": _clean_days,
            "failures":        [label for ok, label in _gate_results if not ok],
            "generated_at":    datetime.now(timezone.utc).isoformat(),
        }
        _status_path = BASE_DIR / "data" / "reports" / "readiness_status_latest.json"
        _status_path.parent.mkdir(parents=True, exist_ok=True)
        _status_path.write_text(json.dumps(_status, indent=2))
    except Exception as _rs_err:
        print(f"[WARN] _write_readiness_status failed (non-fatal): {_rs_err}")

_write_readiness_status()


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
passes   = sum(1 for s, _ in results if s == PASS)
warnings = sum(1 for s, _ in results if s == WARN)
failures = sum(1 for s, _ in results if s == FAIL)

print()
print(f"{passes} checks passed, {warnings} warnings, {failures} failures")

sys.exit(1 if failures else 0)
