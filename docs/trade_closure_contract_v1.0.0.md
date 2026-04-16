# BullBearBot Trade Closure Definition and Data Contract
# Version: v1.0.0
# Owner: production core owner
# Last updated: 2026-04-16
# Status: LOCKED — changes require version bump and schema owner approval
# Depends on: taxonomy_v1.0.0.md (close_reason labels)

---

## 1. Versioning Policy

- Minor addition (new field, new closure_source label): bump minor → v1.1.0
- Breaking change (field rename, removal, status model change): bump major → v2.0.0
- Schema version field required on all closure artifacts
- Backward-compatible readers required for at least one prior version

---

## 2. Canonical Definition of a Closed Trade

A trade is considered closed when ALL of the following are true:

1. The tracked position for that trade_id / decision_id lineage is fully exited
2. No remaining tracked exposure exists for that trade lineage in the broker account
3. A closure record has been persisted with status = "closed"
4. Realized outcome fields are present OR explicitly null-pending

### Locked Design Opinion

A trade MAY be closed before full enrichment is complete.

- Closure event exists → realized fields may be null briefly → backfill is allowed
- The closure record is the source of truth for the fact of closure
- Enrichment is a subsequent operation, not a precondition for closure

---

## 3. What is NOT a Closed Trade

The following must never appear in closed-trade analysis or realized PnL stats:

| Event | Why Excluded |
|-------|-------------|
| HOLD decision | A decision artifact, not a trade lifecycle event |
| No-action cycle | Bot evaluated and chose not to act |
| Kernel rejection | Trade never entered the system |
| Executor rejection | Trade submitted but rejected before broker |
| Near miss | Signal scored but not submitted |
| Partial reduction | Exposure remains — trade is still open |
| Open position with stale thesis | Still open; thesis quality is a separate concern |
| Pending exit order not yet filled | Order submitted, position not yet closed |
| Paper trading HOLD recorded as loss | BUG-003 — explicitly excluded, corrected in T0.8 |

### Locked Design Opinion

HOLD is a decision artifact.

- It may appear in decision-quality analysis
- It must never appear in realized trade closure analysis
- This is non-negotiable

---

## 4. Trade Lifecycle States

| Status | Definition |
|--------|-----------|
| `open` | Position entered, exposure exists |
| `partially_closed` | Position reduced but exposure remains |
| `closed` | Fully exited, closure record persisted |
| `reconciled_closed` | Closed and reconciled against broker state |

### State Transitions

```
open → partially_closed     (partial fill or partial exit)
open → closed               (full exit)
partially_closed → closed   (remaining exposure exited)
closed → reconciled_closed  (broker reconciliation complete)
```

No backward transitions allowed.
`reconciled_closed` is the terminal state.

### Implementation Note for v1.0.0

`partially_closed` is schema-valid and reader-supported in v1.0.0.

Writer-optional for initial implementation:
- The contract supports it now
- Downstream code can understand it
- If the current executor path does not yet emit it consistently, that is
  acceptable for the first pass
- Full partial-close emission coverage is not required for Foundation Gate exit
  unless current execution paths already support it

---

## 5. Closure Event Schema

### Required at Closure Record Creation

| Field | Type | Notes |
|-------|------|-------|
| `trade_id` | string | Stable ID for this trade |
| `decision_id` | string | Linked decision from attribution.py |
| `symbol` | string | Ticker or asset |
| `account` | string | "A1" or "A2" |
| `opened_at` | ISO 8601 | Timestamp of position entry |
| `closed_at` | ISO 8601 | Timestamp of full exit |
| `qty` | float | Total quantity traded |
| `close_reason` | taxonomy label | From taxonomy_v1.0.0.md close_reason dimension |
| `closure_source` | source label | From Section 7 below |
| `status` | string | Must be "closed" |
| `schema_version` | string | Must be "1.0.0" |

### Nullable at Creation, Backfillable Later

| Field | Type | Notes |
|-------|------|-------|
| `entry_price` | float or null | Average entry price |
| `exit_price` | float or null | Average exit price |
| `fill_price` | float or null | Actual broker fill price (from T0.1) |
| `gross_realized_pnl_usd` | float or null | Before fees |
| `net_realized_pnl_usd` | float or null | After fees if available |
| `realized_pnl_r` | float or null | PnL in R-multiples if ATR stop used |
| `holding_period_minutes` | int or null | Time from open to close |
| `holding_period_hours` | float or null | Derived from minutes |

### Optional Enrichment Fields

| Field | Type | Notes |
|-------|------|-------|
| `linked_order_ids` | array of strings | Broker order IDs |
| `linked_position_id` | string or null | Broker position ID if available |
| `notes` | string or null | Human or system notes |
| `thesis_checksum_id` | string or null | Link to T2.1 checksum if exists |
| `forensic_review_id` | string or null | Link to T2.3 review if exists |

### Fields That Explicitly Do Not Exist

These must never be added without a version bump:

- No `is_loss` boolean — derive from net_realized_pnl_usd
- No `is_win` boolean — derive from net_realized_pnl_usd
- No `hold_count` — HOLDs are not closure events
- No free-text `outcome` field — use close_reason + realized PnL

---

## 6. Source-of-Truth Hierarchy

| Layer | Role | File |
|-------|------|------|
| **Source of truth** | What happened | `trades.jsonl` or execution event ledger |
| **Enriched view** | Analysis and outcomes | `decision_outcomes.py` |
| **Aggregate summary** | Performance metrics only | `performance.json` |

### Rules

- `trades.jsonl` owns closure facts — timestamps, prices, quantities
- `decision_outcomes.py` may enrich but never contradict the event ledger
- `performance.json` is derived — never the source of truth for any closure event
- Discrepancies between layers must be resolved by the event ledger,
  not performance.json

---

## 7. Closure Source Labels

**Definition:** What mechanically caused the position to be closed?

Distinct from `close_reason` (semantic/strategic) in taxonomy_v1.0.0.md.
Both fields are required on every closure record. They answer different questions.

| Label | Definition |
|-------|-----------|
| `broker_fill` | Generic broker execution fill |
| `stop_order_fill` | Stop loss order filled at broker |
| `take_profit_fill` | Take profit limit order filled |
| `manual_command` | Human-initiated close command |
| `deadline_enforcer` | Time-bound action enforcer fired (TSM-style exits) |
| `risk_containment` | Drawdown guard, VIX threshold, or regime halted position |
| `reconciliation_action` | Closure to reconcile with broker state |
| `expiry` | Options expiry or instrument expiry |
| `unknown` | Source not determinable — always valid |

### Semantic vs Mechanical Separation Example

```
close_reason    = thesis_invalidated   (why — strategic)
closure_source  = manual_command       (how — mechanical)
```

---

## 8. Data Availability Rules

### At Closure Record Creation

Must be present:
- trade_id, decision_id, symbol, account
- opened_at, closed_at
- qty
- close_reason (or `unknown`)
- closure_source (or `unknown`)
- status = "closed"
- schema_version = "1.0.0"

May be null:
- All price fields
- All PnL fields
- holding_period fields
- Optional enrichment fields

### Backfill Policy

- Realized PnL fields may be backfilled within 15 minutes of closure
- If broker fill price is unavailable after 1 hour, mark as null for the
  current artifact version and flag for manual reconciliation
- Backfill writes must preserve original `closed_at` timestamp
- No in-place rewrites of historical closure records
- Backfill appends a separate enrichment record linked by trade_id

---

## 9. HOLD and No-Action Handling

HOLD decisions produce a decision record, not a trade closure record.

| Event | Goes Into | Never Goes Into |
|-------|----------|----------------|
| HOLD decision | decisions.json | trades.jsonl closure records |
| No-action cycle | decisions.json | Any trade analysis |
| HOLD with stop update | decisions.json + stop refresh | Trade PnL stats |

### BUG-003 Fix Requirement (Part of T0.8)

The current behavior where `stock_hold` produces a loss record in
`performance.json` must be corrected before T2.3 (forensic reviewer) is built.

**T0.8 Acceptance Criteria:**
- HOLD decisions never generate closure records
- HOLD decisions never affect realized PnL metrics
- HOLD decisions never affect win/loss counts
- Existing code paths that currently treat stock_hold as loss are corrected
- One regression test added for HOLD-as-loss contamination

**Correct behavior:**
- HOLD → decision record with action = "hold"
- HOLD → NOT a loss, NOT a win, NOT a closed trade
- HOLD → eligible for decision quality analysis only
- HOLD → never contributes to win rate, loss rate, or realized PnL stats

---

## 10. Dependency Map

| Ticket | Blocked by |
|--------|-----------|
| T2.3 Post-trade forensic reviewer | T0.8 (this document) + T1.1 |
| T2.1 Thesis checksum | T0.8 (closure schema for linking) |
| T3.3 Module ROI with outcomes | T0.8 (clean closure records required) |
| T5.1 Close/roll reason persistence (A2) | T0.8 (shares closure source taxonomy) |

---

*Last updated: 2026-04-16*
*Status: LOCKED — changes require version bump and schema owner approval*
*BUG-003 fix is a hard Foundation Gate requirement before Phase 1*
