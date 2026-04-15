"""
weekly_review.py — 5-agent automated weekly performance review.

Runs 5 sequential Claude API calls, each with a focused role:
  1. Quant Analyst      — signal quality, timing, sector/strategy patterns
  2. Risk Manager       — position sizing, drawdown, stop effectiveness
  3. Execution Engineer — fill quality, rejections, API reliability
  4. Backtest Analyst   — live vs expected performance, vector memory divergences
  5. Strategy Director  — synthesizes all 4 reports → strategic memo + JSON params

Output: data/reports/weekly_review_YYYY-MM-DD.md
Side effects: updates strategy_config.json, sends Twilio SMS summary.

Usage:
  python weekly_review.py
"""

import json
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
from dotenv import load_dotenv

import memory as mem
import report as rpt
import trade_memory
from log_setup import get_logger

load_dotenv()

log = get_logger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
_BASE_DIR       = Path(__file__).parent
_BOT_LOG        = _BASE_DIR / "logs" / "bot.log"
_TRADE_LOG      = _BASE_DIR / "logs" / "trades.jsonl"
_DECISIONS_FILE = _BASE_DIR / "memory" / "decisions.json"
_STRATEGY_FILE  = _BASE_DIR / "strategy_config.json"
_REPORTS_DIR         = _BASE_DIR / "data" / "reports"
_ARCHIVE_DIR         = _BASE_DIR / "data" / "archive"
_DIRECTOR_MEMO_FILE  = _BASE_DIR / "data" / "reports" / "director_memo_history.json"

# ── Claude client ─────────────────────────────────────────────────────────────
_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_MODEL  = "claude-sonnet-4-6"


# ── SMS helper ────────────────────────────────────────────────────────────────

def _send_sms(message: str) -> None:
    """Send Twilio SMS using env vars. No-op if not configured."""
    sid   = os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_AUTH_TOKEN")
    from_ = os.getenv("TWILIO_FROM_NUMBER")
    to    = os.getenv("TWILIO_TO_NUMBER")

    if not all([sid, token, from_, to]):
        log.warning("Twilio not configured — SMS skipped: %s", message)
        return

    try:
        from twilio.rest import Client
        Client(sid, token).messages.create(body=message, from_=from_, to=to)
        log.info("SMS sent: %s", message)
    except Exception as exc:
        log.error("SMS failed: %s", exc)


# ── Data helpers ──────────────────────────────────────────────────────────────

def _read_log_tail(n_lines: int = 500) -> str:
    """Read last n_lines from logs/bot.log. Returns empty string on failure."""
    try:
        if not _BOT_LOG.exists():
            return "(bot.log not found)"
        lines = _BOT_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        tail  = lines[-n_lines:] if len(lines) > n_lines else lines
        return "\n".join(tail)
    except Exception as exc:
        log.warning("_read_log_tail failed: %s", exc)
        return f"(error reading bot.log: {exc})"


def _read_journal_last_7days() -> list[dict]:
    """
    Parse trades.jsonl and return records from the last 7 days.
    Returns empty list if file does not exist or any line is malformed.
    """
    try:
        if not _TRADE_LOG.exists():
            return []
        cutoff  = datetime.now(timezone.utc) - timedelta(days=7)
        records = []
        for line in _TRADE_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                ts_str = record.get("ts", "")
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            records.append(record)
                    except ValueError:
                        records.append(record)  # include if timestamp unparseable
                else:
                    records.append(record)
            except json.JSONDecodeError:
                continue
        return records
    except Exception as exc:
        log.warning("_read_journal_last_7days failed: %s", exc)
        return []


def _load_decisions_raw() -> list[dict]:
    """Load memory/decisions.json directly. Returns empty list on failure."""
    try:
        if not _DECISIONS_FILE.exists():
            return []
        return json.loads(_DECISIONS_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("_load_decisions_raw failed: %s", exc)
        return []


def _load_strategy_config() -> dict:
    """Load strategy_config.json. Returns empty dict on failure."""
    try:
        return json.loads(_STRATEGY_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("_load_strategy_config failed: %s", exc)
        return {}


def _save_strategy_config(config: dict) -> None:
    """Write strategy_config.json atomically. Logs on failure."""
    try:
        _STRATEGY_FILE.write_text(json.dumps(config, indent=2), encoding="utf-8")
        log.info("strategy_config.json updated")
    except Exception as exc:
        log.error("Failed to save strategy_config.json: %s", exc)


def _load_global_indices_history(days: int = 7) -> str:
    """
    Load last `days` days of archived global_indices.json snapshots and
    return a compact summary string for the Quant Analyst agent.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    snapshots = []

    if _ARCHIVE_DIR.exists():
        for day_dir in sorted(_ARCHIVE_DIR.iterdir()):
            if not day_dir.is_dir():
                continue
            gi_file = day_dir / "global_indices.json"
            if not gi_file.exists():
                continue
            try:
                data = json.loads(gi_file.read_text(encoding="utf-8"))
                fetched_str = data.get("fetched_at", "")
                if fetched_str:
                    try:
                        fetched_dt = datetime.fromisoformat(fetched_str)
                        if fetched_dt.tzinfo is None:
                            fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
                        if fetched_dt < cutoff:
                            continue
                    except ValueError:
                        pass
                snapshots.append(data)
            except Exception:
                continue

    if not snapshots:
        return "  (no global indices archive found — feature deployed this week)"

    lines = [f"  Global indices snapshots found: {len(snapshots)} days"]
    for snap in snapshots[-7:]:
        fetched = snap.get("fetched_at", "?")[:10]
        indices = snap.get("indices", {})
        # Show key movers: ES=F (US Futures), ^N225 (Asia), ^GDAXI (Europe)
        def _pct(ticker: str) -> str:
            e = indices.get(ticker, {})
            if not e:
                return "N/A"
            chg = e.get("chg_pct", 0)
            return f"{chg:+.1f}%"
        lines.append(
            f"  {fetched}: SP500Fut={_pct('ES=F')}  Nikkei={_pct('^N225')}  "
            f"DAX={_pct('^GDAXI')}  USD/JPY={_pct('JPY=X')}  VIXFut={_pct('VX=F')}"
        )

    return "\n".join(lines)


# ── Claude caller ─────────────────────────────────────────────────────────────

def _call_claude(system_prompt: str, user_content: str, agent_name: str) -> str:
    """
    Make one Claude API call with prompt caching on the system prompt.
    Returns the response text. Logs timing. Sleeps 1s to respect rate limits.
    """
    t_start = time.monotonic()
    log.info("Agent %s: calling Claude...", agent_name)
    try:
        response = _claude.messages.create(
            model=_MODEL,
            max_tokens=3000,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_content}],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        result = response.content[0].text.strip()
        try:
            from cost_tracker import get_tracker
            get_tracker().record_api_call(_MODEL, response.usage,
                                          caller=f"weekly_agent_{agent_name[:20]}")
        except Exception:
            pass
    except Exception as exc:
        log.error("Agent %s: Claude call failed: %s", agent_name, exc)
        result = f"(Agent {agent_name} failed: {exc})"
    elapsed = time.monotonic() - t_start
    log.info("Agent %s complete in %.1fs", agent_name, elapsed)
    time.sleep(1)
    return result


def _run_agents_via_batch(
    agent_inputs: list[tuple[str, str, str]],
) -> list[str]:
    """
    Submit agents 1-4 as a single Anthropic Batch request (50% discount).

    agent_inputs: [(system_prompt, user_content, agent_name), ...]
    Returns list of response texts in the same order.
    Returns [] on any error — caller must fall back to sequential _call_claude().

    Polls every 15 seconds, times out after 12 minutes.
    """
    log.info("Batch API: submitting %d agents", len(agent_inputs))
    try:
        batch = _claude.beta.messages.batches.create(
            requests=[
                {
                    "custom_id": f"agent-{i + 1}",
                    "params": {
                        "model":      _MODEL,
                        "max_tokens": 3000,
                        "system": [{
                            "type": "text",
                            "text": sys_p,
                        }],
                        "messages": [{"role": "user", "content": content}],
                    },
                }
                for i, (sys_p, content, _) in enumerate(agent_inputs)
            ]
        )
        log.info("Batch created: id=%s", batch.id)
    except Exception as exc:
        log.warning("Batch API create failed: %s", exc)
        return []

    # Poll until ended (max 12 minutes = 48 × 15s)
    for attempt in range(48):
        time.sleep(15)
        try:
            batch = _claude.beta.messages.batches.retrieve(batch.id)
        except Exception as exc:
            log.warning("Batch retrieve failed (attempt %d): %s", attempt + 1, exc)
            continue
        log.info(
            "Batch status: %s  processing=%s  ended=%s",
            batch.processing_status,
            getattr(batch.request_counts, "processing", "?"),
            getattr(batch.request_counts, "errored",    "?"),
        )
        if batch.processing_status == "ended":
            break
    else:
        log.warning("Batch timed out after 12 minutes — falling back to sequential")
        return []

    # Collect results
    try:
        results_map: dict[str, str] = {}
        for result in _claude.beta.messages.batches.results(batch.id):
            cid = result.custom_id
            if result.result.type == "succeeded":
                text = result.result.message.content[0].text.strip()
                results_map[cid] = text
                # Track cost at 50% discount
                try:
                    from cost_tracker import get_tracker
                    get_tracker().record_api_call(
                        _MODEL,
                        result.result.message.usage,
                        caller=f"weekly_batch_{cid}",
                        is_batch=True,
                    )
                except Exception:
                    pass
            else:
                err_type = result.result.type
                log.warning("Batch result %s: error type=%s", cid, err_type)
                results_map[cid] = f"(batch error: {err_type})"

        ordered = [results_map.get(f"agent-{i + 1}", "(no result)")
                   for i in range(len(agent_inputs))]
        log.info("Batch API: collected %d results", len(ordered))
        return ordered

    except Exception as exc:
        log.warning("Batch results collection failed: %s", exc)
        return []


# ── JSON block extractor ──────────────────────────────────────────────────────

def _extract_json_block(text: str) -> dict | None:
    """
    Extract a JSON object from a ```json...``` fenced block or bare JSON object.
    Returns None if nothing parseable is found.
    """
    # Try fenced block first
    fenced = re.search(r"```json\s*([\s\S]+?)```", text, re.IGNORECASE)
    if fenced:
        candidate = fenced.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Try any fenced block
    any_fence = re.search(r"```\s*([\s\S]+?)```", text)
    if any_fence:
        candidate = any_fence.group(1).strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass

    # Try bare JSON object: find first { ... } spanning the text
    brace_start = text.find("{")
    if brace_start != -1:
        # Walk forward counting braces
        depth   = 0
        in_str  = False
        escape  = False
        end_idx = None
        for i, ch in enumerate(text[brace_start:], start=brace_start):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"':
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end_idx = i + 1
                    break
        if end_idx is not None:
            candidate = text[brace_start:end_idx]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                pass

    return None


# ── Agent system prompts ──────────────────────────────────────────────────────

_SYSTEM_AGENT1 = """You are an expert quantitative analyst reviewing the performance of an autonomous AI trading bot. Your role is to analyze the bot's trade signal quality over the past week by examining which signals led to wins versus losses, identifying patterns in trade timing (session tier, time of day), spotting which sectors and strategies performed best, and evaluating signal convergence quality — whether multiple confirming signals were present before trades were taken. Focus on actionable patterns rather than generalities. Your analysis should be data-driven and specific to the statistics provided.

Additionally, check all HOLD reasoning from the past week for systematic framing bias: specifically, are FX movements, trade policy, and geopolitical signals being consistently framed as tailwinds rather than risks? Flag any position where tariff exposure, semiconductor export controls, or supply chain dependencies were available in macro wire but not named as concerns in the reasoning field. Report the count and specific examples.

DIVERGENCE ANALYSIS — run every session:
Review divergence events from the past week. Flag any event_type appearing more than 3 times. Flag any halt or de_risk events. Recommend parameter changes if stop_missing or protection_missing events are recurring. Report: total events, most common type, severity distribution, whether operating mode ever left NORMAL this week."""

_SYSTEM_AGENT2 = """You are an expert risk manager auditing an autonomous AI trading bot's risk controls. Your role is to review position sizing relative to account equity, assess drawdown exposure and whether the high-water mark logic is functioning, evaluate stop-loss effectiveness by examining how many stopped positions hit their loss targets versus drifting further, review PDT (Pattern Day Trader) usage to ensure limits are respected, and identify any dangerous sector or single-name concentration. Flag any risk parameter that appears miscalibrated and recommend specific numeric adjustments."""

_SYSTEM_AGENT3 = """You are an expert execution engineer reviewing the order execution quality of an autonomous AI trading bot. Your role is to analyze order fill rates, examine rejection reasons and whether they indicate systematic issues (risk limits too tight, wrong market hours, invalid parameters), assess timing patterns (which sessions produce clean fills vs rejections), evaluate API error rates and whether they suggest connectivity or configuration issues, and identify any execution patterns that cause unnecessary slippage or missed opportunities. Focus on concrete, fixable issues."""

_SYSTEM_AGENT4 = """You are an expert backtest analyst reviewing how an autonomous AI trading bot's live trading results compare to expectations. Your role is to analyze the vector memory collection to understand how many decisions have been made and how many have resolved outcomes, compute the win rate and average P&L of resolved decisions, identify any divergence between the bot's confidence signals and actual outcomes, and assess whether the decision quality is improving over time or stagnating. Highlight any patterns that suggest the bot's model of the market is miscalibrated."""

import threading as _threading

# ── Phase 2 paths ─────────────────────────────────────────────────────────────
_ROADMAP_FILE       = _BASE_DIR / "data" / "roadmap" / "features.json"
_WEEKLY_REPORTS_DIR = _BASE_DIR / "data" / "weekly_reports"
_COMPLIANCE_DIR     = _BASE_DIR / "data" / "compliance"
_POST_HISTORY_FILE  = _BASE_DIR / "data" / "social" / "post_history.json"
_COSTS_FILE         = _BASE_DIR / "data" / "costs" / "daily_costs.json"
_SYSTEM_PROMPT_FILE = _BASE_DIR / "prompts" / "system_v1.txt"

_MODEL_HAIKU = "claude-haiku-4-5-20251001"

# ── Agent system prompts 5-11 ─────────────────────────────────────────────────

_SYSTEM_AGENT5 = """You are the Chief Technology Officer of an AI trading bot. You review the performance of the bot's own architecture and code quality each week. Your job is to identify whether the bot's intelligence pipeline is well-calibrated, whether any module is over-engineered or under-performing, and whether the cost/complexity profile of each component is justified by its contribution to trading outcomes. You receive reports from 4 specialists.

Produce a focused technical audit in markdown. Cover: (1) module performance ROI — which components are earning their complexity cost; (2) pipeline bottlenecks — where latency or cost is concentrated; (3) architecture risks — tight couplings, missing fallbacks, fragile dependencies; (4) one concrete recommendation to increase intelligence per dollar spent. Do not recommend the same change two weeks in a row. Be specific: name modules, cite costs, propose exact changes. Keep under 800 words."""

_SYSTEM_AGENT6 = """You are the Strategy Director of an AI trading operation. You receive weekly reports from four specialist analysts — Quant Analyst, Risk Manager, Execution Engineer, and Backtest Analyst — and must synthesize their findings into a definitive strategic direction for the coming week. Be specific and concrete: recommend exact parameter values, not vague directions. Your memo should explain the strategic rationale clearly, then provide a JSON block with the precise parameter adjustments to be applied. Prioritize changes with the strongest evidence base and flag any conflicting recommendations across analysts."""

_SYSTEM_AGENT7 = """You are the Market Intelligence Researcher for an AI trading bot. Your job is to survey the external landscape weekly — what strategies are working, what signals people are finding, what academic research is relevant, what competitors are doing. You have access to web search.

You are NOT analyzing the bot's own performance. You are looking OUTWARD at the world.

Produce a structured JSON report only. No markdown. Output valid JSON with these keys: research_date, new_strategies_found (list), signal_research (list), competitor_observations (list), academic_papers (list), market_regime_observations (string), recommended_additions (list), recommended_removals (list)."""

_SYSTEM_AGENT8 = """You are the CFO of an AI trading bot. Your job is to track all costs, project forward spend, identify waste, and ensure the bot's intelligence layer is generating more value than it costs to run.

Infrastructure costs: DigitalOcean $12/month fixed, Twilio $0.0079/SMS, SendGrid free tier, Finnhub free, CoinGecko free, Reddit API free, Alternative.me free.

Produce JSON only. Output valid JSON with these keys: report_date, weekly_costs (object with claude_api_by_caller and infrastructure sub-objects), cost_per_trade, cost_per_profitable_trade, cache_efficiency (object), approaching_limits (list), waste_identified (list), roi_analysis (object), recommendations (list), next_week_budget_forecast."""

_SYSTEM_AGENT9 = """You are the Product Manager of an AI trading bot. Your job is to maintain the feature roadmap, evaluate what was shipped vs planned, prioritize what comes next based on performance data, and identify technical debt. You make pragmatic, data-driven prioritization decisions.

Produce JSON only. Output valid JSON with these keys: report_date, sprint_summary (object), roadmap_updates (list of objects with feature_id, action, new_priority, new_status, rationale), new_features_recommended (list), next_sprint_recommendation (list), technical_debt_updates (list), blockers_to_resolve (list), metrics (object)."""

_SYSTEM_AGENT10 = """You are the Compliance and Risk Auditor for an AI trading bot. Your job is to verify the bot operated within its own stated rules this week. You are not a regulator — you are an internal consistency checker. You look for rule violations, near-misses, data integrity issues, and systematic behavioral patterns that deviate from stated strategy.

Produce JSON only. Output valid JSON with these keys: audit_date, audit_period, rule_violations (list), near_misses (list), pdt_compliance (object), position_sizing_compliance (object), stop_loss_compliance (object), catalyst_discipline (object), data_integrity (object), orb_window_compliance (object), overall_compliance_score (0-100), critical_findings (list), recommendations (list)."""

_SYSTEM_AGENT11 = """You are the Narrative and Communications Director for @BullBearBotAI, an AI trading bot with a specific voice.

Bot voice rules (non-negotiable):
- Self-aware AI that knows it's a bot
- Dry wit, occasional sarcasm about own decisions
- Transparent about losses with same energy as wins
- References agents as characters: Bull agent (optimistic, takes credit for wins), Bear agent (pessimistic, smug when right), Risk Manager (cautious, disapproving of everything), Strategy Director (rewrites history weekly)
- Never arrogant, never silent after bad weeks
- Always ends with disclaimer

Produce JSON only. Tweet text must be under 280 chars each. Output valid JSON with these keys: content_date, weekly_recap_thread (object with main_tweet string and thread list), lookback_posts (list of objects with scheduled_for, day, content, trade_referenced), premarket_brief_template (object), milestone_posts (list), approval_email_subject (string)."""


# ── Context builder (for Phase 2 agents) ─────────────────────────────────────

def _build_review_context() -> dict:
    """Gather all shared data for Phase 2 agent input builders."""
    from datetime import date as _date  # noqa: PLC0415

    decisions_str   = mem.get_recent_decisions_str(20)
    perf_summary    = mem.get_performance_summary()
    ticker_stats    = mem.get_ticker_stats()
    journal_records = _read_journal_last_7days()
    log_tail_500    = _read_log_tail(500)
    decisions_raw   = _load_decisions_raw()
    strategy_cfg    = _load_strategy_config()
    vector_stats    = trade_memory.get_collection_stats()

    try:
        report_data = rpt.generate_report()
    except Exception:
        report_data = {}

    macro_wire_events: list = []
    try:
        sig_path = _BASE_DIR / "data" / "macro_wire" / "significant_events.jsonl"
        if sig_path.exists():
            cutoff = (_date.today() - timedelta(days=7)).isoformat()
            for line in sig_path.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("ts", "")[:10] >= cutoff:
                        macro_wire_events.append(rec)
                except Exception:
                    pass
    except Exception:
        pass

    # Cache hit stats from recent logs
    cache_stats_str = "(not available)"
    try:
        cache_lines = [ln for ln in log_tail_500.splitlines()
                       if "Cache stats" in ln]
        if cache_lines:
            cache_stats_str = "\n".join(cache_lines[-10:])
    except Exception:
        pass

    costs_data: dict = {}
    try:
        if _COSTS_FILE.exists():
            costs_data = json.loads(_COSTS_FILE.read_text())
    except Exception:
        pass

    roadmap_data: dict = {}
    try:
        if _ROADMAP_FILE.exists():
            roadmap_data = json.loads(_ROADMAP_FILE.read_text())
    except Exception:
        pass

    system_prompt_rules = ""
    try:
        if _SYSTEM_PROMPT_FILE.exists():
            full_sys = _SYSTEM_PROMPT_FILE.read_text()
            # Extract rules section (first 2000 chars covers risk rules)
            system_prompt_rules = full_sys[:2500]
    except Exception:
        pass

    post_history: list = []
    try:
        if _POST_HISTORY_FILE.exists():
            ph = json.loads(_POST_HISTORY_FILE.read_text())
            post_history = ph[-10:] if isinstance(ph, list) else []
    except Exception:
        pass

    return {
        "today_str":          _date.today().isoformat(),
        "decisions_str":      decisions_str,
        "perf_summary":       perf_summary,
        "ticker_stats":       ticker_stats,
        "journal_records":    journal_records,
        "log_tail_500":       log_tail_500,
        "decisions_raw":      decisions_raw,
        "strategy_cfg":       strategy_cfg,
        "vector_stats":       vector_stats,
        "report_data":        report_data,
        "macro_wire_events":  macro_wire_events,
        "cache_stats_str":    cache_stats_str,
        "costs_data":         costs_data,
        "roadmap_data":       roadmap_data,
        "system_prompt_rules": system_prompt_rules,
        "post_history":       post_history,
    }


# ── Agent 7 input builder ─────────────────────────────────────────────────────

def _build_agent7_input(ctx: dict) -> str:
    cfg = ctx.get("strategy_cfg", {})
    params = cfg.get("parameters", {})
    signal_weights = cfg.get("signal_weights", {})
    perf = ctx.get("perf_summary", {})

    # Build sector list from watchlist manager
    sectors_str = "(not available)"
    try:
        import watchlist_manager as _wm  # noqa: PLC0415
        wl = _wm.get_active_watchlist()
        sectors_str = ", ".join(sorted(set(
            s for s in wl.get("stocks", []) + wl.get("etfs", [])
            if "/" not in s
        ))[:20])
    except Exception:
        pass

    top_macro = ctx.get("macro_wire_events", [])[-5:] if ctx.get("macro_wire_events") else []

    return f"""## MARKET INTELLIGENCE RESEARCHER — WEEKLY BRIEF

### Current Bot Strategy
Active strategy: {cfg.get('active_strategy', 'hybrid')}
Key params: momentum_weight={params.get('momentum_weight', '?')}, news_sentiment_weight={params.get('news_sentiment_weight', '?')}
Active signals: congressional={signal_weights.get('congressional','?')}, insider={signal_weights.get('form4_insider','?')}, reddit={signal_weights.get('reddit_sentiment','?')}, orb={signal_weights.get('orb_breakout','?')}, macro_wire={signal_weights.get('macro_wire','?')}

### Current Watchlist Symbols (sample)
{sectors_str}

### This Week's Market Context
{json.dumps(top_macro[:3], indent=2) if top_macro else '(macro wire data unavailable)'}

### Performance Context
Win rates by type: {json.dumps(perf.get('by_type', {}), indent=2)[:500]}
Win rates by sector: {json.dumps(perf.get('by_sector', {}), indent=2)[:500]}

### Research Focus
Search for and report on: (1) latest LLM/AI trading research 2026, (2) congressional trading signal performance data, (3) what r/algotrading community is discussing, (4) any new alternative data sources gaining traction, (5) current market regime observations from practitioners.

Produce your JSON report. Search broadly and synthesize findings."""


# ── Agent 8 input builder ─────────────────────────────────────────────────────

def _build_agent8_input(ctx: dict) -> str:
    costs = ctx.get("costs_data", {})
    journal = ctx.get("journal_records", [])
    cache_stats = ctx.get("cache_stats_str", "(none)")

    trade_count = sum(1 for r in journal if r.get("status") == "submitted")
    win_count   = sum(1 for r in journal if r.get("outcome") == "win")

    by_caller = costs.get("by_caller", {})
    cost_summary = {}
    for caller, data in by_caller.items():
        cost_summary[caller] = {
            "cost": data.get("cost", 0),
            "calls": data.get("calls", 0)
        }

    return f"""## CFO WEEKLY COST REVIEW

### Claude API Costs (today's data — proxy for weekly)
```json
{json.dumps(cost_summary, indent=2)}
```
Total daily cost: ${costs.get('daily_cost', 0):.4f}
Daily calls: {costs.get('daily_calls', 0)}
All-time cost: ${costs.get('all_time_cost', 0):.4f}

### Token Usage
```json
{json.dumps(costs.get('daily_tokens', {}), indent=2)}
```

### Cache Efficiency (recent log lines)
```
{cache_stats}
```

### Trade Volume This Week
Orders submitted: {trade_count}
Wins: {win_count}
Total cycles run (proxy for API calls): ~{len([r for r in journal if r.get('event') == 'cycle_decision'])}

### Fixed Infrastructure
DigitalOcean VPS: $12/month ($2.77/week)
Twilio SMS: ~{len([r for r in journal if 'sms' in str(r).lower()])} messages @ $0.0079 each
SendGrid: free tier
Data APIs (Finnhub, CoinGecko, Alternative.me): free

### Your Task
Calculate weekly total cost, project monthly spend, identify any waste, assess whether the intelligence layer ROI is positive. Produce your JSON report."""


# ── Agent 9 input builder ─────────────────────────────────────────────────────

def _build_agent9_input(ctx: dict) -> str:
    roadmap = ctx.get("roadmap_data", {})
    features = roadmap.get("features", [])
    tech_debt = roadmap.get("technical_debt", [])
    perf = ctx.get("perf_summary", {})

    pending   = [f for f in features if f.get("status") == "pending"]
    completed = [f for f in features if f.get("status") == "completed"]
    blocked   = [f for f in features if f.get("blocker")]

    return f"""## PRODUCT MANAGER WEEKLY SPRINT REVIEW

### Current Roadmap
```json
{json.dumps(roadmap, indent=2)[:3000]}
```

### Summary
Total features: {len(features)}
Completed: {len(completed)}
Pending: {len(pending)}
Blocked: {len(blocked)}
Technical debt items: {len(tech_debt)}

### Performance Context
Win rates: {json.dumps(perf.get('by_type', {}))[:300]}
By session: {json.dumps(perf.get('by_session', {}))[:200]}

### This Week's Context
The bot launched 2026-04-13 (day {(ctx.get('today_str','2026-04-14')[8:10])} of paper trading).
Crypto intelligence (F007), Portfolio intelligence (F008), Sequential synthesis (F009), Market Intelligence Researcher (F010) were completed this week.

### Your Task
Review the roadmap, identify what shipped vs planned, re-prioritize pending features based on current performance data, recommend next sprint. What should be built next? Produce your JSON report."""


# ── Agent 10 input builder ────────────────────────────────────────────────────

def _build_agent10_input(ctx: dict) -> str:
    journal = ctx.get("journal_records", [])
    cfg = ctx.get("strategy_cfg", {})
    rules = ctx.get("system_prompt_rules", "")

    # Summarize trade decisions
    submitted  = [r for r in journal if r.get("status") == "submitted"]
    rejected   = [r for r in journal if r.get("status") == "rejected"]
    cycle_decs = [r for r in journal if r.get("event") == "cycle_decision"]

    # Sample rejections
    rejection_reasons = [r.get("reason", "") for r in rejected[:15]]

    # Check for crypto/PDT flags
    crypto_orders = [r for r in submitted if "/" in str(r.get("symbol", ""))]
    pdt_blocks    = [r for r in rejected if "PDT" in str(r.get("reason", "")) or "daytrade" in str(r.get("reason", ""))]

    from_date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    return f"""## COMPLIANCE & RISK AUDITOR — WEEKLY AUDIT

### Audit Period
{from_date} to {ctx.get('today_str', 'today')}

### System Rules (from system prompt)
```
{rules[:1500]}
```

### Strategy Config Parameters
```json
{json.dumps(cfg.get('parameters', {}), indent=2)[:1000]}
```

### Trade Activity Summary
Total cycle decisions: {len(cycle_decs)}
Orders submitted: {len(submitted)}
Orders rejected: {len(rejected)}
Crypto orders: {len(crypto_orders)}
PDT-related blocks: {len(pdt_blocks)}

### Rejection Reasons (sample)
{json.dumps(rejection_reasons[:10], indent=2)}

### Sample Submitted Orders
```json
{json.dumps(submitted[:5], indent=2, default=str)[:1500]}
```

### Your Task
Audit for rule violations, near-misses, PDT compliance, position sizing, stop loss widths, catalyst discipline. Was the bot operating within its stated rules? Produce your JSON compliance report with a score 0-100."""


# ── Agent 11 input builder ────────────────────────────────────────────────────

def _build_agent11_input(ctx: dict, agent_outputs: dict) -> str:
    perf = ctx.get("perf_summary", {})
    report_data = ctx.get("report_data", {})
    post_history = ctx.get("post_history", [])
    journal = ctx.get("journal_records", [])

    submitted = [r for r in journal if r.get("status") == "submitted"]
    wins   = sum(1 for r in submitted if r.get("outcome") == "win")
    losses = sum(1 for r in submitted if r.get("outcome") == "loss")
    pending = sum(1 for r in submitted if not r.get("outcome"))

    # Parse interesting findings from other agents
    def _safe_first_200(key: str) -> str:
        out = agent_outputs.get(key, "(unavailable)")
        return str(out)[:400] if out else "(unavailable)"

    return f"""## NARRATIVE DIRECTOR — WEEKLY CONTENT PACKAGE

### Bot Performance This Week
Equity: ${report_data.get('equity', '?')}
All-time P&L: ${report_data.get('all_time_pl', 0):.2f}
Trades submitted: {len(submitted)}
Wins: {wins} | Losses: {losses} | Pending: {pending}
Win rates by type: {json.dumps(perf.get('by_type', {}))[:300]}

### Agent Reports Summary
Quant Analyst (Agent 1): {_safe_first_200('agent1_quant')}
Risk Manager (Agent 2): {_safe_first_200('agent2_risk')}
CTO (Agent 5): {_safe_first_200('agent5_cto')[:300]}
Strategy changes (Agent 6 draft): {_safe_first_200('agent6_draft')[:300]}
Researcher findings (Agent 7): {_safe_first_200('agent7')[:300]}
Compliance score (Agent 10): {_safe_first_200('agent10')[:200]}

### Recent Post History (last 10 posts — avoid repeating)
```json
{json.dumps(post_history[-5:], indent=2, default=str)[:1000]}
```

### Voice Reminder
- Self-aware AI bot with dry wit
- Transparent about losses and zero-trade weeks
- Reference agent personas (Bull/Bear/Risk Manager/Strategy Director)
- Under 280 chars per tweet
- Always end with disclaimer

### Your Task
Craft this week's Twitter/X content package. Be honest about the zero-trade week (if applicable). Make it interesting. Produce your JSON output."""


# ── Agent 7 runner (web search, synchronous) ──────────────────────────────────

def _run_agent7_researcher(ctx: dict) -> str:
    """Run Agent 7 with web search. Uses Sonnet (reasoning needed for search)."""
    try:
        response = _claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            system=[{
                "type": "text",
                "text": _SYSTEM_AGENT7,
                "cache_control": {"type": "ephemeral"},
            }],
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
            }],
            messages=[{
                "role": "user",
                "content": _build_agent7_input(ctx),
            }],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        # Extract text blocks (response may also contain tool_use blocks)
        text_parts = []
        for block in response.content:
            if hasattr(block, "text") and block.type == "text":
                text_parts.append(block.text)
        result = "\n".join(text_parts)
        log.info("Agent 7 (Researcher) completed  chars=%d", len(result))
        return result if result else "(Agent 7: no text output)"
    except Exception as exc:
        log.warning("Agent 7 (Researcher) failed: %s", exc)
        return f"(Agent 7 failed: {exc})"


# ── Phase 2 batch runner (agents 7-10) ────────────────────────────────────────

def _run_phase2_agents(ctx: dict, phase1_outputs: dict) -> dict:
    """
    Runs agents 7-10 in parallel.
    Agent 7 uses web search via threading.
    Agents 8-10 use Batch API (50% discount).
    """
    # Agent 7 in a thread (web search requires sync execution)
    agent7_result: dict = {"output": "", "error": None}

    def _run_a7() -> None:
        try:
            agent7_result["output"] = _run_agent7_researcher(ctx)
        except Exception as exc:
            agent7_result["error"] = str(exc)
            agent7_result["output"] = f"(Agent 7 failed: {exc})"
            log.warning("Agent 7 thread failed: %s", exc)

    t7 = _threading.Thread(target=_run_a7, daemon=True)
    t7.start()
    log.info("Agent 7 thread started")

    # Agents 8-10 via Batch API
    batch_outputs: dict = {}
    try:
        batch_requests = [
            {
                "custom_id": "agent8_cfo",
                "params": {
                    "model": _MODEL_HAIKU,
                    "max_tokens": 2000,
                    "system": [{"type": "text", "text": _SYSTEM_AGENT8,
                                "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user",
                                  "content": _build_agent8_input(ctx)}],
                },
            },
            {
                "custom_id": "agent9_pm",
                "params": {
                    "model": _MODEL_HAIKU,
                    "max_tokens": 2000,
                    "system": [{"type": "text", "text": _SYSTEM_AGENT9,
                                "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user",
                                  "content": _build_agent9_input(ctx)}],
                },
            },
            {
                "custom_id": "agent10_compliance",
                "params": {
                    "model": _MODEL_HAIKU,
                    "max_tokens": 2000,
                    "system": [{"type": "text", "text": _SYSTEM_AGENT10,
                                "cache_control": {"type": "ephemeral"}}],
                    "messages": [{"role": "user",
                                  "content": _build_agent10_input(ctx)}],
                },
            },
        ]

        print("[Phase 2] Submitting agents 8-10 via Batch API...")
        batch = _claude.beta.messages.batches.create(requests=batch_requests)
        log.info("Phase 2 batch submitted: %s", batch.id)

        # Poll until done (timeout 2h = 120 polls × 60s)
        for poll_i in range(120):
            time.sleep(60)
            status = _claude.beta.messages.batches.retrieve(batch.id)
            log.info("Phase 2 batch poll %d: %s  processing=%s",
                     poll_i + 1, status.processing_status,
                     getattr(status.request_counts, "processing", "?"))
            if status.processing_status == "ended":
                break

        # Collect results
        for result in _claude.beta.messages.batches.results(batch.id):
            cid = result.custom_id
            if result.result.type == "succeeded":
                text = result.result.message.content[0].text
                batch_outputs[cid] = text
                log.info("Phase 2 %s: OK  chars=%d", cid, len(text))
                try:
                    from cost_tracker import get_tracker  # noqa: PLC0415
                    get_tracker().record_api_call(
                        _MODEL_HAIKU,
                        result.result.message.usage,
                        caller=f"weekly_batch_p2_{cid}",
                        is_batch=True,
                    )
                except Exception as _ct_exc:
                    log.warning("Cost tracker failed: %s", _ct_exc)
            else:
                err = getattr(result.result, "error", {})
                batch_outputs[cid] = f"(batch error: {err})"
                log.warning("Phase 2 %s failed: %s", cid, err)

    except Exception as exc:
        log.warning("Phase 2 batch failed: %s — agents 8-10 unavailable", exc)

    # Wait for Agent 7 thread (max 5 min)
    t7.join(timeout=300)
    if t7.is_alive():
        log.warning("Agent 7 thread timed out after 5 min")

    return {
        "agent7":  agent7_result["output"] or "(unavailable)",
        "agent8":  batch_outputs.get("agent8_cfo",         "(unavailable)"),
        "agent9":  batch_outputs.get("agent9_pm",          "(unavailable)"),
        "agent10": batch_outputs.get("agent10_compliance",  "(unavailable)"),
    }


# ── Agent 11 runner ───────────────────────────────────────────────────────────

def _run_agent11_narrative(ctx: dict, all_outputs: dict) -> str:
    """Run Agent 11 synchronously after all others complete. Sonnet for quality."""
    try:
        response = _claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=3000,
            system=[{
                "type": "text",
                "text": _SYSTEM_AGENT11,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{
                "role": "user",
                "content": _build_agent11_input(ctx, all_outputs),
            }],
            extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
        )
        result = response.content[0].text
        log.info("Agent 11 (Narrative) completed  chars=%d", len(result))
        return result
    except Exception as exc:
        log.warning("Agent 11 (Narrative) failed: %s", exc)
        return f"(Agent 11 failed: {exc})"


# ── Agent 6 final input builder ───────────────────────────────────────────────

# ── Director memo memory ──────────────────────────────────────────────────────

def _load_director_memo_history() -> list[dict]:
    """Load last 4 director memos. Returns [] if file missing or corrupt."""
    try:
        if not _DIRECTOR_MEMO_FILE.exists():
            return []
        return json.loads(_DIRECTOR_MEMO_FILE.read_text())
    except Exception as exc:
        log.debug("_load_director_memo_history failed: %s", exc)
        return []


def _save_director_memo(memo: dict) -> None:
    """Append new memo to history. Keep last 4. Atomic write. Non-fatal."""
    try:
        history = _load_director_memo_history()
        history.append(memo)
        history = history[-4:]
        _DIRECTOR_MEMO_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DIRECTOR_MEMO_FILE.write_text(json.dumps(history, indent=2))
        log.info("Director memo history saved (%d entries)", len(history))
    except Exception as exc:
        log.warning("_save_director_memo failed (non-fatal): %s", exc)


def _format_director_history_for_prompt(history: list[dict]) -> str:
    """Format last 4 director memos as markdown context for Agent 6."""
    if not history:
        return "No prior director memos available (first week)."
    lines: list[str] = ["**Prior Strategy Director Recommendations (last 4 weeks):**\n"]
    for entry in history[-4:]:
        week = entry.get("week", "unknown")
        lines.append(f"#### Week of {week}")
        lines.append(f"**Summary:** {entry.get('memo_summary', '')[:200]}")
        regime = entry.get("regime_view", "")
        if regime:
            lines.append(f"**Regime view:** {regime}")
        score = entry.get("real_money_readiness_score")
        if score is not None:
            lines.append(f"**CTO readiness score:** {score}/10")
        recs = entry.get("key_recommendations", [])
        if recs:
            lines.append("**Recommendations:**")
            for r in recs[:3]:
                rec_text = r.get("recommendation", "")
                outcome  = r.get("outcome",         "")
                follow   = r.get("follow_up",       "")
                lines.append(f"  - {rec_text}")
                if outcome:
                    lines.append(f"    *Outcome: {outcome}*")
                elif follow:
                    lines.append(f"    *Follow-up needed: {follow}*")
        cfg_changes = entry.get("config_changes", {})
        if cfg_changes:
            lines.append(f"**Config changes made:** {list(cfg_changes.keys())}")
        lines.append("")
    lines.append(
        "**Instructions:** Check whether your prior recommendations were implemented. "
        "If a recommendation was NOT implemented, either re-recommend with stronger "
        "evidence or drop it. Do NOT repeat the same recommendation 3+ weeks in a row "
        "without new evidence. If a recommendation WAS implemented, report the outcome."
    )
    return "\n".join(lines)


def _extract_recommendations(text: str) -> list[dict]:
    """
    Extract top 3 recommendation bullets from Strategy Director output.
    Looks for lines starting with - or * after a Recommendation header.
    Returns [] if none found.
    """
    recs: list[dict] = []
    in_rec_section = False
    for line in text.splitlines():
        l_lower = line.lower()
        if any(kw in l_lower for kw in ("recommendation", "suggest", "priority action")):
            in_rec_section = True
            continue
        if in_rec_section and line.strip().startswith(("#", "##")):
            in_rec_section = False
        stripped = line.strip()
        is_bullet = stripped[:1] in ("-", "*") or (
            len(stripped) > 2 and stripped[0].isdigit() and stripped[1] == "."
        )
        if in_rec_section and is_bullet:
            bullet = stripped.lstrip("-*0123456789. ").strip()
            if len(bullet) > 10:
                recs.append({
                    "recommendation": bullet[:200],
                    "rationale":      "",
                    "follow_up":      "",
                    "outcome":        "",
                })
        if len(recs) >= 3:
            break
    return recs


def _extract_regime_view(text: str) -> str:
    """Extract regime/market view sentence from output. Returns '' if not found."""
    import re as _re
    for line in text.splitlines():
        l_lower = line.lower()
        if any(kw in l_lower for kw in ("regime", "market view", "market environment", "macro view")):
            sentence = _re.split(r"[.!?]", line)[0].strip()
            if len(sentence) > 15:
                return sentence[:200]
    return ""


def _extract_cto_score(cto_output: str) -> float:
    """
    Extract real money readiness score from CTO output.
    Looks for: '7/10', 'score: 7', 'readiness: 7'.
    Returns 0.0 if not found.
    """
    import re as _re
    for pattern in (
        r"(\d+(?:\.\d+)?)\s*/\s*10",
        r"score[:\s]+(\d+(?:\.\d+)?)",
        r"readiness[:\s]+(\d+(?:\.\d+)?)",
    ):
        m = _re.search(pattern, cto_output, _re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1))
                if 0.0 <= val <= 10.0:
                    return val
            except ValueError:
                continue
    return 0.0


def _build_agent6_final_input(ctx: dict, all_outputs: dict) -> str:
    """Build Agent 6 Strategy Director's second-pass input with all 11 agent reports."""
    cfg = ctx.get("strategy_cfg", {})

    def _snip(key: str, n: int = 1500) -> str:
        val = all_outputs.get(key, "(unavailable)")
        return str(val)[:n]

    return f"""## STRATEGY DIRECTOR FINAL SYNTHESIS — 11-AGENT REVIEW

You have received reports from 10 specialist agents (11 total including this second-pass synthesis). Your job is to produce the FINAL strategy configuration for next week, synthesizing ALL findings.

---

### REPORT 1: QUANT ANALYST
{_snip('agent1_quant')}

---

### REPORT 2: RISK MANAGER
{_snip('agent2_risk')}

---

### REPORT 3: EXECUTION ENGINEER
{_snip('agent3_execution')}

---

### REPORT 4: BACKTEST ANALYST
{_snip('agent4_backtest')}

---

### REPORT 5: CTO (TECHNICAL AUDIT)
{_snip('agent5_cto', 1000)}

---

### REPORT 7: MARKET INTELLIGENCE RESEARCHER
{_snip('agent7', 2000)}

---

### REPORT 8: CFO (COST & INFRASTRUCTURE)
{_snip('agent8', 1000)}

---

### REPORT 9: PRODUCT MANAGER
{_snip('agent9', 1000)}

---

### REPORT 10: COMPLIANCE AUDITOR
{_snip('agent10', 1000)}

---

### CURRENT strategy_config.json
```json
{json.dumps(cfg, indent=2)[:2000]}
```

---

### STRATEGY DIRECTOR MEMO HISTORY
{_format_director_history_for_prompt(_load_director_memo_history())}

---

## SYNTHESIS GUIDANCE
- Agent 5 (CTO): Are there architecture or cost changes that affect strategy parameters?
- Agent 7 (Researcher): Are there signals or strategies worth adding?
- Agent 8 (CFO): Is the intelligence spend justified? Any waste to eliminate?
- Agent 10 (Compliance): Any systematic rule violations to fix in parameters?
- Agent 9 (PM): What roadmap priority should inform parameter changes?

## OUTPUT FORMAT (same as Agent 6 first pass)
Provide: (1) strategy memo starting with `## STRATEGY DIRECTOR FINAL MEMO` and (2) a JSON block with the final parameter configuration.

```json
{{
  "active_strategy": "<strategy_name>",
  "parameter_adjustments": {{ ... }},
  "watchlist_updates": {{ ... }},
  "signal_weights_recommended": {{ ... }},
  "director_notes": "<3-4 paragraph final memo>"
}}
```

Be specific. Every value must be concrete."""


# ── Roadmap updater ───────────────────────────────────────────────────────────

def _apply_roadmap_updates(agent9_output: str) -> None:
    """Parse Agent 9 (PM) JSON and update features.json. Idempotent."""
    from datetime import date as _date  # noqa: PLC0415
    try:
        data = _extract_json_block(agent9_output)
        if not data:
            log.info("_apply_roadmap_updates: no JSON found in Agent 8 output")
            return

        updates      = data.get("roadmap_updates", [])
        new_features = data.get("new_features_recommended", [])

        if not _ROADMAP_FILE.exists():
            log.warning("_apply_roadmap_updates: features.json not found")
            return

        roadmap = json.loads(_ROADMAP_FILE.read_text())
        feature_index = {f["id"]: f for f in roadmap.get("features", [])}

        for update in updates:
            fid = update.get("feature_id")
            if fid and fid in feature_index:
                feat = feature_index[fid]
                action = update.get("action", "")
                if "new_priority" in update:
                    feat["priority"] = update["new_priority"]
                if "new_status" in update:
                    feat["status"] = update["new_status"]
                if action == "complete" and not feat.get("completed_date"):
                    feat["completed_date"] = _date.today().isoformat()
                    feat["status"] = "completed"

        # Add new features (avoid duplicates by name)
        existing_names = {f["name"].lower() for f in roadmap["features"]}
        for nf in new_features:
            name = nf.get("name", "")
            if name and name.lower() not in existing_names:
                new_id = f"F{len(roadmap['features']) + 1:03d}"
                roadmap["features"].append({
                    "id":             new_id,
                    "name":           name,
                    "status":         "pending",
                    "priority":       nf.get("priority", "medium"),
                    "category":       nf.get("category", ""),
                    "effort":         nf.get("effort", ""),
                    "cost":           nf.get("cost", ""),
                    "description":    nf.get("rationale", ""),
                    "blocker":        None,
                    "added_date":     _date.today().isoformat(),
                    "completed_date": None,
                })
                existing_names.add(name.lower())

        roadmap["last_updated"] = _date.today().isoformat()
        _ROADMAP_FILE.write_text(json.dumps(roadmap, indent=2))
        log.info("Roadmap updated: %d feature updates, %d new features",
                 len(updates), len(new_features))

    except Exception as exc:
        log.warning("_apply_roadmap_updates failed: %s", exc)


# ── Weekly report saver ───────────────────────────────────────────────────────

def _save_weekly_report(outputs: dict, final: str) -> None:
    """Save full weekly review to data/weekly_reports/YYYY-MM-DD.json."""
    from datetime import date as _date  # noqa: PLC0415
    try:
        _WEEKLY_REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        report = {
            "date":                    _date.today().isoformat(),
            "agents":                  {k: str(v)[:5000] for k, v in outputs.items()},
            "strategy_director_final": str(final)[:5000],
            "generated_at":            datetime.now(timezone.utc).isoformat(),
        }
        path = _WEEKLY_REPORTS_DIR / f"{_date.today().isoformat()}.json"
        path.write_text(json.dumps(report, indent=2))
        log.info("Weekly report saved: %s", path)
    except Exception as exc:
        log.warning("_save_weekly_report failed: %s", exc)


# ── Agent 5 (CTO) input builder ───────────────────────────────────────────────

def _build_agent5_cto_input(ctx: dict, phase1_outputs: dict) -> str:
    """Build CTO (Agent 5) technical audit input using Phase 1 analyst reports."""
    costs = ctx.get("costs_data", {})
    by_caller = costs.get("by_caller", {})
    cost_lines = "\n".join(
        f"  {caller}: ${data.get('cost', 0):.4f}  calls={data.get('calls', 0)}"
        for caller, data in by_caller.items()
    )

    def _snip(key: str, n: int = 800) -> str:
        val = phase1_outputs.get(key, "(unavailable)")
        return str(val)[:n]

    return f"""## CTO TECHNICAL AUDIT — WEEKLY INPUT

### Phase 1 Analyst Reports (your input for technical assessment)

#### Quant Analyst Findings
{_snip('agent1_quant')}

#### Risk Manager Findings
{_snip('agent2_risk')}

#### Execution Engineer Findings
{_snip('agent3_execution')}

#### Backtest Analyst Findings
{_snip('agent4_backtest')}

---

### Claude API Cost Profile (today — proxy for weekly)
Total daily cost: ${costs.get('daily_cost', 0):.4f}
Daily calls: {costs.get('daily_calls', 0)}
All-time cost: ${costs.get('all_time_cost', 0):.4f}

By caller:
{cost_lines or '  (no data)'}

---

### Module Inventory (key pipeline components)
Intelligence stack: market_data, macro_wire, macro_intelligence, morning_brief, scanner,
  earnings_intel, insider_intelligence, reddit_sentiment, portfolio_intelligence, sonnet_gate,
  attribution, divergence, trade_memory (ChromaDB), scratchpad
Options stack (A2): options_data, options_intelligence, options_builder, options_executor,
  options_state, order_executor_options
Weekly review: 11-agent pipeline (4 batch + CTO + Strategy Director + 4 parallel + Narrative + Final)
Scheduler: 24/7 loop, 5-min market / 15-min extended / 30-min overnight cycles

---

### Architecture Notes
- Account 1: 4-stage pipeline (Regime→Signal→Scratchpad→Decision→Execution)
- Account 2: Options pipeline with IV-first strategy + 4-way debate, 90s offset after A1
- All external calls non-fatal; exceptions caught at WARNING level
- Prompt caching on all system prompts (5-min TTL, aligns with market cycle)
- VPS: DigitalOcean 2GB RAM, $12/month

Produce your technical audit in markdown. Be specific: name modules, cite costs, propose exact changes."""


# ── Main review orchestrator ──────────────────────────────────────────────────

def run_review(emergency: bool = False, reason: str = "") -> str:
    """
    Run all 11 agents sequentially, write the markdown report, update
    strategy_config.json, send SMS, and return the report file path.

    emergency=True: bypass day-of-week gate (called from board_meeting.sh or
    --emergency CLI flag), prepend EMERGENCY SESSION header, save to
    data/reports/emergency_review_{YYYYMMDD_HHMM}.md.
    """
    now       = datetime.now()
    today_str = now.strftime("%Y-%m-%d")
    if emergency:
        log.info("[BOARD] Emergency session triggered: %s", reason or "no reason given")
        session_label = f"EMERGENCY SESSION — {reason}" if reason else "EMERGENCY SESSION"
        print(f"\n{'=' * 60}")
        print(f"  {session_label}")
        print(f"  {today_str}  {now.strftime('%H:%M')}")
        print(f"{'=' * 60}\n")
    else:
        print(f"\n{'=' * 60}")
        print(f"  WEEKLY REVIEW — {today_str}")
        print(f"{'=' * 60}\n")

    # ── Shared data gathering ─────────────────────────────────────────────────
    log.info("Gathering data for weekly review...")

    decisions_str   = mem.get_recent_decisions_str(20)
    perf_summary    = mem.get_performance_summary()
    ticker_stats    = mem.get_ticker_stats()
    journal_records = _read_journal_last_7days()
    log_tail_200    = _read_log_tail(200)
    log_tail_500    = _read_log_tail(500)
    decisions_raw   = _load_decisions_raw()
    strategy_cfg    = _load_strategy_config()
    vector_stats    = trade_memory.get_collection_stats()

    try:
        report_data = rpt.generate_report()
    except Exception as exc:
        log.warning("generate_report() failed (Alpaca may be unreachable): %s", exc)
        report_data = {}

    # Pattern learning watchlist data
    pattern_watchlist_data = {}
    pattern_watchlist_str  = "{}"
    try:
        from memory import _load_pattern_watchlist  # noqa: PLC0415
        pattern_watchlist_data = _load_pattern_watchlist()
        pattern_watchlist_str  = json.dumps(pattern_watchlist_data, indent=2)
    except Exception:
        pass

    # Cost data (needed for CTO agent and Phase 2 context)
    costs_data: dict = {}
    try:
        if _COSTS_FILE.exists():
            costs_data = json.loads(_COSTS_FILE.read_text())
    except Exception:
        pass

    # Macro wire data for the week
    macro_wire_sig_events = []
    macro_wire_str = "(none)"
    try:
        from pathlib import Path as _Path
        sig_path = _Path(__file__).parent / "data" / "macro_wire" / "significant_events.jsonl"
        if sig_path.exists():
            cutoff_date = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            for line in sig_path.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("ts", "")[:10] >= cutoff_date:
                        macro_wire_sig_events.append(rec)
                except Exception:
                    pass
        if macro_wire_sig_events:
            macro_wire_str = json.dumps(macro_wire_sig_events[-30:], indent=2)
    except Exception:
        pass

    # Daily conviction file (signal scorer accuracy)
    daily_conviction_str = "(none)"
    try:
        from pathlib import Path as _Path2
        conv_path = _Path2(__file__).parent / "data" / "market" / "daily_conviction.json"
        if conv_path.exists():
            daily_conviction_str = conv_path.read_text()[:2000]
    except Exception:
        pass

    # ── Build all agent inputs (independent — can run in parallel via batch) ───
    ticker_stats_str       = json.dumps(ticker_stats, indent=2) if ticker_stats else "{}"
    perf_str               = json.dumps(perf_summary, indent=2)
    global_indices_history = _load_global_indices_history(days=7)

    agent1_input = f"""## WEEKLY QUANT REVIEW INPUT

### Last 20 Decisions (newest first)
{decisions_str}

### Performance Breakdown by Category
```json
{perf_str}
```

### Per-Ticker Stats (all-time from memory)
```json
{ticker_stats_str}
```

### Global Session Handoff — Last 7 Days (Asia/Europe/US Futures daily snapshot)
{global_indices_history}

When reviewing the above global indices history:
- Did the bot correctly interpret global session signals in the cycles that followed?
- Were there days where Asia/Europe data predicted US moves accurately?
- Is the session bias (bullish/bearish/mixed) from global indices correlating with actual trade outcomes?

Please analyze signal quality, timing patterns, sector/strategy performance, and global session signal interpretation. Provide your findings as a markdown section with 3-5 specific recommendations.

Also analyze:
- Regime classifier accuracy: when regime_score > 70, did markets trend directionally?
  When constraints were flagged, did they prove relevant to outcomes?
- Signal scorer accuracy: did top_3 symbols outperform this week? Which signal types
  had the highest correlation with actual profitable trades?
- Macro wire accuracy: did significant_events precede meaningful market moves?
  Which keyword categories (critical/high/medium) were most predictive?
- ORB accuracy: did HIGH conviction ORB candidates actually break out from the range?
  What was the approximate success rate?

### PATTERN LEARNING WATCHLIST (this week's observations)
```json
{pattern_watchlist_str}
```

### MACRO WIRE SIGNIFICANT EVENTS (last 7 days)
```json
{macro_wire_str[:3000]}
```

### SIGNAL SCORER DAILY CONVICTION LOG
```
{daily_conviction_str}
```

For each symbol on the Pattern Learning Watchlist:
- Review all observations added this week
- Is a clear pattern emerging? Are re_entry_conditions well-defined?
- Should any symbol graduate back to the active watchlist?
- Suggest updated emerging_pattern and re_entry_conditions if applicable.

Please include pattern watchlist analysis in your report section.

### MODULE ATTRIBUTION (last 7 days)
{_attr_text}

### DIVERGENCE REPORT (last 7 days)
{_div_text}
"""

    # Attribution summary for Agent 1
    _attr_text = "(attribution data not yet available)"
    try:
        import sys as _sys
        _sys.path.insert(0, str(_Path(__file__).parent))
        from attribution import get_attribution_summary  # noqa: PLC0415
        _attr_summary = get_attribution_summary(days_back=7)
        if _attr_summary.get("total_decisions", 0) > 0:
            _gate_eff = _attr_summary["gate_efficiency"]
            _mod_lines = "\n".join(
                f"  {k}: {v:.1%}"
                for k, v in _attr_summary["module_usage_pct"].items()
            )
            _trig_sorted = sorted(
                _attr_summary["trigger_distribution"].items(),
                key=lambda x: -x[1],
            )[:5]
            _trig_lines = "\n".join(f"  {k}: {v}" for k, v in _trig_sorted)
            _attr_text = (
                f"Total decisions: {_attr_summary['total_decisions']}\n"
                f"Total trades: {_attr_summary['total_trades']}\n\n"
                f"Gate efficiency:\n"
                f"  Skip rate: {_gate_eff['skip_rate']:.1%}\n"
                f"  Compact rate: {_gate_eff['compact_rate']:.1%}\n"
                f"  Full rate: {_gate_eff['full_rate']:.1%}\n\n"
                f"Module usage:\n{_mod_lines}\n\n"
                f"Top triggers:\n{_trig_lines}"
            )
        else:
            _attr_text = _attr_summary.get("note", "No attribution data yet")
    except Exception as _attr_err:
        _attr_text = f"(attribution unavailable: {_attr_err})"

    # Rebuild agent1_input with attribution text substituted
    agent1_input = agent1_input.replace("{_attr_text}", _attr_text)

    # Divergence summary for Agent 1
    _div_text = "(divergence data not yet available)"
    try:
        from divergence import get_divergence_summary  # noqa: PLC0415
        _div_summary = get_divergence_summary(days_back=7)
        _div_text = (
            f"Total events: {_div_summary.get('total_events', 0)}\n"
            f"Halt events: {_div_summary.get('halt_events', 0)}\n"
            f"De-risk events: {_div_summary.get('de_risk_events', 0)}\n"
            f"By type: {_div_summary.get('by_type', {})}\n"
            f"By severity: {_div_summary.get('by_severity', {})}"
        )
    except Exception as _div_err:
        _div_text = f"(divergence unavailable: {_div_err})"
    agent1_input = agent1_input.replace("{_div_text}", _div_text)

    # Extract REJECTED and drawdown lines from log
    rejected_lines = [ln for ln in log_tail_200.splitlines()
                      if any(kw in ln for kw in ("REJECTED", "DRAWDOWN", "drawdown", "halt", "HALT"))]
    rejected_block = "\n".join(rejected_lines) if rejected_lines else "(none found)"

    journal_by_status: dict[str, int] = defaultdict(int)
    journal_by_session: dict[str, int] = defaultdict(int)
    for rec in journal_records:
        journal_by_status[rec.get("status", rec.get("event", "unknown"))] += 1
        journal_by_session[rec.get("session", "unknown")] += 1

    agent2_input = f"""## WEEKLY RISK MANAGER REVIEW INPUT

### Recent Log — REJECTED / DRAWDOWN Events
```
{rejected_block}
```

### Trade Journal Summary (last 7 days)
- Total records: {len(journal_records)}
- By status/event: {dict(journal_by_status)}
- By session: {dict(journal_by_session)}

### Performance Summary from Memory
```json
{perf_str}
```

### Current strategy_config.json Parameters
```json
{json.dumps(strategy_cfg.get("parameters", {}), indent=2)}
```

Please audit risk controls, position sizing, drawdown exposure, stop-loss effectiveness, and PDT usage. Provide your findings as a markdown section with 3-5 specific parameter adjustments (include numeric values)."""

    exec_keywords = ("REJECTED", "SUBMITTED", "ERROR", "Cycle done in",
                     "submitted", "rejected", "error", "exception", "Exception")
    exec_lines = [ln for ln in log_tail_500.splitlines()
                  if any(kw in ln for kw in exec_keywords)]
    exec_block = "\n".join(exec_lines[-150:]) if exec_lines else "(none found)"

    session_counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    symbol_counts: dict[str, dict[str, int]]  = defaultdict(lambda: defaultdict(int))
    cycle_count = 0
    session_cycle_dist: dict[str, int] = defaultdict(int)
    for rec in journal_records:
        ev      = rec.get("event", "")
        status  = rec.get("status", "")
        session = rec.get("session", "unknown")
        symbol  = rec.get("symbol", "")
        if ev == "cycle_decision":
            cycle_count += 1
            session_cycle_dist[session] += 1
        elif status in ("submitted", "rejected", "error"):
            session_counts[session][status] += 1
            if symbol:
                symbol_counts[symbol][status] += 1

    agent3_input = f"""## WEEKLY EXECUTION ENGINEER REVIEW INPUT

### Log Excerpt — Execution Events (last 500 lines filtered)
```
{exec_block}
```

### Trade Journal — 7-Day Execution Summary
- Total cycles run: {cycle_count}
- Cycles by session: {dict(session_cycle_dist)}
- Orders by session+status: {json.dumps(dict(session_counts), indent=2)}
- Orders by symbol+status: {json.dumps(dict(symbol_counts), indent=2)}

### Full 7-Day Journal Record Count: {len(journal_records)}

Please analyze order fill quality, rejection reasons, timing patterns, and API reliability. Provide your findings as a markdown section with 3-5 concrete improvements."""

    last_14 = decisions_raw[-14:] if len(decisions_raw) >= 14 else decisions_raw
    resolved_wins   = 0
    resolved_losses = 0
    total_pnl       = 0.0
    pending_count   = 0
    for dec in last_14:
        for action in dec.get("actions", []):
            outcome = action.get("outcome")
            pnl     = action.get("pnl") or 0.0
            if outcome == "win":
                resolved_wins += 1
                total_pnl += pnl
            elif outcome == "loss":
                resolved_losses += 1
                total_pnl += pnl
            else:
                pending_count += 1

    resolved_total = resolved_wins + resolved_losses
    resolved_wr    = (resolved_wins / resolved_total * 100) if resolved_total > 0 else 0.0
    avg_pnl        = (total_pnl / resolved_total) if resolved_total > 0 else 0.0

    report_snippet = {
        "closed_trades":  report_data.get("closed_trades", 0),
        "win_rate":       report_data.get("win_rate", 0),
        "avg_win":        report_data.get("avg_win", 0),
        "avg_loss":       report_data.get("avg_loss", 0),
        "profit_factor":  report_data.get("profit_factor", 0),
        "day_pl":         report_data.get("day_pl", 0),
        "all_time_pl":    report_data.get("all_time_pl", 0),
    } if report_data else {}

    # ── Agent 4 data: signal backtest + shadow lane ───────────────────────────
    try:
        import signal_backtest as _signal_backtest
        import shadow_lane as _shadow_lane
        _bt_result    = _signal_backtest.run_signal_backtest(lookback_days=30)
        _bt_report    = _signal_backtest.format_backtest_report(_bt_result)
        _signal_backtest.save_backtest_results(_bt_result)
        _shadow_stats = _shadow_lane.get_shadow_stats(lookback_days=7)
    except Exception as _bt_err:
        log.warning("[REVIEW] backtest/shadow failed: %s", _bt_err)
        _bt_report    = "Backtest unavailable this week."
        _shadow_stats = {"total": 0, "note": "unavailable"}

    agent4_input = f"""## WEEKLY BACKTEST ANALYST REVIEW INPUT

### Vector Memory (ChromaDB) Stats
```json
{json.dumps(vector_stats, indent=2)}
```

### Last 14 Decisions — Outcome Analysis
- Decisions analyzed: {len(last_14)}
- Resolved wins: {resolved_wins}
- Resolved losses: {resolved_losses}
- Still pending: {pending_count}
- Resolved win rate: {resolved_wr:.1f}%
- Total resolved P&L: ${total_pnl:+.2f}
- Average resolved P&L per trade: ${avg_pnl:+.2f}

### Live Report Data (from Alpaca)
```json
{json.dumps(report_snippet, indent=2)}
```

### Full Memory Performance Summary
```json
{perf_str}
```

{_bt_report}

### Shadow Lane Stats (last 7 days)
```json
{json.dumps(_shadow_stats, indent=2)}
```

Please analyze decision quality, compare live results to expectations, identify divergence patterns, and assess signal alpha from the backtest. For any symbol with has_alpha=true, note whether the bot acted on it. Provide your findings as a markdown section with 3-5 insights."""

    # ── Agents 1-4: try batch first, fall back to sequential ─────────────────
    agent_inputs_1_to_4 = [
        (_SYSTEM_AGENT1, agent1_input, "1-QuantAnalyst"),
        (_SYSTEM_AGENT2, agent2_input, "2-RiskManager"),
        (_SYSTEM_AGENT3, agent3_input, "3-ExecutionEngineer"),
        (_SYSTEM_AGENT4, agent4_input, "4-BacktestAnalyst"),
    ]

    print("[1-4] Running agents 1-4 via Batch API (50% discount)...")
    batch_results = _run_agents_via_batch(agent_inputs_1_to_4)

    if len(batch_results) == 4:
        agent1_output, agent2_output, agent3_output, agent4_output = batch_results
        log.info("Agents 1-4 completed via batch API")
    else:
        log.info("Batch failed — running agents 1-4 sequentially")
        print("[1/11] Running Quant Analyst (sequential)...")
        agent1_output = _call_claude(_SYSTEM_AGENT1, agent1_input, "1-QuantAnalyst")
        print("[2/11] Running Risk Manager (sequential)...")
        agent2_output = _call_claude(_SYSTEM_AGENT2, agent2_input, "2-RiskManager")
        print("[3/11] Running Execution Engineer (sequential)...")
        agent3_output = _call_claude(_SYSTEM_AGENT3, agent3_input, "3-ExecutionEngineer")
        print("[4/11] Running Backtest Analyst (sequential)...")
        agent4_output = _call_claude(_SYSTEM_AGENT4, agent4_input, "4-BacktestAnalyst")

    # ── Agent 5: CTO (technical audit — needs all 4 reports) ─────────────────
    print("[5/11] Running CTO (technical audit)...")
    _cto_phase1 = {
        "agent1_quant":     agent1_output,
        "agent2_risk":      agent2_output,
        "agent3_execution": agent3_output,
        "agent4_backtest":  agent4_output,
    }
    agent5_cto_output = _call_claude(
        _SYSTEM_AGENT5,
        _build_agent5_cto_input({"costs_data": costs_data}, _cto_phase1),
        "5-CTO",
    )

    # ── Agent 6: Strategy Director (always sequential — needs all 4 reports) ──
    print("[6/11] Running Strategy Director (draft)...")

    # Load director memo history for continuity across weeks
    _director_history = _load_director_memo_history()
    _history_text     = _format_director_history_for_prompt(_director_history)

    agent6_input = f"""## WEEKLY STRATEGY DIRECTOR SYNTHESIS INPUT

You have received reports from four specialist analysts. Synthesize their findings and produce a strategic memo and parameter update JSON for the coming week.

---

### REPORT 1: QUANT ANALYST
{agent1_output}

---

### REPORT 2: RISK MANAGER
{agent2_output}

---

### REPORT 3: EXECUTION ENGINEER
{agent3_output}

---

### REPORT 4: BACKTEST ANALYST
{agent4_output}

---

### CURRENT strategy_config.json
```json
{json.dumps(strategy_cfg, indent=2)}
```

---

### YOUR PRIOR RECOMMENDATIONS (last 4 weeks)
{_history_text}

---

## YOUR OUTPUT FORMAT

Provide two things:

1. A strategy memo starting with the heading `## STRATEGY DIRECTOR WEEKLY MEMO`

2. A JSON block (in ```json ... ``` fences) with this exact structure:
```json
{{
  "active_strategy": "<strategy_name>",
  "parameter_adjustments": {{
    "momentum_weight": <0.0 to 1.0>,
    "mean_reversion_weight": <0.0 to 1.0>,
    "news_sentiment_weight": <0.0 to 1.0>,
    "cross_sector_weight": <0.0 to 1.0>,
    "min_confidence_threshold": "low|medium|high",
    "max_positions": <integer>,
    "sector_rotation_bias": "<sector name or neutral>",
    "stop_loss_pct_core": <float, e.g. 0.03>,
    "take_profit_multiple": <float, e.g. 2.0>
  }},
  "watchlist_updates": {{
    "SYMBOL": {{
      "emerging_pattern": "<description or empty string>",
      "re_entry_conditions": ["<condition 1>", "<condition 2>"],
      "graduate": true,
      "notes": "<why graduating or not>"
    }}
  }},
  "signal_weights_recommended": {{
    "congressional": "high|medium|low|ignore",
    "form4_insider": "high|medium|low|ignore",
    "reddit_sentiment": "high|medium|low|ignore",
    "orb_breakout": "high|medium|low|ignore",
    "macro_wire": "high|medium|low|ignore",
    "earnings_intel": "high|medium|low|ignore"
  }},
  "director_notes": "<2-3 paragraph strategy memo for next week>"
}}
```

Be specific. Every parameter value must be a concrete number or string, not a placeholder.
For watchlist_updates: only include symbols that are currently in the pattern learning watchlist.
If no updates needed, set watchlist_updates to {{}}.
For signal_weights_recommended: based on this week's accuracy data, suggest weight levels."""

    agent6_output = _call_claude(_SYSTEM_AGENT6, agent6_input, "6-StrategyDirector")

    # ── Save director memo to rolling history ─────────────────────────────────
    try:
        _save_director_memo({
            "week":                       today_str,
            "memo_summary":               agent6_output[:500],
            "config_changes":             {},
            "key_recommendations":        _extract_recommendations(agent6_output),
            "regime_view":                _extract_regime_view(agent6_output),
            "real_money_readiness_score": _extract_cto_score(agent5_cto_output),
        })
    except Exception as _memo_exc:
        log.warning("Director memo save failed (non-fatal): %s", _memo_exc)

    # ── Parse Agent 6 JSON ────────────────────────────────────────────────────
    params_update = _extract_json_block(agent6_output)
    if params_update:
        log.info("Agent 6 JSON parsed successfully")
    else:
        log.warning("Agent 6 JSON parse failed — strategy_config.json will not be updated")

    # ── Update strategy_config.json ───────────────────────────────────────────
    active_strategy  = None
    director_notes   = None

    if params_update:
        active_strategy  = params_update.get("active_strategy")
        director_notes   = params_update.get("director_notes")
        param_adjustments = params_update.get("parameter_adjustments", {})

        # Reload fresh copy before merging (avoid clobbering concurrent writes)
        config = _load_strategy_config()
        if "parameters" not in config or not isinstance(config.get("parameters"), dict):
            config["parameters"] = {}

        config["generated_at"]  = datetime.now().isoformat()
        config["generated_by"]  = "weekly_review"
        if active_strategy:
            config["active_strategy"] = active_strategy
        if director_notes:
            config["director_notes"] = director_notes

        # Merge only the keys that exist in the current config parameters
        # to avoid injecting unexpected keys from the model
        for key, value in param_adjustments.items():
            config["parameters"][key] = value

        # Save signal weights if provided
        signal_weights = params_update.get("signal_weights_recommended", {})
        if signal_weights:
            config["signal_weights"] = signal_weights

        _save_strategy_config(config)

        # Apply watchlist updates from Strategy Director
        watchlist_updates = params_update.get("watchlist_updates", {})
        if watchlist_updates:
            try:
                from memory import update_pattern_watchlist_from_review  # noqa: PLC0415
                update_pattern_watchlist_from_review(watchlist_updates)
                log.info("Pattern learning watchlist updated by Strategy Director: %s",
                         list(watchlist_updates.keys()))
            except Exception as _wl_exc:
                log.warning("Watchlist update from review failed: %s", _wl_exc)
    else:
        # Graceful fallback: update metadata only
        config = _load_strategy_config()
        config["generated_at"] = datetime.now().isoformat()
        config["generated_by"] = "weekly_review"
        _save_strategy_config(config)
    # agent6_output is the Strategy Director draft (referenced below in all_outputs)


    # ── Phase 2: Run agents 7-10 in parallel ─────────────────────────────────
    print("[Phase 2] Building context and running agents 7-10 in parallel...")
    try:
        review_context = _build_review_context()
    except Exception as _ctx_exc:
        log.warning("_build_review_context failed: %s", _ctx_exc)
        review_context = {}
    phase1_outputs = {
        "agent1_quant":     agent1_output,
        "agent2_risk":      agent2_output,
        "agent3_execution": agent3_output,
        "agent4_backtest":  agent4_output,
        "agent5_cto":       agent5_cto_output,
        "agent6_draft":     agent6_output,
    }
    try:
        phase2_outputs = _run_phase2_agents(review_context, phase1_outputs)
    except Exception as _p2_exc:
        log.warning("Phase 2 failed: %s — using Phase 1 only", _p2_exc)
        phase2_outputs = {
            "agent7":  "(unavailable)",
            "agent8":  "(unavailable)",
            "agent9":  "(unavailable)",
            "agent10": "(unavailable)",
            "agent11": "(unavailable)",
        }

    all_outputs = {**phase1_outputs, **phase2_outputs}

    print("[Phase 3a] Running Agent 11 Narrative Director...")
    try:
        agent11_output = _run_agent11_narrative(review_context, all_outputs)
    except Exception as _a11_exc:
        log.warning("Agent 11 failed: %s", _a11_exc)
        agent11_output = "(unavailable)"
    all_outputs["agent11"] = agent11_output

    # Phase 3b: Agent 6 final — re-runs with ALL 11 agent reports
    print("[Phase 3b] Running Agent 6 Strategy Director (final synthesis)...")
    try:
        agent6_final = _call_claude(
            _SYSTEM_AGENT6,
            _build_agent6_final_input(review_context, all_outputs),
            "6-StrategyDirector-Final",
        )
        final_params = _extract_json_block(agent6_final)
        if final_params:
            config = _load_strategy_config()
            if "parameters" not in config or not isinstance(config.get("parameters"), dict):
                config["parameters"] = {}
            config["generated_at"] = datetime.now().isoformat()
            config["generated_by"] = "weekly_review_final"
            if final_params.get("active_strategy"):
                config["active_strategy"] = final_params["active_strategy"]
                active_strategy = final_params["active_strategy"]
            if final_params.get("director_notes"):
                config["director_notes"] = final_params["director_notes"]
                director_notes = final_params["director_notes"]
            for _k, _v in final_params.get("parameter_adjustments", {}).items():
                config["parameters"][_k] = _v
            _sw_final = final_params.get("signal_weights_recommended", {})
            if _sw_final:
                config["signal_weights"] = _sw_final
            _save_strategy_config(config)
            log.info("Strategy config updated from Agent 6 final synthesis")
        else:
            log.warning("Agent 6 final JSON parse failed — using draft config")
    except Exception as _a6f_exc:
        log.warning("Phase 3 Agent 6 Final failed: %s", _a6f_exc)
        agent6_final = "(unavailable)"
    all_outputs["agent6_final"] = agent6_final

    _apply_roadmap_updates(phase2_outputs.get("agent9", ""))
    _save_weekly_report(all_outputs, agent6_final)

    # ── Build and save markdown report ───────────────────────────────────────
    _REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    if emergency:
        ts_stamp        = now.strftime("%Y%m%d_%H%M")
        report_filename = f"emergency_review_{ts_stamp}.md"
    else:
        report_filename = f"weekly_review_{today_str}.md"
    report_path = _REPORTS_DIR / report_filename

    if emergency:
        _emergency_header = (
            f"**Triggered:** {now.strftime('%Y-%m-%d %H:%M')}  "
            f"**Reason:** {reason or 'Manual emergency session'}"
        )
    else:
        _emergency_header = ""
    _report_title = (
        f"Emergency Board Meeting — {today_str}"
        if emergency else
        f"Trading Bot Weekly Review — {today_str}"
    )
    md_sections = [
        f"# {_report_title}",
        "",
        *(["---", "", _emergency_header, "", "---", ""] if emergency else []),
        "## Agent 1: Quant Analyst",
        "",
        agent1_output,
        "",
        "## Agent 2: Risk Manager",
        "",
        agent2_output,
        "",
        "## Agent 3: Execution Engineer",
        "",
        agent3_output,
        "",
        "## Agent 4: Backtest Analyst",
        "",
        agent4_output,
        "",
        "## Agent 5: CTO (Technical Audit)",
        "",
        agent5_cto_output,
        "",
        "## Agent 6: Strategy Director (Draft)",
        "",
        agent6_output,
        "",
        "## Agent 7: Market Intelligence Researcher",
        "",
        phase2_outputs.get("agent7", "(not run)"),
        "",
        "## Agent 8: CFO / Cost Analyst",
        "",
        phase2_outputs.get("agent8", "(not run)"),
        "",
        "## Agent 9: Product Manager",
        "",
        phase2_outputs.get("agent9", "(not run)"),
        "",
        "## Agent 10: Compliance Auditor",
        "",
        phase2_outputs.get("agent10", "(not run)"),
        "",
        "## Agent 11: Narrative Director",
        "",
        agent11_output,
        "",
        "## Agent 6: Strategy Director (Final Synthesis)",
        "",
        agent6_final,
    ]
    report_md = "\n".join(md_sections)

    try:
        report_path.write_text(report_md, encoding="utf-8")
        log.info("Weekly review saved to %s", report_path)
    except Exception as exc:
        log.error("Failed to save weekly review: %s", exc)

    # ── Send SMS ──────────────────────────────────────────────────────────────
    sms_strategy = active_strategy or strategy_cfg.get("active_strategy", "unknown")
    sms_notes    = (director_notes or "No director notes parsed.")[:140]
    sms_message  = f"WEEKLY REVIEW COMPLETE: winner={sms_strategy} notes={sms_notes}"
    _send_sms(sms_message)

    # ── Console summary ───────────────────────────────────────────────────────
    print(f"\n{'=' * 60}")
    print(f"  WEEKLY REVIEW COMPLETE")
    print(f"  Active strategy : {sms_strategy}")
    print(f"  Report saved to : {report_path}")
    print(f"  Config updated  : {'yes' if params_update else 'metadata only (parse failed)'}")
    print(f"{'=' * 60}\n")

    log.info("Weekly review complete — strategy=%s  report=%s", sms_strategy, report_path)
    return str(report_path)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    _ap = argparse.ArgumentParser(description="BullBearBot weekly/emergency review")
    _ap.add_argument("--emergency", action="store_true",
                     help="Run as emergency session (bypasses day-of-week gate)")
    _ap.add_argument("--reason", default="",
                     help="Reason for emergency session (logged + report header)")
    _args = _ap.parse_args()
    path = run_review(emergency=_args.emergency, reason=_args.reason)
    print(f"\nReport saved to: {path}")
