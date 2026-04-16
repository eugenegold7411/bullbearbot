#!/usr/bin/env python3
"""Adds _emit_spine_record to attribution.py and wires it into log_attribution_event."""

from pathlib import Path

path = Path("/home/trading-bot/attribution.py")
content = path.read_text()

adapter = '''

def _emit_spine_record(event: dict, extra: dict) -> None:
    """Best-effort spine adapter. Non-fatal. Called from log_attribution_event()."""
    try:
        from cost_attribution import log_spine_record  # lazy import, avoids circular risk
        module_tags = event.get("module_tags") or {}
        log_spine_record(
            module_name=module_tags.get("module") or event.get("caller") or "unknown",
            layer_name=module_tags.get("layer") or "execution_control",
            ring=module_tags.get("ring") or "prod",
            model=extra.get("model") or "unknown",
            purpose=event.get("event_type") or "unknown",
            linked_subject_id=event.get("decision_id") or None,
            linked_subject_type="decision" if event.get("decision_id") else None,
            input_tokens=extra.get("input_tokens"),
            output_tokens=extra.get("output_tokens"),
            cached_tokens=extra.get("cached_tokens") or extra.get("cache_read_tokens"),
            estimated_cost_usd=extra.get("estimated_cost_usd"),
        )
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning("[T0.7] spine adapter failed: %s", e)
'''

# Step 1: add the function if missing
if "_emit_spine_record" not in content:
    content = content + adapter
    print("adapter function added")
else:
    print("adapter function already present")

# Step 2: wire the call inside log_attribution_event if missing
if "_emit_spine_record(event" not in content:
    # Place call just before the outer except block in log_attribution_event
    old = "    except Exception as e:\n        log.warning"
    new = "    _emit_spine_record(event, extra or {})\n    except Exception as e:\n        log.warning"
    if old in content:
        content = content.replace(old, new, 1)
        print("call wired into log_attribution_event")
    else:
        print("WARNING: could not find insertion point — wire manually")
else:
    print("call already wired")

path.write_text(content)
print("done")
