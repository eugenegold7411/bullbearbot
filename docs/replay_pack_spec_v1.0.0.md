# Replay Pack Spec v1.0.0

> Governs the scenario library implemented in `replay_packs.py`.
> Storage: `data/replay_packs/` with `manifest.json`.

---

## 1. What Is a Replay Pack

A replay pack is a named snapshot of inputs (regime, signals, positions, macro backdrop)
stored as a JSON file. Packs can be replayed against the A1 decision engine via
`replay_debugger.replay_a1_decision()` to test what the model would say given a specific
set of conditions — without affecting live state.

Use cases:
- Regression testing after prompt changes (did the model regress on a known-good scenario?)
- Exploring what-if forks (what would the model do if VIX were 38 instead of 14?)
- Scenario library for the weekly review (Agent 4 Backtest Analyst)
- Pre-launch smoke tests: seed 2 synthetic packs and verify model produces expected verdicts

---

## 2. Pack Types

| Pack Type | Description |
|-----------|-------------|
| `trade_win` | Actual winning trade — test model can identify the bullish signal |
| `trade_loss` | Actual losing trade — test model can recognize the risk |
| `trade_rejected` | Trade rejected by risk kernel — test kernel alignment |
| `trade_near_miss` | Shadow lane near-miss — what-if analysis |
| `weekly_review` | Weekly review scenario — replay agent inputs |
| `incident` | Incident record — test recovery reasoning |
| `a2_approved` | A2 options proposal that was approved |
| `a2_vetoed` | A2 options proposal that was vetoed |
| `regime_transition` | Regime score shifted significantly mid-pack |
| `vix_spike` | VIX elevated scenario (> 25) |
| `synthetic` | Hand-crafted test scenario, no live origin |

---

## 3. Storage Format

```
data/replay_packs/
├── manifest.json                          # index of all packs
├── pack_{YYYYMMDD_HHMMSS}_{hex6}.json     # individual pack files
└── pack_syn_{YYYYMMDD_HHMMSS}_{hex6}.json # synthetic pack files
```

**Manifest structure:**
```json
{
  "schema_version": 1,
  "total": 5,
  "updated_at": "2026-04-16T14:23:11Z",
  "packs": [
    {
      "pack_id": "pack_20260416_142311_a1b2c3",
      "pack_type": "vix_spike",
      "name": "crisis_regime_vix_spike",
      "symbol": null,
      "is_synthetic": true,
      "source": "synthetic",
      "created_at": "...",
      "replay_count": 0,
      "tags": ["crisis", "vix_above_35", "seed"]
    }
  ]
}
```

---

## 4. Public API

```python
from replay_packs import (
    save_pack,                 # ReplayPack → Path
    load_pack,                 # pack_id → ReplayPack | None
    list_packs,                # pack_type? → list[dict]
    build_pack_from_decision,  # decision_id → ReplayPack | None
    replay_pack,               # pack_id, fork_config? → ReplayResult | None
    build_synthetic_pack,      # name, description, inputs_snapshot → ReplayPack
    update_manifest,           # rebuild manifest from pack files → Path
)
```

`replay_pack()` requires `enable_replay_fork_debugger=True` in shadow_flags.
Returns `None` if the flag is off or if the pack has no `decision_id`.

---

## 5. Adding New Packs

**From a live decision:**
```python
pack = build_pack_from_decision(decision_id="dec_20260416_...", pack_type="trade_win")
if pack:
    save_pack(pack)
```

**As a synthetic scenario:**
```python
from replay_packs import build_synthetic_pack, save_pack
pack = build_synthetic_pack(
    name="my_scenario",
    description="...",
    inputs_snapshot={"regime_score": 45, "vix": 22.1, ...},
    pack_type="synthetic",
    tags=["test"],
)
save_pack(pack)
```

**Seed initial packs:**
```bash
python3 scripts/build_initial_replay_packs.py
python3 scripts/build_initial_replay_packs.py --from-decisions
```
