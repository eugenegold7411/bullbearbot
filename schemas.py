"""
schemas.py — Canonical contract layer for BullBearBot.

Single source of truth for all inter-module data contracts:
  - Enums: AccountAction, OptionStrategy, Conviction, Direction, Tier,
           SessionTier, StructureLifecycle
  - Symbol helpers: normalize_symbol, is_crypto, alpaca_symbol, yfinance_symbol
  - Claude output:  TradeIdea, ClaudeDecision (intent-based — NO qty/stop/target)
  - Signal data:    SignalScore
  - Risk kernel output: BrokerAction, OptionsAction (broker-ready — HAS qty/stop/target)
  - Broker state:   NormalizedPosition, NormalizedOrder, BrokerSnapshot
  - Options structures: OptionsLeg, OptionsStructure

Translation chain (Account 1):
  ClaudeDecision → ideas[] → risk_kernel.process_idea() → BrokerAction → execute_all()

Translation chain (Account 2):
  ClaudeDecision → ideas[] → risk_kernel.process_options_idea() → OptionsAction
  → options_execution.submit_structure() → Alpaca API

order_executor.execute_all() receives list[dict] from BrokerAction.to_dict().
It never sees TradeIdea or ClaudeDecision directly.

No Alpaca imports in this module — keeps the contract layer dependency-free.
from_alpaca_*() factory methods accept duck-typed Alpaca objects (no import needed).
"""

from __future__ import annotations

import subprocess as _subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

# ── Internal helper ───────────────────────────────────────────────────────────

def _maybe_float(v) -> Optional[float]:
    """Convert v to float, returning None if v is None or unconvertible."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Symbol normalisation
#
# Three formats exist in the system:
#   Claude / internal : "BTC/USD"   (slash)
#   Alpaca positions  : "BTCUSD"    (no separator)
#   yfinance          : "BTC-USD"   (dash)
#
# Canonical internal form = slash format.  All public helpers accept any format.
# ─────────────────────────────────────────────────────────────────────────────

_CRYPTO_BASE: frozenset[str] = frozenset({
    "BTC", "ETH", "SOL", "DOGE", "AVAX", "MATIC", "LTC", "XRP",
    "ADA", "DOT", "LINK", "UNI", "AAVE", "ALGO", "ATOM", "FIL",
    "NEAR", "SHIB",
})


def normalize_symbol(symbol: str) -> str:
    """
    Return canonical internal symbol (uppercase; crypto in slash format BTC/USD).

    Accepts any of the three formats:
      "BTC/USD"  -> "BTC/USD"
      "BTCUSD"   -> "BTC/USD"
      "BTC-USD"  -> "BTC/USD"
      "AAPL"     -> "AAPL"
    """
    s = symbol.strip().upper()
    if "/" in s:
        return s
    # Dash format (yfinance): BTC-USD
    if "-" in s:
        parts = s.split("-")
        if len(parts) == 2 and parts[0] in _CRYPTO_BASE and parts[1] == "USD":
            return f"{parts[0]}/USD"
    # No-separator format (Alpaca): BTCUSD
    if s.endswith("USD") and len(s) > 3 and s[:-3] in _CRYPTO_BASE:
        return f"{s[:-3]}/USD"
    return s


def is_crypto(symbol: str) -> bool:
    """
    True for any crypto symbol in any format.

    Examples:
      is_crypto("BTC/USD")  -> True
      is_crypto("BTCUSD")   -> True
      is_crypto("BTC-USD")  -> True
      is_crypto("AAPL")     -> False
    """
    s = symbol.strip().upper()
    if "/" in s:
        return s.split("/")[0] in _CRYPTO_BASE
    if "-" in s:
        return s.split("-")[0] in _CRYPTO_BASE
    if s.endswith("USD") and len(s) > 3:
        return s[:-3] in _CRYPTO_BASE
    return False


def alpaca_symbol(symbol: str) -> str:
    """
    Convert to Alpaca position/order format (no separator for crypto).

    "BTC/USD" -> "BTCUSD",  "AAPL" -> "AAPL"
    """
    s = normalize_symbol(symbol)
    return s.replace("/", "")


def yfinance_symbol(symbol: str) -> str:
    """
    Convert to yfinance format (dash separator for crypto).

    "BTC/USD" -> "BTC-USD",  "AAPL" -> "AAPL"
    """
    s = normalize_symbol(symbol)
    return s.replace("/", "-")


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class AccountAction(str, Enum):
    """All valid action types across both accounts."""
    BUY                = "buy"
    SELL               = "sell"
    HOLD               = "hold"
    CLOSE              = "close"
    REALLOCATE         = "reallocate"
    # Options-specific actions (Account 2)
    BUY_OPTION         = "buy_option"
    SELL_OPTION_SPREAD = "sell_option_spread"
    BUY_STRADDLE       = "buy_straddle"
    CLOSE_OPTION       = "close_option"


class OptionStrategy(str, Enum):
    """All supported options structure strategies."""
    CALL_DEBIT_SPREAD  = "call_debit_spread"
    PUT_DEBIT_SPREAD   = "put_debit_spread"
    CALL_CREDIT_SPREAD = "call_credit_spread"
    PUT_CREDIT_SPREAD  = "put_credit_spread"
    SINGLE_CALL        = "single_call"
    SINGLE_PUT         = "single_put"
    STRADDLE           = "straddle"
    CLOSE_OPTION       = "close_option"


class Conviction(str, Enum):
    """Claude's stated conviction level for a trade idea."""
    HIGH   = "high"
    MEDIUM = "medium"
    LOW    = "low"


class Direction(str, Enum):
    """Directional bias for a trade or signal."""
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class Tier(str, Enum):
    """
    Watchlist / position tier.

    Size limits (enforced by risk kernel):
      CORE     : max 15% equity
      DYNAMIC  : max 8% equity   (scanner-promoted, same-day only)
      INTRADAY : max 5% equity   (exit before close)
    """
    CORE     = "core"
    DYNAMIC  = "dynamic"
    INTRADAY = "intraday"


class SessionTier(str, Enum):
    """Scheduler session tier at cycle time."""
    MARKET    = "market"      # 9:30 AM - 8:00 PM ET, stocks + crypto
    EXTENDED  = "extended"    # 4:00 AM - 9:30 AM, 8 PM - 11 PM, crypto only
    OVERNIGHT = "overnight"   # 11 PM - 4 AM + weekends, BTC/ETH only


class StructureLifecycle(str, Enum):
    """Account 2 options structure lifecycle state."""
    PROPOSED         = "proposed"          # candidate built, not yet submitted
    SUBMITTED        = "submitted"         # order(s) sent to broker
    PARTIALLY_FILLED = "partially_filled"  # some legs filled
    FULLY_FILLED     = "fully_filled"      # all legs filled, structure live
    CLOSED           = "closed"            # fully closed (all legs exited)
    REJECTED         = "rejected"          # broker rejected one or more legs
    EXPIRED          = "expired"           # option expired without exercise
    CANCELLED        = "cancelled"         # manually cancelled before fill


# ─────────────────────────────────────────────────────────────────────────────
# Signal data (Stage 2 Haiku output)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SignalScore:
    """
    Per-symbol output from Stage 2 signal scorer (Haiku).

    Parsed from signal_scores.json -> scored_symbols[symbol].
    Used by risk_kernel as one input alongside TradeIdea.
    """
    symbol:            str
    score:             float            # 0-100
    conviction:        Conviction
    direction:         Direction
    tier:              Tier
    primary_catalyst:  str
    signals:           list[str]  = field(default_factory=list)
    conflicts:         list[str]  = field(default_factory=list)
    orb_candidate:     bool       = False
    pattern_watchlist: bool       = False
    price:             Optional[float] = None   # injected by B1 post-processing

    @classmethod
    def from_dict(cls, symbol: str, d: dict) -> "SignalScore":
        """Parse from a scored_symbols entry in signal_scores.json."""
        raw_conviction = str(d.get("conviction", "low")).lower()
        raw_direction  = str(d.get("direction", "neutral")).lower()
        raw_tier       = str(d.get("tier", "core")).lower()
        try:
            conviction = Conviction(raw_conviction)
        except ValueError:
            conviction = Conviction.LOW
        try:
            direction = Direction(raw_direction)
        except ValueError:
            direction = Direction.NEUTRAL
        try:
            tier = Tier(raw_tier)
        except ValueError:
            tier = Tier.CORE
        return cls(
            symbol=normalize_symbol(symbol),
            score=float(d.get("score", 0)),
            conviction=conviction,
            direction=direction,
            tier=tier,
            primary_catalyst=d.get("primary_catalyst", ""),
            signals=list(d.get("signals", [])),
            conflicts=list(d.get("conflicts", [])),
            orb_candidate=bool(d.get("orb_candidate", False)),
            pattern_watchlist=bool(d.get("pattern_watchlist", False)),
            price=_maybe_float(d.get("price")),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Claude output types (Phase 5+ format)
#
# TradeIdea      — intent only. NO qty, NO absolute stop/target prices.
# ClaudeDecision — top-level response wrapping a list of TradeIdeas.
#
# Phase 5 bot.py will parse Claude's JSON into these types before passing
# to the risk kernel. The risk kernel is the sole place that attaches
# qty/stop/target to produce BrokerAction.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeIdea:
    """
    Claude's intent for a single trade — broker-agnostic, no sizing.

    Fields Claude populates (intent-based format):
      symbol, action, tier, conviction (float 0.0-1.0), direction, catalyst,
      sector_signal, advisory_stop_pct, advisory_target_r, notes, intent,
      option_strategy_hint, exit_symbol, entry_symbol

    Fields the risk kernel adds when producing BrokerAction:
      qty, stop_loss (price), take_profit (price), order_type

    conviction         — float 0.0-1.0 (e.g. 0.80 = high, 0.60 = medium, 0.30 = low).
                         Risk kernel converts to Conviction enum for BrokerAction.
    intent             — Claude's raw intent string (enter_long|enter_short|close|
                         reduce|hold|monitor). Populated by validate_claude_decision().
    advisory_stop_pct  — hint: desired stop as a fraction of entry (e.g. 0.035).
                         Risk kernel applies its own caps; this is a preference.
    advisory_target_r  — hint: desired reward/risk multiple (e.g. 2.5).
                         Risk kernel uses this to compute take_profit from stop dist.
    """
    symbol:               str
    action:               AccountAction
    tier:                 Tier
    conviction:           float
    direction:            Direction
    catalyst:             str
    sector_signal:        str             = ""
    advisory_stop_pct:    Optional[float] = None   # e.g. 0.035 -> 3.5% stop
    advisory_target_r:    Optional[float] = None   # e.g. 2.5 -> 2.5x R/R
    order_type:           str             = "market"
    limit_price:          Optional[float] = None
    notes:                str             = ""
    intent:               str             = ""     # Claude's raw intent string
    # Options hint — risk kernel may override based on IV environment
    option_strategy_hint: Optional[OptionStrategy] = None
    # Reallocate fields
    exit_symbol:          Optional[str]   = None
    entry_symbol:         Optional[str]   = None


@dataclass
class ClaudeDecision:
    """
    Top-level response from ask_claude() in intent-based format.

    Parsed from Claude JSON by validate_claude_decision().
    Supports both new format (ideas[]) and legacy format (actions[]).

    regime_view values: "risk_on" | "risk_off" | "caution" | "halt"
    """
    reasoning:   str
    regime_view: str                        # "risk_on" | "risk_off" | "caution" | "halt"
    ideas:       list[TradeIdea] = field(default_factory=list)
    notes:       str             = ""
    holds:       list[str]       = field(default_factory=list)
    concerns:    str             = ""

    @property
    def regime(self) -> str:
        """Backward-compat alias for regime_view."""
        return self.regime_view


# ─────────────────────────────────────────────────────────────────────────────
# Risk kernel output types
#
# BrokerAction   — equity/ETF/crypto, ready for order_executor.execute_all()
# OptionsAction  — options, ready for options_execution.submit_structure()
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BrokerAction:
    """
    Risk kernel output for equity/ETF/crypto trades.

    Produced by risk_kernel.process_idea().
    Consumed by order_executor.execute_all() via to_dict().

    The to_dict() output matches the exact key/value contract expected
    by execute_all() — including "confidence" (not "conviction") as the
    key for the Conviction value.
    """
    symbol:        str              # canonical (BTC/USD for crypto)
    action:        AccountAction
    qty:           float            # shares or crypto units
    order_type:    str              # "market" | "limit"
    tier:          Tier
    conviction:    Conviction       # serialised as "confidence" in to_dict()
    catalyst:      str
    stop_loss:     Optional[float]  = None
    take_profit:   Optional[float]  = None
    limit_price:   Optional[float]  = None
    sector_signal: str              = ""
    # Reallocate fields
    exit_symbol:   Optional[str]    = None
    entry_symbol:  Optional[str]    = None
    # Traceability — not serialised to dict, not included in repr
    source_idea:   Optional[TradeIdea] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        """
        Produce a dict compatible with order_executor.execute_all().

        Key mapping:
          conviction -> "confidence"   (executor uses the "confidence" key)
          tier.value -> str            ("core" | "dynamic" | "intraday")
          action.value -> str          ("buy" | "sell" | "hold" | "close" | ...)
        """
        d: dict = {
            "action":        self.action.value,
            "symbol":        self.symbol,
            "qty":           self.qty,
            "order_type":    self.order_type,
            "limit_price":   self.limit_price,
            "stop_loss":     self.stop_loss,
            "take_profit":   self.take_profit,
            "tier":          self.tier.value,
            "confidence":    self.conviction.value,  # executor key is "confidence"
            "catalyst":      self.catalyst,
            "sector_signal": self.sector_signal,
        }
        if self.exit_symbol is not None:
            d["exit_symbol"] = self.exit_symbol
        if self.entry_symbol is not None:
            d["entry_symbol"] = self.entry_symbol
        return d


@dataclass
class OptionsAction:
    """
    Risk kernel output for options trades (Account 2).

    Produced by risk_kernel.process_options_idea().
    Consumed by options_execution.submit_structure() via to_dict().

    The to_dict() output maintains backward compatibility with the dict
    format expected by submit_options_order() until Phase 4 wires in
    options_execution.
    """
    symbol:          str              # canonical underlying (e.g. "AAPL")
    action:          str              # "buy_option" | "sell_option_spread" |
                                      # "buy_straddle" | "close_option" | "hold"
    option_strategy: OptionStrategy
    expiration:      str              # "YYYY-MM-DD"
    long_strike:     Optional[float]
    short_strike:    Optional[float]
    contracts:       int
    max_cost_usd:    float
    tier:            Tier
    conviction:      Conviction
    catalyst:        str
    direction:       Direction
    iv_rank:         Optional[float]  = None
    delta:           Optional[float]  = None
    rationale:       str              = ""
    confidence:      float            = 0.0    # synthesis confidence 0-1
    reason:          str              = ""     # set when action == "hold"
    # Traceability — not serialised to dict, not included in repr
    source_idea:     Optional[TradeIdea] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        """Produce dict compatible with submit_options_order() / options_execution."""
        return {
            "action":          self.action,
            "symbol":          self.symbol,
            "option_strategy": self.option_strategy.value,
            "expiration":      self.expiration,
            "long_strike":     self.long_strike,
            "short_strike":    self.short_strike,
            "contracts":       self.contracts,
            "max_cost_usd":    self.max_cost_usd,
            "tier":            self.tier.value,
            "confidence":      self.conviction.value,
            "catalyst":        self.catalyst,
            "direction":       self.direction.value,
            "iv_rank":         self.iv_rank,
            "delta":           self.delta,
            "rationale":       self.rationale,
            "reason":          self.reason,
        }


# ─────────────────────────────────────────────────────────────────────────────
# Broker state (normalised from Alpaca API objects)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NormalizedPosition:
    """
    Normalised view of a single open position.

    symbol     — canonical internal format (BTC/USD for crypto)
    alpaca_sym — Alpaca position key (BTCUSD for crypto)
    """
    symbol:          str
    alpaca_sym:      str
    qty:             float
    avg_entry_price: float
    current_price:   float
    market_value:    float
    unrealized_pl:   float
    unrealized_plpc: float    # fraction, not percentage (e.g. 0.023 = 2.3%)
    is_crypto_pos:   bool

    @property
    def side(self) -> str:
        """Position side derived from qty sign: 'long' or 'short'."""
        return "short" if self.qty < 0 else "long"

    @classmethod
    def from_alpaca_position(cls, pos) -> "NormalizedPosition":
        """
        Build from an alpaca-py Position object.
        No alpaca import needed — accesses attributes via duck typing.
        """
        raw_sym   = getattr(pos, "symbol", "")
        canonical = normalize_symbol(raw_sym)
        return cls(
            symbol=canonical,
            alpaca_sym=alpaca_symbol(canonical),
            qty=float(pos.qty),
            avg_entry_price=float(pos.avg_entry_price),
            current_price=float(pos.current_price),
            market_value=float(pos.market_value),
            unrealized_pl=float(pos.unrealized_pl),
            unrealized_plpc=float(pos.unrealized_plpc),
            is_crypto_pos=is_crypto(raw_sym),
        )


@dataclass
class NormalizedOrder:
    """
    Normalised view of a single open broker order.

    symbol     — canonical internal format
    alpaca_sym — Alpaca order symbol
    side       — "buy" | "sell"
    order_type — "market" | "limit" | "stop" | "stop_limit"
    status     — "open" | "filled" | "cancelled" | "partially_filled" | etc.
    """
    order_id:      str
    symbol:        str
    alpaca_sym:    str
    side:          str
    order_type:    str
    qty:           float
    filled_qty:    float
    stop_price:    Optional[float]
    limit_price:   Optional[float]
    status:        str
    time_in_force: str = "day"

    @classmethod
    def from_alpaca_order(cls, order) -> "NormalizedOrder":
        """
        Build from an alpaca-py Order object.
        Strips enum prefixes (e.g. "OrderSide.BUY" -> "buy").
        """
        raw_sym   = getattr(order, "symbol", "")
        canonical = normalize_symbol(raw_sym)

        def _enum_str(attr: str) -> str:
            return str(getattr(order, attr, "")).lower().split(".")[-1]

        return cls(
            order_id=str(getattr(order, "id", "")),
            symbol=canonical,
            alpaca_sym=alpaca_symbol(canonical),
            side=_enum_str("side"),
            order_type=_enum_str("type"),
            qty=float(getattr(order, "qty", 0) or 0),
            filled_qty=float(getattr(order, "filled_qty", 0) or 0),
            stop_price=_maybe_float(getattr(order, "stop_price", None)),
            limit_price=_maybe_float(getattr(order, "limit_price", None)),
            status=_enum_str("status"),
            time_in_force=_enum_str("time_in_force") or "day",
        )


@dataclass
class BrokerSnapshot:
    """
    Complete normalised view of broker state at a point in time.

    Used by:
      - risk_kernel  (sizing, exposure checks, PDT guard)
      - reconciliation (desired-state diff engine)
    """
    positions:    list[NormalizedPosition]
    open_orders:  list[NormalizedOrder]
    equity:       float
    cash:         float
    buying_power: float
    timestamp:    str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    # ── Convenience accessors ─────────────────────────────────────────────────

    @property
    def position_by_symbol(self) -> dict[str, NormalizedPosition]:
        """Index positions by canonical symbol."""
        return {p.symbol: p for p in self.positions}

    @property
    def orders_by_symbol(self) -> dict[str, list[NormalizedOrder]]:
        """Index open orders by canonical symbol."""
        result: dict[str, list[NormalizedOrder]] = {}
        for o in self.open_orders:
            result.setdefault(o.symbol, []).append(o)
        return result

    @property
    def exposure_dollars(self) -> float:
        """Total market value of all open positions."""
        return sum(p.market_value for p in self.positions)

    @property
    def exposure_pct(self) -> float:
        """Total exposure as a fraction of equity (e.g. 0.18 = 18%)."""
        return (self.exposure_dollars / self.equity) if self.equity > 0 else 0.0

    @property
    def held_symbols(self) -> set[str]:
        """Set of canonical symbols with open positions (long or short, qty != 0)."""
        return {p.symbol for p in self.positions if p.qty != 0}


# ─────────────────────────────────────────────────────────────────────────────
# Options structure types (Account 2)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class OptionsLeg:
    """
    A single leg within an options structure.

    occ_symbol  — full OCC format: AAPL230120C00150000
    underlying  — canonical underlying ticker
    side        — "buy" | "sell"
    option_type — "call" | "put"
    """
    occ_symbol:   str
    underlying:   str
    side:         str             # "buy" | "sell"
    qty:          int
    option_type:  str             # "call" | "put"
    strike:       float
    expiration:   str             # "YYYY-MM-DD"
    order_id:      Optional[str]   = None
    filled_price:  Optional[float] = None
    bid:           Optional[float] = None
    ask:           Optional[float] = None
    mid:           Optional[float] = None
    delta:         Optional[float] = None
    open_interest: Optional[int]   = None
    volume:        Optional[int]   = None


@dataclass
class OptionsStructure:
    """
    A logical multi-leg options position for Account 2.

    Account 2 always thinks in structures, never individual legs.
    Persisted to data/account2/positions/structures.json.

    Leg ordering rule (enforced in options_execution):
      When submitting spreads, the long leg is always submitted before
      the short leg to avoid naked short positions at any point.
    """
    structure_id: str
    underlying:   str               # canonical (e.g. "AAPL")
    strategy:     OptionStrategy
    lifecycle:    StructureLifecycle
    legs:         list[OptionsLeg]
    contracts:    int
    max_cost_usd: float
    opened_at:    str               # ISO-8601 UTC
    catalyst:     str
    tier:         Tier
    iv_rank:      Optional[float]   = None
    order_ids:    list[str]         = field(default_factory=list)
    closed_at:    Optional[str]     = None
    realized_pnl:   Optional[float]  = None
    notes:          str              = ""
    direction:      str              = ""    # "bullish"|"bearish"|"neutral"
    expiration:     str              = ""    # top-level convenience field (matches legs)
    long_strike:    Optional[float]  = None
    short_strike:   Optional[float]  = None
    debit_paid:     Optional[float]  = None  # actual fill economics (per contract × 100)
    max_profit_usd: Optional[float]  = None
    audit_log:      list             = field(default_factory=list)
    # Roll tracking (set when this structure replaces a prior one)
    roll_group_id:           Optional[str]  = None   # links all structures in a roll chain
    roll_from_structure_id:  Optional[str]  = None   # immediate predecessor structure_id
    roll_reason:             str            = ""     # e.g. "dte_approaching", "underlying_moved"
    thesis_status:           str            = "intact"  # "intact" | "weakened" | "invalidated"
    # Close/roll audit trail (D13)
    close_reason_code:       Optional[str]  = None   # e.g. "stop_loss_hit", "expiry_approaching"
    close_reason_detail:     Optional[str]  = None   # free-text detail with timestamp
    roll_reason_code:        Optional[str]  = None   # e.g. "dte_approaching", "time_stop"
    roll_reason_detail:      Optional[str]  = None   # free-text roll detail
    rolled_to_structure_id:  Optional[str]  = None   # set when new structure is linked
    initiated_by:            Optional[str]  = None   # "auto_rule" | method name

    def is_terminal(self) -> bool:
        """True if structure has reached a final, non-reversible state."""
        return self.lifecycle in (
            StructureLifecycle.CLOSED,
            StructureLifecycle.REJECTED,
            StructureLifecycle.EXPIRED,
            StructureLifecycle.CANCELLED,
        )

    def is_open(self) -> bool:
        """True if structure is actively live (fully or partially filled)."""
        return self.lifecycle in (
            StructureLifecycle.FULLY_FILLED,
            StructureLifecycle.PARTIALLY_FILLED,
        )

    def net_debit_per_contract(self) -> Optional[float]:
        """
        Net debit paid per contract (positive = debit paid, negative = credit received).
        Returns debit_paid if already set; otherwise computes from leg filled_prices.
        None if any leg has no fill data.
        """
        if self.debit_paid is not None:
            return self.debit_paid
        if not self.legs:
            return None
        total = 0.0
        for leg in self.legs:
            if leg.filled_price is None:
                return None
            if leg.side == "buy":
                total += leg.filled_price
            else:
                total -= leg.filled_price
        return round(total, 4)

    def add_audit(self, msg: str) -> None:
        """Append a timestamped audit entry to audit_log."""
        self.audit_log.append({
            "ts":  datetime.now(timezone.utc).isoformat(),
            "msg": msg,
        })

    def to_dict(self) -> dict:
        """Serialise for JSON persistence."""
        d = asdict(self)
        # asdict() already produces .value for str-Enum fields; re-assign
        # defensively to guard against any Python version variation.
        d["strategy"]  = self.strategy.value
        d["lifecycle"] = self.lifecycle.value
        d["tier"]      = self.tier.value
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "OptionsStructure":
        """Deserialise from JSON persistence."""
        legs = [
            OptionsLeg(
                occ_symbol=leg["occ_symbol"],
                underlying=leg["underlying"],
                side=leg["side"],
                qty=int(leg["qty"]),
                option_type=leg["option_type"],
                strike=float(leg["strike"]),
                expiration=leg["expiration"],
                order_id=leg.get("order_id"),
                filled_price=_maybe_float(leg.get("filled_price")),
                bid=_maybe_float(leg.get("bid")),
                ask=_maybe_float(leg.get("ask")),
                mid=_maybe_float(leg.get("mid")),
                delta=_maybe_float(leg.get("delta")),
                open_interest=int(leg["open_interest"]) if leg.get("open_interest") is not None else None,
                volume=int(leg["volume"]) if leg.get("volume") is not None else None,
            )
            for leg in d.get("legs", [])
        ]
        try:
            strategy = OptionStrategy(d["strategy"])
        except (ValueError, KeyError):
            strategy = OptionStrategy.SINGLE_CALL
        try:
            lifecycle = StructureLifecycle(d["lifecycle"])
        except (ValueError, KeyError):
            lifecycle = StructureLifecycle.CANCELLED
        try:
            tier = Tier(d.get("tier", "core"))
        except ValueError:
            tier = Tier.CORE
        return cls(
            structure_id=d["structure_id"],
            underlying=normalize_symbol(d["underlying"]),
            strategy=strategy,
            lifecycle=lifecycle,
            legs=legs,
            contracts=int(d["contracts"]),
            max_cost_usd=float(d["max_cost_usd"]),
            opened_at=d["opened_at"],
            catalyst=d.get("catalyst", ""),
            tier=tier,
            iv_rank=_maybe_float(d.get("iv_rank")),
            order_ids=list(d.get("order_ids", [])),
            closed_at=d.get("closed_at"),
            realized_pnl=_maybe_float(d.get("realized_pnl")),
            notes=d.get("notes", ""),
            direction=d.get("direction", ""),
            expiration=d.get("expiration", ""),
            long_strike=_maybe_float(d.get("long_strike")),
            short_strike=_maybe_float(d.get("short_strike")),
            debit_paid=_maybe_float(d.get("debit_paid")),
            max_profit_usd=_maybe_float(d.get("max_profit_usd")),
            audit_log=list(d.get("audit_log", [])),
            roll_group_id=d.get("roll_group_id"),
            roll_from_structure_id=d.get("roll_from_structure_id"),
            roll_reason=d.get("roll_reason", ""),
            thesis_status=d.get("thesis_status", "intact"),
            close_reason_code=d.get("close_reason_code"),
            close_reason_detail=d.get("close_reason_detail"),
            roll_reason_code=d.get("roll_reason_code"),
            roll_reason_detail=d.get("roll_reason_detail"),
            rolled_to_structure_id=d.get("rolled_to_structure_id"),
            initiated_by=d.get("initiated_by"),
        )


@dataclass
class StructureProposal:
    """
    A candidate options structure built by options_builder, before submission.

    Passed to the four-way debate and order_executor_options for confirmation.
    Not persisted directly — converted to OptionsStructure on submission.
    direction uses the Direction enum: Direction.BULLISH / BEARISH / NEUTRAL.
    """
    symbol:         str
    strategy:       OptionStrategy
    direction:      Direction
    conviction:     float
    iv_rank:        float
    max_cost_usd:   float
    target_dte_min: int       = 7
    target_dte_max: int       = 45
    rationale:      str       = ""
    signal_score:   int       = 0
    proposed_at:    str       = ""      # ISO-8601 UTC timestamp


@dataclass
class A2FeaturePack:
    """Normalized feature object for A2 options decision pipeline."""
    symbol: str
    # Directional (from A1 signal scores)
    a1_signal_score: float          # 0-100 from signal_scores.json
    a1_direction: str               # "bullish" | "bearish" | "neutral"
    trend_score: Optional[float]    # from intraday_cache if available
    momentum_score: Optional[float]
    sector_alignment: str           # sector from symbol_metadata
    # IV / structure (from options_data)
    iv_rank: float
    iv_environment: str             # "very_cheap"|"cheap"|"neutral"|"expensive"|"very_expensive"
    term_structure_slope: Optional[float]
    skew: Optional[float]
    expected_move_pct: float
    # Flow / positioning (UW — all Optional, None until UW integrated)
    flow_imbalance_30m: Optional[float]
    sweep_count: Optional[int]
    gex_regime: Optional[str]
    oi_concentration: Optional[dict]
    # Event state
    earnings_days_away: Optional[int]
    macro_event_flag: bool
    # Trade geometry
    premium_budget_usd: float
    liquidity_score: float          # 0-1
    # Metadata
    built_at: str                   # ISO timestamp
    data_sources: list[str]         # which sources populated this pack


# ─────────────────────────────────────────────────────────────────────────────
# A2 pipeline stage contracts (Stage 1 → Stage 2 → Stage 3)
# ─────────────────────────────────────────────────────────────────────────────

NO_TRADE_REASONS: list[str] = [
    "no_signal_scores",
    "no_candidates_after_router",
    "no_candidates_after_veto",
    "debate_low_confidence",
    "debate_parse_failed",
    "debate_rejected_all",
    "execution_rejected",
    "execution_error",
    "preflight_halt",
    "session_not_market",
    "obs_mode_active",
    "rollback_active",
]


def validate_no_trade_reason(reason: str) -> str:
    """Assert reason is in NO_TRADE_REASONS, return it. Raises ValueError if not."""
    if reason not in NO_TRADE_REASONS:
        raise ValueError(
            f"no_trade_reason={reason!r} not in NO_TRADE_REASONS taxonomy. "
            f"Valid values: {NO_TRADE_REASONS}"
        )
    return reason


def _get_git_commit() -> Optional[str]:
    try:
        return _subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=_subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return None


@dataclass
class A2CandidateSet:
    """Output of Stage 1 → input to Stage 2."""
    symbol: str
    pack: A2FeaturePack
    allowed_structures: list[str]       # from _route_strategy()
    router_rule_fired: str              # e.g. "RULE5" — for logging/audit
    generated_candidates: list[dict]    # from generate_candidate_structures()
    vetoed_candidates: list[dict]       # {candidate_id, reason}
    surviving_candidates: list[dict]    # candidates that passed veto
    generation_errors: list[str]        # non-fatal errors during generation
    built_at: str                       # ISO timestamp


@dataclass
class A2DecisionRecord:
    """Full audit trail for one A2 decision cycle."""
    decision_id: str
    session_tier: str
    candidate_sets: list                # list[A2CandidateSet]
    debate_input: Optional[str]         # full prompt sent to Claude
    debate_output_raw: Optional[str]    # raw Claude response
    debate_parsed: Optional[dict]       # parsed JSON or None
    selected_candidate: Optional[dict]  # the winning candidate
    execution_result: Optional[str]     # "submitted"|"rejected"|"no_trade"|"error"
    no_trade_reason: Optional[str]      # reject taxonomy (see NO_TRADE_REASONS)
    elapsed_seconds: float
    schema_version: int = 1             # bump when fields added/removed/renamed
    code_version: Optional[str] = field(default_factory=_get_git_commit)
    built_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ─────────────────────────────────────────────────────────────────────────────
# Validation helpers
# ─────────────────────────────────────────────────────────────────────────────

def validate_trade_idea(idea: TradeIdea) -> tuple[bool, str]:
    """
    Basic structural validation of a TradeIdea before passing to risk kernel.

    Returns (ok: bool, reason: str).
    Does NOT check market state, equity, or exposure — those live in risk_kernel.
    Advisory, not authoritative — risk_kernel is the final gate.
    """
    if not idea.symbol:
        return False, "missing symbol"
    if idea.action == AccountAction.BUY:
        if not idea.catalyst or idea.catalyst.lower() in ("", "none", "null", "no"):
            return False, "buy requires a named catalyst"
        if idea.conviction <= 0.35:
            return False, "low conviction buy — risk kernel will reject; elevate or drop"
    if idea.action == AccountAction.REALLOCATE:
        if not idea.exit_symbol:
            return False, "reallocate requires exit_symbol"
        if not idea.entry_symbol:
            return False, "reallocate requires entry_symbol"
    if idea.advisory_stop_pct is not None and idea.advisory_stop_pct <= 0:
        return False, f"advisory_stop_pct={idea.advisory_stop_pct} must be > 0"
    if idea.advisory_target_r is not None and idea.advisory_target_r < 1.0:
        return False, f"advisory_target_r={idea.advisory_target_r} must be >= 1.0"
    return True, "ok"


def validate_claude_decision(data: dict) -> "ClaudeDecision":
    """
    Parse a raw Claude JSON response dict into ClaudeDecision.

    Handles both intent-based format (ideas[]) and legacy format (actions[]).
    Legacy detection: "actions" key present and "ideas" key absent.

    Mapping for validate_claude_decision:
      intent "enter_long"  → AccountAction.BUY
      intent "enter_short" → AccountAction.SELL
      intent "close"       → AccountAction.CLOSE
      intent "reduce"      → AccountAction.SELL
      intent "hold"        → AccountAction.HOLD
      intent "monitor"     → AccountAction.HOLD
    """
    import logging as _logging
    _log = _logging.getLogger(__name__)

    # ── Legacy format detection ────────────────────────────────────────────────
    if "actions" in data and "ideas" not in data:
        _log.warning(
            "[SCHEMA] WARNING: legacy Claude format detected — converting to intent-based"
        )
        data = _convert_legacy_decision(data)

    # ── Intent → AccountAction map ─────────────────────────────────────────────
    _intent_map: dict[str, AccountAction] = {
        "enter_long":  AccountAction.BUY,
        "enter_short": AccountAction.SELL,
        "close":       AccountAction.CLOSE,
        "reduce":      AccountAction.SELL,
        "hold":        AccountAction.HOLD,
        "monitor":     AccountAction.HOLD,
    }
    _tier_map: dict[str, Tier] = {
        "core":     Tier.CORE,
        "dynamic":  Tier.DYNAMIC,
        "intraday": Tier.INTRADAY,
        "crypto":   Tier.CORE,
    }

    # ── Parse ideas ────────────────────────────────────────────────────────────
    ideas: list[TradeIdea] = []
    for raw in data.get("ideas", []):
        try:
            intent_str = str(raw.get("intent", "hold")).lower()
            action     = _intent_map.get(intent_str, AccountAction.HOLD)
            tier_raw   = str(raw.get("tier_preference", raw.get("tier", "core"))).lower()
            tier       = _tier_map.get(tier_raw, Tier.CORE)
            dir_raw    = str(raw.get("direction", "neutral")).lower()
            try:
                direction = Direction(dir_raw)
            except ValueError:
                direction = Direction.NEUTRAL

            conv_raw = raw.get("conviction", 0.60)
            try:
                conviction = float(conv_raw)
            except (TypeError, ValueError):
                conviction = 0.60
            conviction = max(0.0, min(1.0, conviction))

            # Reallocate detection: enter_long + exit_symbol present
            if intent_str == "enter_long" and raw.get("exit_symbol"):
                action = AccountAction.REALLOCATE

            # Option strategy hint
            opt_hint = None
            hint_raw = raw.get("option_strategy_hint")
            if hint_raw:
                try:
                    opt_hint = OptionStrategy(str(hint_raw).lower())
                except ValueError:
                    pass

            ideas.append(TradeIdea(
                symbol=normalize_symbol(str(raw.get("symbol", ""))),
                action=action,
                tier=tier,
                conviction=conviction,
                direction=direction,
                catalyst=str(raw.get("catalyst", "")),
                sector_signal=str(raw.get("sector_signal", "")),
                advisory_stop_pct=_maybe_float(raw.get("advisory_stop_pct")),
                advisory_target_r=_maybe_float(raw.get("advisory_target_r")),
                notes=str(raw.get("notes", "")),
                intent=intent_str,
                option_strategy_hint=opt_hint,
                exit_symbol=raw.get("exit_symbol"),
                entry_symbol=raw.get("entry_symbol"),
            ))
        except Exception as _exc:
            _log.warning("[SCHEMA] Skipping unparseable idea %r: %s", raw, _exc)

    # ── regime_view normalisation ──────────────────────────────────────────────
    regime_raw = str(data.get("regime_view", data.get("regime", "caution"))).lower()
    # Map old regime values to new
    _regime_map = {"normal": "risk_on", "risk_on": "risk_on", "risk_off": "risk_off",
                   "caution": "caution", "halt": "halt"}
    regime_view = _regime_map.get(regime_raw, "caution")

    return ClaudeDecision(
        reasoning=str(data.get("reasoning", "")),
        regime_view=regime_view,
        ideas=ideas,
        notes=str(data.get("notes", "")),
        holds=[str(h) for h in data.get("holds", [])],
        concerns=str(data.get("concerns", "")),
    )


def _convert_legacy_decision(data: dict) -> dict:
    """Convert legacy actions[] response to new ideas[] format."""
    _intent_legacy: dict[str, str] = {
        "buy":        "enter_long",
        "sell":       "enter_short",
        "close":      "close",
        "hold":       "hold",
        "reallocate": "enter_long",
    }
    _dir_legacy: dict[str, str] = {
        "buy": "bullish", "sell": "bearish", "close": "neutral",
        "hold": "neutral", "reallocate": "bullish",
    }
    _conv_legacy: dict[str, float] = {"high": 0.80, "medium": 0.60, "low": 0.30}

    ideas = []
    holds = []
    for a in data.get("actions", []):
        act = str(a.get("action", "hold")).lower()
        if act in ("hold", "monitor"):
            sym = a.get("symbol", "")
            if sym:
                holds.append(sym)
            continue
        ideas.append({
            "intent":        _intent_legacy.get(act, "hold"),
            "symbol":        a.get("symbol", ""),
            "conviction":    _conv_legacy.get(
                str(a.get("confidence", "medium")).lower(), 0.60
            ),
            "tier_preference": a.get("tier", "core"),
            "catalyst":      a.get("catalyst", ""),
            "sector_signal": a.get("sector_signal", ""),
            "direction":     _dir_legacy.get(act, "neutral"),
            "advisory_stop_pct":  None,   # legacy had absolute prices, not pct
            "advisory_target_r":  None,
            "exit_symbol":   a.get("exit_symbol"),
            "entry_symbol":  a.get("entry_symbol"),
        })

    return {
        "reasoning":   data.get("reasoning", ""),
        "regime_view": data.get("regime", "caution"),
        "ideas":       ideas,
        "holds":       holds,
        "notes":       data.get("notes", ""),
        "concerns":    "",
    }


def validate_broker_action(action: BrokerAction) -> tuple[bool, str]:
    """
    Structural completeness check for a BrokerAction before execution.

    Returns (ok: bool, reason: str).
    Lightweight — does not replicate the full order_executor.validate_action() logic.
    """
    if not action.symbol:
        return False, "missing symbol"
    if action.action == AccountAction.BUY:
        if action.qty is None or action.qty <= 0:
            return False, f"qty={action.qty} must be > 0 for buy"
        if action.stop_loss is None:
            return False, "buy action requires stop_loss"
        if action.take_profit is None:
            return False, "buy action requires take_profit"
        if action.stop_loss >= action.take_profit:
            return False, (
                f"stop_loss={action.stop_loss} must be < "
                f"take_profit={action.take_profit}"
            )
    if action.action in (AccountAction.SELL, AccountAction.CLOSE):
        if action.qty is None or action.qty <= 0:
            return False, f"qty={action.qty} must be > 0 for {action.action.value}"
    if action.action == AccountAction.REALLOCATE:
        if not action.exit_symbol:
            return False, "reallocate requires exit_symbol"
        if not action.entry_symbol:
            return False, "reallocate requires entry_symbol"
    return True, "ok"


# ─────────────────────────────────────────────────────────────────────────────
# Required fields manifest
#
# Used by risk_kernel and validation layers to verify completeness before
# submission without duplicating field-name strings in every module.
# ─────────────────────────────────────────────────────────────────────────────

REQUIRED_FIELDS_BY_ACTION: dict[str, list[str]] = {
    "buy": [
        "symbol", "qty", "stop_loss", "take_profit",
        "tier", "confidence", "catalyst",
    ],
    "sell": [
        "symbol", "qty",
    ],
    "close": [
        "symbol", "qty",
    ],
    "hold": [
        "symbol",
    ],
    "reallocate": [
        "symbol", "qty", "exit_symbol", "entry_symbol",
    ],
    "buy_option": [
        "symbol", "option_strategy", "expiration",
        "long_strike", "contracts", "max_cost_usd",
    ],
    "sell_option_spread": [
        "symbol", "option_strategy", "expiration",
        "long_strike", "short_strike", "contracts", "max_cost_usd",
    ],
    "buy_straddle": [
        "symbol", "option_strategy", "expiration",
        "long_strike", "contracts", "max_cost_usd",
    ],
    "close_option": [
        "symbol",
    ],
}


__all__ = [
    # Symbol helpers
    "normalize_symbol", "is_crypto", "alpaca_symbol", "yfinance_symbol",
    # Enums
    "AccountAction", "OptionStrategy", "Conviction", "Direction",
    "Tier", "SessionTier", "StructureLifecycle",
    # Claude output
    "TradeIdea", "ClaudeDecision", "validate_claude_decision",
    # Signal data
    "SignalScore",
    # Risk kernel output
    "BrokerAction", "OptionsAction",
    # Broker state
    "NormalizedPosition", "NormalizedOrder", "BrokerSnapshot",
    # Options structure
    "OptionsLeg", "OptionsStructure", "StructureProposal", "A2FeaturePack",
    # A2 stage contracts
    "NO_TRADE_REASONS", "validate_no_trade_reason",
    "A2CandidateSet", "A2DecisionRecord",
    # Validation
    "validate_trade_idea", "validate_broker_action",
    # Manifest
    "REQUIRED_FIELDS_BY_ACTION",
]
