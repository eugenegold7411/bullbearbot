#!/usr/bin/env python3
"""
Opus 4.7 vs Sonnet 4.6 — Blinded Evaluation Runner

Pre-registered protocol. Do not modify after first call fires.

Usage:
    python3 run_eval.py --manifest manifest.json --dry-run   # validate only
    python3 run_eval.py --manifest manifest.json             # actual run

Before running:
    1. PRE_REGISTRATION.md must exist and be committed (SHA-256 captured)
    2. All prompts/ files present per manifest
    3. All ground_truth/ files present per manifest (except A2 cases)
    4. ANTHROPIC_API_KEY in environment

After running:
    - outputs/ populated with blinded A/B files
    - call_log.jsonl populated with token/cost/latency metadata
    - model_mapping.json written and immediately moved to SEALED/ directory
    - Score from scoring_sheet_template.csv without opening SEALED/
"""

import argparse
import hashlib
import json
import os
import random
import shutil
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

try:
    from anthropic import Anthropic
except ImportError:
    print("ERROR: anthropic package not installed. `pip install anthropic`", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Model identifiers — locked at protocol creation time
# ---------------------------------------------------------------------------
MODEL_SONNET = "claude-sonnet-4-6"
MODEL_OPUS = "claude-opus-4-7"

# Exact model strings must be confirmed against the API before the run.
# If either string fails the first test call, halt and do not proceed with
# substitutes — the pre-registration locks these specific models.


# ---------------------------------------------------------------------------
# Pricing (as of run date — confirm against current Anthropic pricing page)
# ---------------------------------------------------------------------------
# Per million tokens
PRICING = {
    MODEL_SONNET: {
        "input": 3.00,
        "output": 15.00,
        "cache_write": 3.75,
        "cache_read": 0.30,
    },
    MODEL_OPUS: {
        # CONFIRM THESE AGAINST CURRENT ANTHROPIC PRICING PAGE BEFORE RUN
        "input": 15.00,   # placeholder — verify
        "output": 75.00,  # placeholder — verify
        "cache_write": 18.75,  # placeholder — verify
        "cache_read": 1.50,    # placeholder — verify
    },
}


@dataclass
class CallResult:
    artifact_id: str
    model: str  # real model identifier, logged only to call_log (not visible in output files)
    blinded_label: str  # "A" or "B"
    run_timestamp: str
    cache_hit_input_tokens: int
    cache_write_input_tokens: int
    uncached_input_tokens: int
    output_tokens: int
    wall_clock_ms: int
    estimated_cost_usd: float
    output_text: str
    error: Optional[str] = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def compute_cost(model: str, usage) -> float:
    """Compute USD cost from usage block. Handles cache fields if present."""
    p = PRICING[model]
    cache_hit = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    uncached_input = getattr(usage, "input_tokens", 0) or 0
    output = getattr(usage, "output_tokens", 0) or 0

    cost = (
        cache_hit * p["cache_read"] / 1_000_000
        + cache_write * p["cache_write"] / 1_000_000
        + uncached_input * p["input"] / 1_000_000
        + output * p["output"] / 1_000_000
    )
    return round(cost, 6)


def load_manifest(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def validate_manifest(manifest: dict, base_dir: Path) -> list[str]:
    """Check that every artifact's prompt file exists. Return list of missing artifacts."""
    missing = []
    for artifact in manifest["artifacts"]:
        prompt_path = base_dir / "prompts" / f"{artifact['id']}.txt"
        if not prompt_path.exists():
            missing.append(artifact["id"])
            continue

        # Verify SHA-256 matches manifest
        expected = artifact.get("prompt_sha256")
        if expected:
            actual = sha256_file(prompt_path)
            if actual != expected:
                missing.append(f"{artifact['id']} (SHA mismatch)")
    return missing


def call_model(
    client: Anthropic,
    model: str,
    prompt: str,
    system: Optional[str],
    temperature: float,
    max_tokens: int,
) -> tuple[str, object, int]:
    """Make the API call. Returns (output_text, usage, wall_clock_ms)."""
    start = time.monotonic()
    kwargs = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system

    response = client.messages.create(**kwargs)
    wall_ms = int((time.monotonic() - start) * 1000)

    # Extract text from first content block
    output_text = ""
    for block in response.content:
        if hasattr(block, "text"):
            output_text += block.text

    return output_text, response.usage, wall_ms


def run_evaluation(manifest_path: Path, dry_run: bool) -> None:
    base_dir = manifest_path.parent
    manifest = load_manifest(manifest_path)

    # Verify PRE_REGISTRATION.md hash matches manifest record
    prereg_path = base_dir / "PRE_REGISTRATION.md"
    if not prereg_path.exists():
        print("FATAL: PRE_REGISTRATION.md not found. Run is invalid.", file=sys.stderr)
        sys.exit(2)

    actual_prereg_hash = sha256_file(prereg_path)
    expected_prereg_hash = manifest.get("pre_registration_sha256")
    if expected_prereg_hash and actual_prereg_hash != expected_prereg_hash:
        print(
            f"FATAL: PRE_REGISTRATION.md has been modified since manifest was created.\n"
            f"  Expected: {expected_prereg_hash}\n"
            f"  Actual:   {actual_prereg_hash}\n"
            f"Run is invalid. Regenerate manifest and restart.",
            file=sys.stderr,
        )
        sys.exit(2)

    # Validate all prompts present
    missing = validate_manifest(manifest, base_dir)
    if missing:
        print(f"FATAL: missing or corrupted prompt files: {missing}", file=sys.stderr)
        sys.exit(2)

    print(f"[OK] Pre-registration hash verified: {actual_prereg_hash[:16]}...")
    print(f"[OK] {len(manifest['artifacts'])} artifacts validated")

    if dry_run:
        print("[DRY RUN] Exiting without API calls.")
        return

    # Prepare output directories
    outputs_dir = base_dir / "outputs"
    sealed_dir = base_dir / "SEALED"
    outputs_dir.mkdir(exist_ok=True)
    sealed_dir.mkdir(exist_ok=True)

    if (base_dir / "model_mapping.json").exists():
        print(
            "FATAL: model_mapping.json already exists. Previous run in progress or complete.\n"
            "Manually remove or move it to confirm you want to start fresh.",
            file=sys.stderr,
        )
        sys.exit(2)

    client = Anthropic()
    call_log_path = base_dir / "call_log.jsonl"
    model_mapping = {}

    # Randomize A/B assignment per artifact
    rng = random.Random(manifest.get("random_seed", 42))

    for artifact in manifest["artifacts"]:
        aid = artifact["id"]
        print(f"\n--- {aid} ---")

        prompt = (base_dir / "prompts" / f"{aid}.txt").read_text()
        system_path = base_dir / "prompts" / f"{aid}_system.txt"
        system = system_path.read_text() if system_path.exists() else None

        temperature = artifact.get("temperature", 0.0)
        max_tokens = artifact.get("max_tokens", 4000)

        # Randomize which model gets label A vs B
        if rng.random() < 0.5:
            label_map = {"A": MODEL_SONNET, "B": MODEL_OPUS}
        else:
            label_map = {"A": MODEL_OPUS, "B": MODEL_SONNET}

        model_mapping[aid] = label_map

        for label in ["A", "B"]:
            model = label_map[label]
            print(f"  [{label}] calling {model}...", end=" ", flush=True)

            try:
                output_text, usage, wall_ms = call_model(
                    client, model, prompt, system, temperature, max_tokens
                )
                cost = compute_cost(model, usage)
                result = CallResult(
                    artifact_id=aid,
                    model=model,
                    blinded_label=label,
                    run_timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    cache_hit_input_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                    cache_write_input_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
                    uncached_input_tokens=getattr(usage, "input_tokens", 0) or 0,
                    output_tokens=getattr(usage, "output_tokens", 0) or 0,
                    wall_clock_ms=wall_ms,
                    estimated_cost_usd=cost,
                    output_text=output_text,
                )
                print(f"OK {wall_ms}ms ${cost:.4f}")

            except Exception as e:
                result = CallResult(
                    artifact_id=aid,
                    model=model,
                    blinded_label=label,
                    run_timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    cache_hit_input_tokens=0,
                    cache_write_input_tokens=0,
                    uncached_input_tokens=0,
                    output_tokens=0,
                    wall_clock_ms=0,
                    estimated_cost_usd=0.0,
                    output_text="",
                    error=str(e),
                )
                print(f"ERROR: {e}")

            # Write blinded output (model identity NOT in filename)
            out_path = outputs_dir / f"{aid}__model_{label}.txt"
            out_path.write_text(result.output_text or f"[ERROR: {result.error}]")

            # Append to call log (model identity IS logged here, but this file
            # must not be opened during scoring)
            log_entry = asdict(result)
            log_entry.pop("output_text")  # don't duplicate into log
            with open(call_log_path, "a") as f:
                f.write(json.dumps(log_entry) + "\n")

    # Seal the model mapping
    mapping_path = sealed_dir / "model_mapping.json"
    with open(mapping_path, "w") as f:
        json.dump(model_mapping, f, indent=2)

    print(f"\n[DONE] Outputs written to {outputs_dir}")
    print(f"[DONE] Call log: {call_log_path}")
    print(f"[SEALED] Model mapping: {mapping_path}")
    print(f"\nNext step: score outputs blind using scoring_sheet_template.csv")
    print("Do NOT open SEALED/model_mapping.json or call_log.jsonl until scoring is complete.")


def build_manifest(base_dir: Path) -> None:
    """Helper: scan prompts/ directory and build manifest.json with SHA hashes."""
    artifacts = []
    prompts_dir = base_dir / "prompts"

    for prompt_file in sorted(prompts_dir.glob("*.txt")):
        if prompt_file.stem.endswith("_system"):
            continue
        aid = prompt_file.stem
        artifacts.append(
            {
                "id": aid,
                "prompt_sha256": sha256_file(prompt_file),
                "temperature": 0.0,
                "max_tokens": 4000,
            }
        )

    prereg_path = base_dir / "PRE_REGISTRATION.md"
    manifest = {
        "pre_registration_sha256": sha256_file(prereg_path) if prereg_path.exists() else None,
        "random_seed": 42,
        "artifacts": artifacts,
    }

    out = base_dir / "manifest.json"
    with open(out, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f"[OK] Manifest written: {out}")
    print(f"     {len(artifacts)} artifacts indexed")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=Path("manifest.json"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--build-manifest", action="store_true",
                        help="Scan prompts/ and build manifest.json")
    args = parser.parse_args()

    if args.build_manifest:
        build_manifest(args.manifest.parent if args.manifest.parent != Path() else Path("."))
        return

    if not args.manifest.exists():
        print(f"FATAL: manifest not found: {args.manifest}", file=sys.stderr)
        print("Run with --build-manifest first to generate it.", file=sys.stderr)
        sys.exit(2)

    run_evaluation(args.manifest, args.dry_run)


if __name__ == "__main__":
    main()
