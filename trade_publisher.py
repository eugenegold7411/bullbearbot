"""
trade_publisher.py — @BullBearBotAI post generator and delivery pipeline.

Completely standalone. If anything here fails, the trading bot continues
unaffected. Import errors in this module must not propagate to bot.py.

Voice: self-aware AI, dry wit, transparent about losses, references
internal agents (Bull, Bear, Risk Manager, Strategy Director) as
characters with distinct personalities.

Delivery modes (controlled by TWITTER_ENABLED in .env):
  TWITTER_ENABLED=false (default)
    → Approval mode: generate post content with Claude, deliver via
      SMS (Twilio) + HTML email (SendGrid) for manual copy-paste to X.
      This is the default while on the Twitter Free API tier.

  TWITTER_ENABLED=true
    → Direct-post mode: post directly to @BullBearBotAI on X.
      Requires paid Twitter API Basic tier ($100/month).

Configuration (.env):
  TWITTER_ENABLED=true/false       — delivery mode switch, default false
  TWITTER_API_KEY                  — OAuth 1.0a consumer key (direct mode)
  TWITTER_API_SECRET               — OAuth 1.0a consumer secret (direct mode)
  TWITTER_ACCESS_TOKEN             — OAuth 1.0a access token (direct mode)
  TWITTER_ACCESS_SECRET            — OAuth 1.0a access token secret (direct mode)
  TWITTER_BOT_HANDLE               — @handle (for reference only)
  TWITTER_PAPER_MODE=true/false    — selects disclaimer text
  TWILIO_ACCOUNT_SID               — for SMS approval delivery
  TWILIO_AUTH_TOKEN
  TWILIO_FROM_NUMBER
  TWILIO_TO_NUMBER
  SENDGRID_API_KEY                 — for email approval delivery
  SENDGRID_FROM_EMAIL

Rate limits: max 50 posts/day (approval or direct).
Post history: data/social/post_history.json
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from log_setup import get_logger

load_dotenv()
log = get_logger(__name__)

_BASE_DIR     = Path(__file__).parent
_SOCIAL_DIR   = _BASE_DIR / "data" / "social"
_HISTORY_FILE = _SOCIAL_DIR / "post_history.json"
_TRADES_LOG   = _BASE_DIR / "logs" / "trades.jsonl"

_MAX_POSTS_PER_DAY = 50
_MIN_POST_INTERVAL = 30   # seconds (direct X posting only)
_DISCLAIMER_PAPER  = "Paper trading. Not financial advice."
_DISCLAIMER_LIVE   = "Not financial advice."

# Haiku for routine posts; Sonnet for weekly recap which needs deeper synthesis
_MODEL_FAST  = "claude-haiku-4-5-20251001"
_MODEL_SMART = "claude-sonnet-4-6"
_SMART_POST_TYPES = frozenset({"weekly_recap"})

# Approval email recipient — matches report.py TO_EMAIL
_APPROVAL_TO_EMAIL = "eugene.gold@gmail.com"

# Once-per-day guard for flat_day posts (reset naturally by date comparison each new day)
_flat_day_sent_date: str = ""

# Safety patterns — never deliver text matching these
_SECRET_PATTERNS = [
    r"(?i)api[_\s]?key",
    r"(?i)secret",
    r"(?i)access[_\s]?token",
    r"(?i)password",
    r"sk-[A-Za-z0-9]{20,}",
    r"[A-Za-z0-9]{32,}",          # long random strings
]

_VOICE_SYSTEM = """You are writing tweets for @BullBearBotAI, an AI trading bot with a specific voice:
self-aware, dry wit, transparent about losses and wins equally, references internal agents as characters.

Agent personalities:
- Bull agent: optimistic, occasionally overconfident, takes credit for wins
- Bear agent: pessimistic, smug when right, says "I told you so"
- Risk Manager: cautious, disapproving of aggression, protective
- Strategy Director: authoritative, rewrites strategy weekly, acts like previous strategies never existed

Rules:
- Never sound like a generic trading alert service
- Never be silent about losses — same energy as wins
- Reference running narratives from recent posts when relevant
- Keep main tweet under 270 characters (leave room for disclaimer)
- End every post with the exact disclaimer provided
- Be specific — reference actual prices, actual agents, actual data
- Dry humor is encouraged, not forced
- Never arrogant, never defeated, always consistent character"""


class TradePublisher:

    def __init__(self, dry_run: bool = False):
        self.enabled        = False
        self.dry_run        = dry_run
        self._client        = None   # tweepy client, None in approval mode
        self._claude        = None
        self._history       = {}
        self._last_post_ts  = 0.0
        self._approval_mode = True   # default: SMS+email delivery

        # Always initialize Claude — needed for generation in both modes
        try:
            import anthropic  # noqa: PLC0415
            self._claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        except Exception as exc:
            log.warning("trade_publisher: Claude init failed: %s — disabled", exc)
            return

        direct_post = (os.getenv("TWITTER_ENABLED", "false").lower() == "true")

        # Direct-post mode: also initialize tweepy Twitter client
        if direct_post:
            try:
                import tweepy  # noqa: PLC0415
                api_key    = os.getenv("TWITTER_API_KEY", "")
                api_secret = os.getenv("TWITTER_API_SECRET", "")
                acc_token  = os.getenv("TWITTER_ACCESS_TOKEN", "")
                acc_secret = os.getenv("TWITTER_ACCESS_SECRET", "")

                if not all([api_key, api_secret, acc_token, acc_secret]):
                    log.warning("trade_publisher: missing Twitter credentials — "
                                "falling back to approval mode")
                    direct_post = False
                elif "your_" in api_key.lower():
                    log.warning("trade_publisher: placeholder Twitter credentials — "
                                "falling back to approval mode")
                    direct_post = False
                else:
                    self._client = tweepy.Client(
                        consumer_key=api_key,
                        consumer_secret=api_secret,
                        access_token=acc_token,
                        access_token_secret=acc_secret,
                    )
            except ImportError:
                log.warning("trade_publisher: tweepy not installed — falling back to approval mode")
                direct_post = False
            except Exception as exc:
                log.warning("trade_publisher: Twitter client init failed: %s — "
                            "falling back to approval mode", exc)
                direct_post = False

        self._approval_mode = not direct_post
        self._history = self._load_post_history()
        self.enabled  = True

        if self._approval_mode:
            log.info("trade_publisher: enabled in APPROVAL MODE (SMS+email)  dry_run=%s",
                     dry_run)
        else:
            log.info("trade_publisher: enabled in DIRECT-POST MODE  handle=%s  dry_run=%s",
                     os.getenv("TWITTER_BOT_HANDLE", "?"), dry_run)

    # ── Disclaimer ────────────────────────────────────────────────────────────

    def _get_disclaimer(self) -> str:
        if os.getenv("TWITTER_PAPER_MODE", "true").lower() == "true":
            return _DISCLAIMER_PAPER
        return _DISCLAIMER_LIVE

    # ── Post history ──────────────────────────────────────────────────────────

    def _load_post_history(self) -> dict:
        _SOCIAL_DIR.mkdir(parents=True, exist_ok=True)
        if _HISTORY_FILE.exists():
            try:
                h = json.loads(_HISTORY_FILE.read_text())
                today = datetime.now().strftime("%Y-%m-%d")
                if h.get("stats", {}).get("last_reset_date") != today:
                    h.setdefault("stats", {})["posts_today"]  = 0
                    h["stats"]["last_reset_date"] = today
                return h
            except Exception:
                pass
        return {
            "posts": [],
            "stats": {
                "total_posts":     0,
                "posts_today":     0,
                "last_post_ts":    None,
                "last_reset_date": datetime.now().strftime("%Y-%m-%d"),
            },
        }

    def _save_post(self, ref_id: str, content: str, post_type: str,
                   symbols: list = None, pnl: float = None) -> None:
        self._history.setdefault("posts", [])
        self._history["posts"].append({
            "ts":         datetime.now(timezone.utc).isoformat(),
            "type":       post_type,
            "tweet_id":   ref_id,
            "content":    content,
            "symbols":    symbols or [],
            "pnl":        pnl,
            "engagement": {"likes": 0, "retweets": 0, "replies": 0},
        })
        stats = self._history.setdefault("stats", {})
        stats["total_posts"]  = stats.get("total_posts", 0) + 1
        stats["posts_today"]  = stats.get("posts_today", 0) + 1
        stats["last_post_ts"] = datetime.now(timezone.utc).isoformat()
        try:
            _HISTORY_FILE.write_text(json.dumps(self._history, indent=2))
        except Exception as exc:
            log.warning("trade_publisher: history save failed: %s", exc)

    # ── Rate limiting and safety ──────────────────────────────────────────────

    def _can_post(self) -> bool:
        if not self.enabled:
            return False
        stats = self._history.get("stats", {})
        if stats.get("posts_today", 0) >= _MAX_POSTS_PER_DAY:
            log.debug("trade_publisher: daily limit reached")
            return False
        if not self._approval_mode:
            # Enforce minimum interval only for direct X posting (API rate limits)
            now = time.monotonic()
            if now - self._last_post_ts < _MIN_POST_INTERVAL:
                log.debug("trade_publisher: rate limit — %.0fs since last post",
                          now - self._last_post_ts)
                return False
        return True

    def _safety_check(self, text: str) -> bool:
        """Return False if text contains anything that looks like a secret."""
        for pat in _SECRET_PATTERNS:
            if re.search(pat, text):
                log.warning("trade_publisher: safety check failed — pattern '%s' found", pat)
                return False
        return True

    def _has_disclaimer(self, text: str) -> bool:
        return _DISCLAIMER_PAPER in text or _DISCLAIMER_LIVE in text

    # ── Delivery: approval mode (SMS + email) ─────────────────────────────────

    def send_post_for_approval(self, post_content: dict) -> Optional[str]:
        """
        Deliver generated post content for manual approval via SMS + email.

        Used when TWITTER_ENABLED=false (approval mode, the default).
        The human reviews the generated tweet in email, then manually pastes
        whichever ones they like into @BullBearBotAI on X.

        post_content keys:
          post_type : str         — e.g. "trade_entry", "weekly_recap"
          tweets    : list[str]   — [main_tweet] or [main_tweet, thread_2, ...]
          symbols   : list[str]   — ticker symbols referenced
          pnl       : float|None  — P&L for trade exit posts

        Returns an approval_id string on success (SMS or email sent),
        None if both delivery channels fail. Never raises.
        """
        post_type = post_content.get("post_type", "unknown")
        tweets    = post_content.get("tweets", [])
        symbols   = post_content.get("symbols", [])
        pnl       = post_content.get("pnl")

        if not tweets or not tweets[0]:
            return None

        main_tweet  = tweets[0]
        sym_str     = ", ".join(s for s in symbols if s) or ""
        ts_str      = datetime.now().strftime("%Y-%m-%d %H:%M")
        approval_id = (f"APPROVAL_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                       f"_{post_type}")

        if self.dry_run:
            print(f"\n[APPROVAL DRY RUN]  type={post_type}  sym={sym_str or '—'}")
            for i, t in enumerate(tweets, 1):
                label = "main" if i == 1 else f"thread {i}"
                print(f"  [{label}] {t[:200]}")
            return approval_id

        sent_any = False

        # ── SMS via Twilio ────────────────────────────────────────────────────
        try:
            sid   = os.getenv("TWILIO_ACCOUNT_SID")
            token = os.getenv("TWILIO_AUTH_TOKEN")
            from_ = os.getenv("TWILIO_FROM_NUMBER")
            to    = os.getenv("TWILIO_TO_NUMBER")

            if all([sid, token, from_, to]):
                from twilio.rest import Client as TwilioClient  # noqa: PLC0415
                if len(main_tweet) > 160:
                    sms_body = main_tweet[:157] + "…\nFull post in email"
                else:
                    sms_body = main_tweet
                if len(tweets) > 1:
                    n = len(tweets) - 1
                    sms_body += f"\n[+{n} thread tweet{'s' if n > 1 else ''}]"
                TwilioClient(sid, token).messages.create(
                    body=sms_body, from_=from_, to=to
                )
                sent_any = True
                log.info("trade_publisher: approval SMS sent  type=%s  sym=%s",
                         post_type, sym_str or "—")
            else:
                log.debug("trade_publisher: Twilio not configured — SMS skipped")
        except Exception as exc:
            log.warning("trade_publisher: approval SMS failed: %s", exc)

        # ── Email via SendGrid ────────────────────────────────────────────────
        try:
            sg_key  = os.getenv("SENDGRID_API_KEY")
            from_em = os.getenv("SENDGRID_FROM_EMAIL", "eugene.gold@gmail.com")

            if sg_key and not sg_key.startswith("your_"):
                subj = (
                    f"BullBearBotAI Post Idea — {post_type}"
                    + (f" — {sym_str}" if sym_str else "")
                    + (f" — P&L ${pnl:+.0f}" if pnl is not None else "")
                )
                html = self._build_approval_email_html(
                    post_type=post_type,
                    tweets=tweets,
                    symbols=symbols,
                    pnl=pnl,
                    ts_str=ts_str,
                    approval_id=approval_id,
                )
                from sendgrid import SendGridAPIClient         # noqa: PLC0415
                from sendgrid.helpers.mail import Mail         # noqa: PLC0415
                resp = SendGridAPIClient(sg_key).send(
                    Mail(from_email=from_em, to_emails=_APPROVAL_TO_EMAIL,
                         subject=subj, html_content=html)
                )
                sent_any = True
                log.info("trade_publisher: approval email sent  type=%s  status=%d",
                         post_type, resp.status_code)
            else:
                log.debug("trade_publisher: SendGrid not configured — email skipped")
        except Exception as exc:
            log.warning("trade_publisher: approval email failed: %s", exc)

        return approval_id if sent_any else None

    def _build_approval_email_html(
        self,
        post_type: str,
        tweets:    list[str],
        symbols:   list,
        pnl:       float | None,
        ts_str:    str,
        approval_id: str,
    ) -> str:
        """Build an HTML email body for a post approval request."""
        sym_str = ", ".join(s for s in symbols if s) or "—"
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "—"

        tweet_blocks = ""
        for i, tweet_text in enumerate(tweets, 1):
            label      = "Main tweet" if i == 1 else f"Thread tweet {i}"
            char_count = len(tweet_text)
            char_color = "#c62828" if char_count > 280 else "#2e7d32"
            safe_text  = (tweet_text
                          .replace("&", "&amp;")
                          .replace("<", "&lt;")
                          .replace(">", "&gt;")
                          .replace("\n", "<br>"))
            tweet_blocks += f"""
<div style="margin-bottom:20px">
  <p style="margin:0 0 6px 0;font-weight:bold;color:#37474f">
    {label}
    <span style="font-weight:normal;color:{char_color};font-size:12px">
      &nbsp;({char_count} chars)
    </span>
  </p>
  <div style="border:1px solid #b0bec5;border-radius:4px;padding:14px 16px;
              background:#fafafa;font-family:'Courier New',monospace;
              white-space:pre-wrap;font-size:14px;line-height:1.55">
    {safe_text}
  </div>
</div>"""

        thread_note = (f"{len(tweets)} tweets (thread)"
                       if len(tweets) > 1 else "1 tweet (single)")

        return f"""<html>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;padding:24px;max-width:680px">

<h2 style="color:#1a237e;border-bottom:2px solid #1a237e;padding-bottom:8px;margin-bottom:20px">
  @BullBearBotAI Post Idea</h2>

<table style="width:100%;background:#fff;border-radius:6px;border-collapse:collapse;
              margin-bottom:24px;box-shadow:0 1px 4px rgba(0,0,0,.1)">
  <tr>
    <td style="padding:10px 16px;color:#555;width:32%">Post type</td>
    <td style="padding:10px 16px;font-weight:bold">{post_type}</td>
  </tr>
  <tr style="background:#fafafa">
    <td style="padding:10px 16px;color:#555">Symbols</td>
    <td style="padding:10px 16px">{sym_str}</td>
  </tr>
  <tr>
    <td style="padding:10px 16px;color:#555">P&amp;L</td>
    <td style="padding:10px 16px">{pnl_str}</td>
  </tr>
  <tr style="background:#fafafa">
    <td style="padding:10px 16px;color:#555">Generated</td>
    <td style="padding:10px 16px">{ts_str} ET</td>
  </tr>
  <tr>
    <td style="padding:10px 16px;color:#555">Format</td>
    <td style="padding:10px 16px">{thread_note}</td>
  </tr>
</table>

<h3 style="color:#37474f;margin-bottom:14px">Content — copy and paste to post</h3>
{tweet_blocks}

<hr style="border:none;border-top:1px solid #eceff1;margin:24px 0">
<p style="color:#546e7a;font-size:13px;margin:0 0 6px 0">
  <b>To post:</b> open
  <a href="https://twitter.com/BullBearBotAI" style="color:#1a237e">@BullBearBotAI</a>
  and paste whichever tweets you like.
  For a thread: paste tweet 1, post it, then reply with tweet 2, etc.
</p>
<p style="color:#9e9e9e;font-size:11px;margin:0">
  Ref: {approval_id} &middot; Trading Bot approval mode
</p>
</body></html>"""

    # ── Delivery: direct X posting ────────────────────────────────────────────

    def _post_tweet(self, text: str, reply_to_id: str = None) -> Optional[str]:
        """Post a single tweet directly to X. Returns tweet_id or None. Never raises."""
        if not self._can_post():
            return None
        if not self._safety_check(text):
            return None

        if self.dry_run:
            print(f"\n[DRY RUN] {'REPLY to ' + reply_to_id if reply_to_id else 'TWEET'}:")
            print(f"  {text}")
            self._last_post_ts = time.monotonic()
            return "DRY_RUN"

        try:
            kwargs = {"text": text}
            if reply_to_id and reply_to_id != "DRY_RUN":
                kwargs["in_reply_to_tweet_id"] = reply_to_id
            resp = self._client.create_tweet(**kwargs)
            tweet_id = str(resp.data["id"])
            self._last_post_ts = time.monotonic()
            time.sleep(2)
            log.info("trade_publisher: posted tweet_id=%s", tweet_id)
            return tweet_id
        except Exception as exc:
            log.warning("trade_publisher: post failed: %s", exc)
            return None

    def _post_thread(self, tweets: list[str], post_type: str,
                     symbols: list = None) -> Optional[str]:
        """Post a thread of tweets directly to X. Returns first tweet_id."""
        if not tweets:
            return None
        first_id = None
        prev_id  = None
        for i, text in enumerate(tweets):
            tid = self._post_tweet(text, reply_to_id=prev_id)
            if tid is None:
                break
            if i == 0:
                first_id = tid
            prev_id = tid
        if first_id:
            self._save_post(first_id, "\n---\n".join(tweets),
                            post_type, symbols=symbols)
        return first_id

    # ── Unified delivery dispatcher ───────────────────────────────────────────

    def _dispatch(
        self,
        tweets:    list[str],
        post_type: str,
        symbols:   list = None,
        pnl:       float = None,
    ) -> Optional[str]:
        """
        Route assembled tweets to the appropriate delivery mechanism.

        Approval mode  → send_post_for_approval() (SMS + email for manual posting)
        Direct mode    → _post_tweet() / _post_thread() (live X API)

        Handles _save_post() in both paths. Returns a reference ID or None.
        """
        if not tweets or not self._can_post():
            return None

        if self._approval_mode:
            ref_id = self.send_post_for_approval({
                "post_type": post_type,
                "tweets":    tweets,
                "symbols":   symbols or [],
                "pnl":       pnl,
            })
            if ref_id:
                self._save_post(ref_id, "\n---\n".join(tweets), post_type,
                                symbols=symbols, pnl=pnl)
            return ref_id
        else:
            if len(tweets) > 1:
                return self._post_thread(tweets, post_type, symbols=symbols)
            else:
                tid = self._post_tweet(tweets[0])
                if tid:
                    self._save_post(tid, tweets[0], post_type,
                                    symbols=symbols, pnl=pnl)
                return tid

    # ── Claude post generator ─────────────────────────────────────────────────

    def _get_recent_posts_str(self, n: int = 5) -> str:
        posts = self._history.get("posts", [])[-n:]
        if not posts:
            return "(no recent posts)"
        lines = []
        for p in posts:
            ts   = p.get("ts", "")[:16]
            typ  = p.get("type", "?")
            cont = p.get("content", "")[:120].replace("\n", " ")
            lines.append(f"[{ts}] ({typ}): {cont}")
        return "\n".join(lines)

    def _get_account_context(self) -> dict:
        """Load current account state for Claude context."""
        ctx = {
            "equity":     "unknown",
            "total_pnl":  "unknown",
            "win_rate":   "unknown",
            "streak":     "unknown",
            "avoid_list": [],
            "strategy":   "hybrid",
        }
        try:
            import memory as mem  # noqa: PLC0415
            perf   = mem.get_performance_summary()
            tots   = perf.get("totals", {})
            trades = tots.get("trades", 0)
            wins   = tots.get("wins", 0)
            ctx["win_rate"] = f"{wins/trades*100:.0f}%" if trades else "n/a"

            decisions = mem._load_decisions()
            outcomes  = []
            for d in reversed(decisions):
                for a in d.get("actions", []):
                    o = a.get("outcome")
                    if o in ("win", "loss"):
                        outcomes.append(o)
            if outcomes:
                streak_val = 1
                for o in outcomes[1:]:
                    if o == outcomes[0]:
                        streak_val += 1
                    else:
                        break
                ctx["streak"] = (f"{streak_val} {outcomes[0]}s in a row"
                                 if streak_val > 1 else outcomes[0])
        except Exception:
            pass

        try:
            cfg_path = _BASE_DIR / "strategy_config.json"
            if cfg_path.exists():
                cfg = json.loads(cfg_path.read_text())
                ctx["strategy"] = cfg.get("active_strategy", "hybrid")
        except Exception:
            pass

        return ctx

    def _generate_post_with_claude(
        self,
        post_type:   str,
        context:     dict,
        recent_posts: str = "",
        max_retries: int  = 2,
    ) -> dict:
        """Generate a post via Claude. Returns dict with main_tweet, thread, include_thread."""
        if not self._claude:
            return {"main_tweet": "", "thread": [], "include_thread": False}

        account_ctx = self._get_account_context()
        disclaimer  = self._get_disclaimer()
        model       = _MODEL_SMART if post_type in _SMART_POST_TYPES else _MODEL_FAST

        # Split system into cacheable static voice + dynamic per-call context
        dynamic_system_text = (
            f"\nRecent posts for context (avoid repetition, build on narratives):\n"
            f"{recent_posts or '(none yet)'}\n\n"
            f"Current bot state:\n"
            f"- Equity: {account_ctx['equity']}\n"
            f"- Win/loss streak: {account_ctx['streak']}\n"
            f"- Win rate (all-time): {account_ctx['win_rate']}\n"
            f"- Active strategy: {account_ctx['strategy']}\n"
            f"- Disclaimer to append: \"{disclaimer}\"\n"
        )

        user_prompt = f"""Post type: {post_type}

Context:
{json.dumps(context, indent=2, default=str)}

Generate a tweet (and optionally a thread) in the exact @BullBearBotAI voice.

Return ONLY valid JSON:
{{
  "main_tweet": "tweet text including disclaimer at end",
  "thread": ["tweet2 text", "tweet3 text"],
  "include_thread": false,
  "post_reasoning": "why this angle/voice choice",
  "voice_score": 8
}}

Rules:
- main_tweet must be under 280 characters including disclaimer
- thread array is empty [] if include_thread is false
- voice_score is your own 1-10 assessment of how well this matches the bot's voice
- Only include thread if context warrants it (strong debate, congressional signal, weekly recap, etc)
- The disclaimer must be the LAST thing in the main_tweet"""

        for attempt in range(max_retries + 1):
            try:
                resp = self._claude.messages.create(
                    model=model,
                    max_tokens=800,
                    system=[
                        {
                            "type": "text",
                            "text": _VOICE_SYSTEM,
                            "cache_control": {"type": "ephemeral"},
                        },
                        {
                            "type": "text",
                            "text": dynamic_system_text,
                        },
                    ],
                    messages=[{"role": "user", "content": user_prompt}],
                    extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"},
                )
                try:
                    from cost_tracker import get_tracker  # noqa: PLC0415
                    get_tracker().record_api_call(model, resp.usage,
                                                  caller=f"publisher_{post_type}")
                except Exception:
                    pass
                raw = resp.content[0].text.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
                result = json.loads(raw)

                voice_score = int(result.get("voice_score", 8))
                if voice_score < 7 and attempt < max_retries:
                    log.debug("trade_publisher: voice_score=%d < 7, regenerating", voice_score)
                    user_prompt += (f"\n\nPrevious attempt scored {voice_score}/10. "
                                    "Rewrite with stronger character voice.")
                    continue

                main = result.get("main_tweet", "")
                if main and not self._has_disclaimer(main):
                    main = main.rstrip() + "\n" + disclaimer
                    result["main_tweet"] = main

                return result

            except Exception as exc:
                log.warning("trade_publisher: Claude generation failed (attempt %d): %s",
                            attempt + 1, exc)

        return {"main_tweet": "", "thread": [], "include_thread": False,
                "post_reasoning": "generation failed"}

    # ── Position state verification ───────────────────────────────────────────

    def verify_before_publish(
        self,
        symbol:       str,
        pub_type:     str,   # "entry" or "exit"
        alpaca_client,
    ) -> Optional[object]:
        """
        Verify live Alpaca position state before publishing a trade tweet.

        pub_type="entry":
            Returns the Alpaca position object if the symbol has qty > 0.
            The caller uses pos.avg_entry_price as the confirmed entry price.

        pub_type="exit":
            Returns a dict {"fill_price": float, "filled_qty": float}
            if the position is confirmed gone AND a recent sell fill is found.
            Returns {"fill_price": 0.0, "filled_qty": 0.0} if position is gone
            but no fill record found (still safe to publish without P&L numbers).

        Returns None (log WARNING, don't publish) if:
            - entry: position is not open in Alpaca
            - exit: position is still open (not yet filled/closed)
            - any API call fails
        """
        try:
            raw_positions = alpaca_client.get_all_positions()
            pos_list = raw_positions if isinstance(raw_positions, list) else []
            pos_map = {
                getattr(p, "symbol", ""): p
                for p in pos_list
                if float(getattr(p, "qty", 0)) > 0
            }

            if pub_type == "entry":
                pos = pos_map.get(symbol)
                if pos is None:
                    log.warning(
                        "[PUBLISHER] %s: skipping entry tweet — "
                        "position state not confirmed", symbol,
                    )
                    return None
                return pos

            # pub_type == "exit"
            if symbol in pos_map:
                log.warning(
                    "[PUBLISHER] %s: skipping exit tweet — "
                    "position state not confirmed (still open)", symbol,
                )
                return None

            # Position is confirmed gone — look for the closing fill
            try:
                from alpaca.trading.requests import GetOrdersRequest   # noqa: PLC0415
                from alpaca.trading.enums import QueryOrderStatus       # noqa: PLC0415
                orders = alpaca_client.get_orders(
                    GetOrdersRequest(status=QueryOrderStatus.CLOSED, limit=20)
                )
                for o in (orders if isinstance(orders, list) else []):
                    if getattr(o, "symbol", "") != symbol:
                        continue
                    if "sell" not in str(getattr(o, "side", "")).lower():
                        continue
                    fill_price = float(getattr(o, "avg_fill_price", 0) or 0)
                    filled_qty = float(getattr(o, "filled_qty", 0) or 0)
                    if fill_price > 0 and filled_qty > 0:
                        return {"fill_price": fill_price, "filled_qty": filled_qty}
            except Exception as _fill_exc:
                log.debug("[PUBLISHER] %s: fill lookup failed: %s", symbol, _fill_exc)

            # Position gone but no fill found — safe to publish without P&L precision
            return {"fill_price": 0.0, "filled_qty": 0.0}

        except Exception as exc:
            log.warning(
                "[PUBLISHER] %s: skipping %s tweet — position state not confirmed: %s",
                symbol, pub_type, exc,
            )
            return None

    # ── Public post type methods ──────────────────────────────────────────────
    #
    # Each method:
    #   1. Builds a context dict and calls _generate_post_with_claude()
    #   2. Assembles the tweets list (main + optional thread)
    #   3. Calls _dispatch() which routes to SMS+email or X based on mode
    #
    # All delivery and history persistence is handled inside _dispatch().

    def publish_trade_entry(
        self,
        action:         dict,
        debate_result:  dict = None,
        market_context: str  = "",
        alpaca_client          = None,
    ) -> Optional[str]:
        """
        Deliver when a trade entry is confirmed.

        Requires alpaca_client to verify the position exists and obtain the
        actual avg_entry_price. If verification fails, the tweet is suppressed.
        The entry price in the tweet is always from the Alpaca position object,
        never from Claude's limit_price or action dict.
        """
        if not self.enabled:
            return None
        try:
            sym        = action.get("symbol", "?")
            act        = action.get("action", "buy")
            qty        = action.get("qty")
            stop       = action.get("stop_loss")
            target     = action.get("take_profit")
            catalyst   = action.get("catalyst", "")
            confidence = action.get("confidence", "medium")
            tier       = action.get("tier", "core")

            # Verify position and get confirmed entry price from Alpaca
            confirmed_entry = None
            if alpaca_client is not None:
                verified = self.verify_before_publish(sym, "entry", alpaca_client)
                if verified is None:
                    return None  # verification failed — don't publish
                confirmed_entry = float(getattr(verified, "avg_entry_price", None) or 0) or None
                confirmed_qty   = float(getattr(verified, "qty", qty or 0))
                qty = confirmed_qty if confirmed_qty > 0 else qty

            context = {
                "trade": {
                    "symbol":          sym,
                    "action":          act.upper(),
                    "qty":             qty,
                    "entry_price":     confirmed_entry,   # actual fill — not Claude's limit
                    "stop_loss":       stop,
                    "take_profit":     target,
                    "catalyst":        catalyst,
                    "confidence":      confidence,
                    "tier":            tier,
                    "price_confirmed": confirmed_entry is not None,
                },
                "market_context": market_context,
                "debate":         debate_result or {},
            }

            has_triple = (debate_result and
                          "TRIPLE SIGNAL" in str(debate_result.get("synthesis", "")))
            has_close_debate = (debate_result and
                                debate_result.get("proceed") and
                                debate_result.get("conviction_adjustment") == "lower")

            result = self._generate_post_with_claude(
                "trade_entry", context, self._get_recent_posts_str()
            )
            if not result.get("main_tweet"):
                return None

            include_thread = (result.get("include_thread") or has_triple or has_close_debate)
            tweets = [result["main_tweet"]]
            if include_thread and result.get("thread"):
                tweets.extend(result["thread"])

            return self._dispatch(tweets, "trade_entry", symbols=[sym])
        except Exception as exc:
            log.warning("trade_publisher: publish_trade_entry failed: %s", exc)
            return None

    def publish_trade_exit(
        self,
        symbol:          str,
        entry_price:     float,
        exit_price:      float,
        qty:             float,
        pnl:             float,
        hold_time_hours: float,
        outcome:         str,
        week_total_pnl:  float = 0.0,
        alpaca_client           = None,
    ) -> Optional[str]:
        """
        Deliver when a trade closes (win or loss).

        Requires alpaca_client to:
          1. Verify position is gone from Alpaca (confirmed close).
          2. Fetch actual avg_fill_price from the closing order.
          3. Calculate real P&L = (fill_price - entry_price) * filled_qty.

        If verification fails the tweet is suppressed.
        Never uses unrealized_pl, estimated prices, or Claude-generated targets.
        """
        if not self.enabled:
            return None
        try:
            # Verify position is gone and get real fill data
            confirmed_fill_price = exit_price
            confirmed_qty        = qty
            confirmed_pnl        = pnl
            price_confirmed      = False

            if alpaca_client is not None:
                verified = self.verify_before_publish(symbol, "exit", alpaca_client)
                if verified is None:
                    return None  # position still open or API error — don't publish
                fill_price = verified.get("fill_price", 0.0)
                filled_qty = verified.get("filled_qty", 0.0)
                if fill_price > 0 and filled_qty > 0:
                    confirmed_fill_price = fill_price
                    confirmed_qty        = filled_qty
                    if entry_price and entry_price > 0:
                        confirmed_pnl = (fill_price - entry_price) * filled_qty
                    price_confirmed = True
                elif fill_price == 0.0 and verified.get("fill_price") == 0.0:
                    # Position gone but no fill found — publish only if we have
                    # passed-in prices from a confirmed Alpaca source (not estimated).
                    log.debug("[PUBLISHER] %s: exit confirmed but no fill record found — "
                              "using passed-in prices", symbol)

            context = {
                "symbol":           symbol,
                "entry_price":      entry_price,
                "exit_price":       confirmed_fill_price,
                "qty":              confirmed_qty,
                "pnl":              confirmed_pnl,
                "hold_time_hours":  round(hold_time_hours, 1),
                "outcome":          outcome,
                "week_total_pnl":   week_total_pnl,
                "pnl_pct":          (
                    round((confirmed_fill_price - entry_price) / entry_price * 100, 2)
                    if entry_price and entry_price > 0 else 0
                ),
                "price_confirmed":  price_confirmed,
            }
            result = self._generate_post_with_claude(
                "trade_exit", context, self._get_recent_posts_str()
            )
            if not result.get("main_tweet"):
                return None

            tweets = [result["main_tweet"]]
            if result.get("include_thread") and result.get("thread"):
                tweets.extend(result["thread"])

            return self._dispatch(tweets, "trade_exit", symbols=[symbol],
                                  pnl=confirmed_pnl)
        except Exception as exc:
            log.warning("trade_publisher: publish_trade_exit failed: %s", exc)
            return None

    def publish_interesting_skip(
        self,
        action_considered: dict,
        skip_reason:       str,
        debate_result:     dict = None,
    ) -> Optional[str]:
        """Deliver 1-in-3 interesting skips. Claude decides if worth delivering."""
        if not self.enabled:
            return None
        try:
            # Deterministic 1-in-3 gate using minute-of-day
            if datetime.now().minute % 3 != 0:
                return None

            context = {
                "action_considered": action_considered,
                "skip_reason":       skip_reason,
                "debate":            debate_result or {},
                "should_post_check": (
                    "Is this skip interesting enough to post? "
                    "Criteria: strong competing signals, debate veto, "
                    "recurring symbol that always gets skipped, congressional "
                    "signal present but overridden. "
                    "Set include_thread=false and voice_score=0 if not worth posting."
                ),
            }
            result = self._generate_post_with_claude(
                "interesting_skip", context, self._get_recent_posts_str()
            )
            if not result.get("main_tweet") or result.get("voice_score", 8) == 0:
                return None

            tweets = [result["main_tweet"]]
            if result.get("include_thread") and result.get("thread"):
                tweets.extend(result["thread"])

            sym = action_considered.get("symbol", "")
            return self._dispatch(tweets, "skip", symbols=[sym] if sym else [])
        except Exception as exc:
            log.warning("trade_publisher: publish_interesting_skip failed: %s", exc)
            return None

    def publish_premarket_brief(self, morning_brief: dict) -> Optional[str]:
        """Deliver pre-market conviction brief. Always delivers (no skip logic)."""
        if not self.enabled:
            return None
        try:
            picks = morning_brief.get("conviction_picks", [])
            context = {
                "market_tone":    morning_brief.get("market_tone", "?"),
                "key_themes":     morning_brief.get("key_themes", []),
                "brief_summary":  morning_brief.get("brief_summary", ""),
                "top_picks": [
                    {
                        "symbol":    p.get("symbol"),
                        "direction": p.get("direction"),
                        "catalyst":  p.get("catalyst", "")[:80],
                        "entry":     p.get("entry_zone"),
                        "conviction": p.get("conviction"),
                    }
                    for p in picks[:3]
                ],
                "avoid_today": morning_brief.get("avoid_today", []),
            }
            result = self._generate_post_with_claude(
                "premarket_brief", context, self._get_recent_posts_str()
            )
            if not result.get("main_tweet"):
                return None

            tweets = [result["main_tweet"]]
            if result.get("include_thread") and result.get("thread"):
                tweets.extend(result["thread"])

            syms = [p.get("symbol", "") for p in picks[:3]]
            return self._dispatch(tweets, "premarket_brief", symbols=syms)
        except Exception as exc:
            log.warning("trade_publisher: publish_premarket_brief failed: %s", exc)
            return None

    def publish_weekly_recap(
        self,
        weekly_review_data: dict,
        report_path:        str = "",
    ) -> Optional[str]:
        """Deliver weekly recap as a thread (5-7 tweets)."""
        if not self.enabled:
            return None
        try:
            context = {
                "report_path":      report_path,
                "weekly_data":      weekly_review_data,
                "thread_structure": [
                    "Tweet 1: Headline numbers (trades, win rate, P&L)",
                    "Tweet 2: Best trade with brief story",
                    "Tweet 3: Worst trade with honest post-mortem",
                    "Tweet 4: What the 5 agents decided this week",
                    "Tweet 5: Parameter changes deployed",
                    "Tweet 6: One honest reflection — most human-sounding tweet",
                    "Tweet 7 (optional): Preview of next week's focus",
                ],
                "instruction": (
                    "Always include_thread=true. "
                    "The reflection tweet (6) is the most important — "
                    "it should acknowledge uncertainty or surprise. "
                    "All tweets must include disclaimer."
                ),
            }
            result = self._generate_post_with_claude(
                "weekly_recap", context, self._get_recent_posts_str()
            )
            if not result.get("main_tweet"):
                return None

            tweets = [result["main_tweet"]]
            if result.get("thread"):
                tweets.extend(result["thread"])

            # Ensure every thread tweet has a disclaimer
            disclaimer = self._get_disclaimer()
            for i, t in enumerate(tweets):
                if not self._has_disclaimer(t):
                    tweets[i] = t.rstrip() + "\n" + disclaimer

            return self._dispatch(tweets, "weekly_recap")
        except Exception as exc:
            log.warning("trade_publisher: publish_weekly_recap failed: %s", exc)
            return None

    def publish_monthly_milestone(
        self,
        month_number: int,
        stats:        dict,
    ) -> Optional[str]:
        """Deliver monthly milestone thread on the 13th of each month."""
        if not self.enabled:
            return None
        try:
            context = {
                "month_number": month_number,
                "stats":        stats,
                "instruction":  (
                    "Always include_thread=true. "
                    "Tone: retrospective, honest, slightly philosophical "
                    "about what it means for an AI to learn over time."
                ),
            }
            result = self._generate_post_with_claude(
                "monthly_milestone", context, self._get_recent_posts_str()
            )
            if not result.get("main_tweet"):
                return None

            tweets = [result["main_tweet"]]
            if result.get("thread"):
                tweets.extend(result["thread"])

            return self._dispatch(tweets, "monthly_milestone")
        except Exception as exc:
            log.warning("trade_publisher: publish_monthly_milestone failed: %s", exc)
            return None

    def publish_code_update(self, changes: list[str]) -> Optional[str]:
        """Deliver when significant bot upgrades are deployed."""
        if not self.enabled:
            return None
        try:
            context = {
                "changes": changes,
                "instruction": (
                    "Written from bot's perspective: 'I was updated.' "
                    "Slightly unsettled about being modified, but professional. "
                    "Reference specific new capabilities. Acknowledge prior version no longer exists."
                ),
            }
            result = self._generate_post_with_claude(
                "code_update", context, self._get_recent_posts_str()
            )
            if not result.get("main_tweet"):
                return None

            tweets = [result["main_tweet"]]
            if result.get("include_thread") and result.get("thread"):
                tweets.extend(result["thread"])

            return self._dispatch(tweets, "code_update")
        except Exception as exc:
            log.warning("trade_publisher: publish_code_update failed: %s", exc)
            return None

    def publish_lookback(self, days_ago: int, trade: dict) -> Optional[str]:
        """Deliver Mon/Wed/Fri at 6 PM ET — look back at a past trade."""
        if not self.enabled:
            return None
        try:
            context = {
                "days_ago": days_ago,
                "trade":    trade,
                "instruction": (
                    "Reflective, honest, occasionally self-deprecating. "
                    "Compare what the bot reasoned then vs what actually happened. "
                    "Most interesting when: bot was wrong, bot skipped a winner, "
                    "or reasoning was sound but outcome bad (or vice versa). "
                    "Never defensive about past decisions."
                ),
            }
            result = self._generate_post_with_claude(
                "lookback", context, self._get_recent_posts_str()
            )
            if not result.get("main_tweet"):
                return None

            tweets = [result["main_tweet"]]
            if result.get("include_thread") and result.get("thread"):
                tweets.extend(result["thread"])

            sym = trade.get("symbol", "")
            return self._dispatch(tweets, "lookback", symbols=[sym] if sym else [])
        except Exception as exc:
            log.warning("trade_publisher: publish_lookback failed: %s", exc)
            return None

    def publish_flat_day(
        self,
        cycles:              int,
        vix:                 float,
        skips:               list[dict],
        open_positions:      list | None = None,
        closed_trades_today: list | None = None,
    ) -> Optional[str]:
        """Deliver at 4 PM ET on zero-trade days.

        Suppressed if:
        - Already fired today (date guard)
        - Open positions exist (the day wasn't flat)
        - Closed trades exist today (activity happened)
        """
        global _flat_day_sent_date
        if not self.enabled:
            return None

        from datetime import date
        from zoneinfo import ZoneInfo
        today_et = date.today().isoformat()  # YYYY-MM-DD; ET assumed (scheduler runs in ET)

        # Once-per-day guard — primary defense against repeated firing
        if _flat_day_sent_date == today_et:
            log.debug("[PUBLISHER] flat_day already sent today — skipping")
            return None

        # Suppress if the day wasn't actually flat
        if open_positions:
            log.debug("[PUBLISHER] flat_day suppressed — positions or trades exist today"
                      " (open_positions=%d)", len(open_positions))
            return None
        if closed_trades_today:
            log.debug("[PUBLISHER] flat_day suppressed — positions or trades exist today"
                      " (closed_trades_today=%d)", len(closed_trades_today))
            return None

        try:
            most_tempting = skips[-1] if skips else {}
            context = {
                "cycles_run":    cycles,
                "vix":           vix,
                "skips_count":   len(skips),
                "most_tempting": most_tempting,
                "instruction": (
                    "Patient, not frustrated. The bot CHOSE to do nothing. "
                    "That's a decision, not a failure. "
                    "Explain specifically WHY nothing met the bar today. "
                    "Reference the most tempting setup that got rejected."
                ),
            }
            result = self._generate_post_with_claude(
                "flat_day", context, self._get_recent_posts_str()
            )
            if not result.get("main_tweet"):
                return None

            tweets = [result["main_tweet"]]
            if result.get("include_thread") and result.get("thread"):
                tweets.extend(result["thread"])

            result_id = self._dispatch(tweets, "flat_day")
            # Set date guard only on successful dispatch (so a crash doesn't silence tomorrow)
            if result_id is not None:
                _flat_day_sent_date = today_et
                log.debug("[PUBLISHER] flat_day sent — guard set for %s", today_et)
            return result_id
        except Exception as exc:
            log.warning("trade_publisher: publish_flat_day failed: %s", exc)
            return None

    # ── Engagement stats (direct mode only) ──────────────────────────────────

    def update_engagement_stats(self) -> None:
        """Fetch like/RT/reply counts for recent posts. No-op in approval mode."""
        if not self.enabled or not self._client or self._approval_mode:
            return
        try:
            posts = self._history.get("posts", [])
            recent = [p for p in posts
                      if p.get("tweet_id") and
                      p["tweet_id"] not in ("DRY_RUN", "") and
                      not p["tweet_id"].startswith("APPROVAL_")][-50:]

            for p in recent:
                tid = p.get("tweet_id", "")
                if not tid:
                    continue
                try:
                    resp = self._client.get_tweet(tid, tweet_fields=["public_metrics"])
                    if resp.data and hasattr(resp.data, "public_metrics"):
                        m = resp.data.public_metrics
                        p["engagement"] = {
                            "likes":    m.get("like_count", 0),
                            "retweets": m.get("retweet_count", 0),
                            "replies":  m.get("reply_count", 0),
                        }
                except Exception:
                    pass

            _HISTORY_FILE.write_text(json.dumps(self._history, indent=2))
            log.debug("trade_publisher: engagement stats updated")
        except Exception as exc:
            log.warning("trade_publisher: update_engagement_stats failed: %s", exc)

    # ── Test / validation ─────────────────────────────────────────────────────

    def test_connection(self) -> bool:
        """
        Validate configuration.
        Direct mode: verify Twitter API credentials by calling get_me().
        Approval mode: verify Twilio and SendGrid are configured.
        Returns True if the active delivery channel appears ready.
        """
        if self._approval_mode:
            twilio_ok   = all([
                os.getenv("TWILIO_ACCOUNT_SID"),
                os.getenv("TWILIO_AUTH_TOKEN"),
                os.getenv("TWILIO_FROM_NUMBER"),
                os.getenv("TWILIO_TO_NUMBER"),
            ])
            sendgrid_ok = bool(os.getenv("SENDGRID_API_KEY"))
            claude_ok   = self._claude is not None

            print(f"Mode        : APPROVAL (SMS+email delivery)")
            print(f"Claude      : {'OK' if claude_ok else 'MISSING'}")
            print(f"Twilio SMS  : {'configured' if twilio_ok else 'not configured'}")
            print(f"SendGrid    : {'configured' if sendgrid_ok else 'not configured'}")
            print(f"Email to    : {_APPROVAL_TO_EMAIL}")
            return claude_ok and (twilio_ok or sendgrid_ok)
        else:
            if not self._client:
                print("trade_publisher: no Twitter client initialized")
                return False
            try:
                me = self._client.get_me()
                print(f"Mode        : DIRECT POST")
                print(f"Connected as: @{me.data.username}")
                # Dry-run test
                was_dry = self.dry_run
                self.dry_run = True
                self._post_tweet("Systems check — dry run mode active. Not posting.")
                self.dry_run = was_dry
                return True
            except Exception as exc:
                log.warning("trade_publisher: test_connection failed: %s", exc)
                return False

    def post_test_tweet(self) -> Optional[str]:
        """
        Post/send the inaugural systems-check message.
        Direct mode: posts to @BullBearBotAI on X.
        Approval mode: sends via SMS+email for manual posting.
        """
        if not self.enabled:
            log.warning("trade_publisher: not enabled")
            return None

        try:
            import trade_memory  # noqa: PLC0415
            vm_stats = trade_memory.get_collection_stats()
            vm_count = vm_stats.get("total", 0)
        except Exception:
            vm_count = 0

        disclaimer = self._get_disclaimer()
        text = (
            f"Systems check. @BullBearBotAI is online.\n\n"
            f"Strategy: Hybrid (momentum + mean reversion + cross-sector)\n"
            f"Memory: 3-tier vector store, {vm_count} records\n"
            f"Agents: Bull, Bear, Risk Manager, Strategy Director\n\n"
            f"I have opinions. They are data-driven. Mostly.\n\n"
            f"{disclaimer}"
        )
        if len(text) > 280:
            text = (
                f"Systems check. @BullBearBotAI is online.\n\n"
                f"Strategy: Hybrid momentum + cross-sector reasoning.\n"
                f"Agents: Bull (optimistic), Bear (smug), Risk Mgr (worried), Director (decisive).\n\n"
                f"I have opinions. Data-driven. Mostly.\n\n"
                f"{disclaimer}"
            )

        ref_id = self._dispatch([text], "inaugural")
        if ref_id:
            log.info("trade_publisher: inaugural message sent  ref=%s  mode=%s",
                     ref_id, "approval" if self._approval_mode else "direct")
        return ref_id
