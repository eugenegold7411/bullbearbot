"""
replay_fork.py — Minimal replay harness for decision captures.

Loads a capture from data/captures/{decision_id}.json, applies a fork axis,
re-calls Claude with the captured prompts, and writes a replay artifact to
data/reports/replays/.

Fork axes:
    model            — swap in a different Claude model (e.g. claude-haiku-4-5-20251001)
    prompt_version   — not yet implemented (raises NotImplementedError)
    taxonomy_version — not yet implemented (raises NotImplementedError)

Output: data/reports/replays/{decision_id}_{fork_axis}_{fork_value}_{ts}.json

Strictly read-only with respect to production state. Never writes to
data/captures/, strategy_config.json, or any production log.

CLI:
    python replay_fork.py --decision-id dec_A1_20260417_093000 \\
                          --fork-axis model \\
                          --fork-value claude-haiku-4-5-20251001
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_CAPTURES_DIR  = Path(__file__).parent / "data" / "captures"
_REPLAYS_DIR   = Path(__file__).parent / "data" / "reports" / "replays"
_VALID_AXES    = frozenset({"model", "prompt_version", "taxonomy_version"})


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def load_capture(decision_id: str) -> dict:
    """
    Load a decision capture by decision_id.
    Raises FileNotFoundError if the capture does not exist.
    Raises ValueError if the file is not valid JSON.
    """
    path = _CAPTURES_DIR / f"{decision_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"No capture found for decision_id={decision_id!r}. "
            f"Expected at {path}. "
            "Captures are written by bot.py for every Sonnet cycle."
        )
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"Capture file is not valid JSON: {path}") from exc


def run_replay(
    decision_id: str,
    fork_axis: str,
    fork_value: str,
) -> dict:
    """
    Load capture, apply fork, call Claude, write replay artifact.

    Returns the replay artifact dict on success.
    Raises FileNotFoundError / ValueError / NotImplementedError as appropriate.
    """
    if fork_axis not in _VALID_AXES:
        raise ValueError(
            f"Unknown fork_axis={fork_axis!r}. Valid axes: {sorted(_VALID_AXES)}"
        )

    if fork_axis == "prompt_version":
        raise NotImplementedError(
            "prompt_version fork is not yet implemented. "
            "To fork on a prompt, manually create a capture with the desired prompt text "
            "and run with fork_axis=model."
        )

    if fork_axis == "taxonomy_version":
        raise NotImplementedError(
            "taxonomy_version fork is not yet implemented. "
            "Taxonomy versioning requires a migration pipeline not yet built."
        )

    # fork_axis == "model"
    capture = load_capture(decision_id)

    system_prompt  = capture.get("system_prompt", "")
    user_prompt    = capture.get("user_prompt", "")
    original_model = capture.get("model", "")

    if not system_prompt or not user_prompt:
        raise ValueError(
            f"Capture {decision_id!r} is missing system_prompt or user_prompt. "
            "This capture may have been written by an older version of bot.py."
        )

    replay_model = fork_value

    # Call Claude with captured prompts
    from dotenv import load_dotenv
    load_dotenv()
    import anthropic

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    ts_start = datetime.now(timezone.utc)
    response = client.messages.create(
        model=replay_model,
        max_tokens=2048,
        system=[{"type": "text", "text": system_prompt}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    usage       = response.usage
    raw_text    = response.content[0].text.strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("\n", 1)[-1]
        raw_text = raw_text.rsplit("```", 1)[0].strip()

    try:
        replay_decision = json.loads(raw_text)
        parse_error     = None
    except json.JSONDecodeError as exc:
        replay_decision = None
        parse_error     = str(exc)

    ts_end = datetime.now(timezone.utc)
    ts_str = ts_end.strftime("%Y%m%dT%H%M%SZ")

    artifact = {
        "schema_version":    1,
        "replay_id":         f"replay_{decision_id}_{fork_axis}_{fork_value}_{ts_str}",
        "decision_id":       decision_id,
        "fork_axis":         fork_axis,
        "fork_value":        fork_value,
        "original_model":    original_model,
        "replay_model":      replay_model,
        "replayed_at":       ts_end.isoformat().replace("+00:00", "Z"),
        "duration_seconds":  round((ts_end - ts_start).total_seconds(), 2),
        "usage": {
            "input_tokens":  getattr(usage, "input_tokens",  None),
            "output_tokens": getattr(usage, "output_tokens", None),
        },
        "original_decision": json.loads(capture.get("raw_response", "null")),
        "replay_decision":   replay_decision,
        "replay_raw":        raw_text,
        "parse_error":       parse_error,
    }

    _write_replay_artifact(artifact)
    return artifact


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _write_replay_artifact(artifact: dict) -> None:
    """Atomic write to data/reports/replays/. Non-fatal."""
    try:
        _REPLAYS_DIR.mkdir(parents=True, exist_ok=True)
        rid  = artifact["replay_id"]
        path = _REPLAYS_DIR / f"{rid}.json"
        tmp  = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(artifact, indent=2))
        os.replace(tmp, path)
        log.info("[REPLAY] wrote artifact → %s", path)
    except Exception as exc:
        log.warning("[REPLAY] failed to write artifact: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Replay a bot decision with a fork axis applied.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python replay_fork.py --decision-id dec_A1_20260417_093000 \\
                        --fork-axis model \\
                        --fork-value claude-haiku-4-5-20251001

  python replay_fork.py --decision-id dec_A1_20260417_093000 \\
                        --fork-axis model \\
                        --fork-value claude-opus-4-7
        """,
    )
    p.add_argument("--decision-id", required=True, help="Decision ID to replay")
    p.add_argument(
        "--fork-axis",
        required=True,
        choices=sorted(_VALID_AXES),
        help="Axis to fork on",
    )
    p.add_argument("--fork-value", required=True, help="Value to apply for the fork")
    return p


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _build_parser().parse_args()

    try:
        artifact = run_replay(
            decision_id=args.decision_id,
            fork_axis=args.fork_axis,
            fork_value=args.fork_value,
        )
    except (FileNotFoundError, ValueError, NotImplementedError) as exc:
        print(f"ERROR: {exc}")
        raise SystemExit(1)

    print(f"\nReplay complete: {artifact['replay_id']}")
    print(f"Original model : {artifact['original_model']}")
    print(f"Replay model   : {artifact['replay_model']}")
    print(f"Duration       : {artifact['duration_seconds']}s")
    print(f"Parse error    : {artifact['parse_error'] or 'none'}")

    orig = artifact.get("original_decision") or {}
    replay = artifact.get("replay_decision") or {}
    orig_regime  = orig.get("regime_view",  orig.get("regime",  "?"))
    replay_regime = replay.get("regime_view", replay.get("regime", "?"))
    orig_ideas  = len(orig.get("ideas", orig.get("actions", [])))
    replay_ideas = len(replay.get("ideas", replay.get("actions", [])))
    print(f"\nOriginal  regime={orig_regime}  ideas={orig_ideas}")
    print(f"Replay    regime={replay_regime}  ideas={replay_ideas}")

    output_path = _REPLAYS_DIR / f"{artifact['replay_id']}.json"
    print(f"\nArtifact written to: {output_path}")


if __name__ == "__main__":
    main()
