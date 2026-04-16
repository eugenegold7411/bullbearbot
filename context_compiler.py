"""
context_compiler.py — Shadow-mode prompt section compressor (T1.4).

# SHADOW MODULE — do not import from prod pipeline

Compresses raw prompt sections into shorter provenance-preserving digests.
NO prod prompt mutation. Runs in shadow mode only — outputs logged but never
injected into live prompts.

Feature flag: enable_context_compressor_shadow (in shadow_flags) gates API calls.
If False, compress_section() returns None without calling the API.

Cost attribution: every API call logs to spine with layer_name="context_compiler",
ring="shadow".
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_SHADOW_LOG = Path("data/analytics/context_compiler_shadow.jsonl")

_COMPRESSION_SYSTEM_PROMPT = (
    "You are a context compressor for a trading bot. "
    "Your job is to compress the provided section into a shorter digest "
    "that preserves all key facts, numbers, signals, and named entities. "
    "Output ONLY the compressed text — no preamble, no meta-commentary. "
    "Be ruthlessly concise while preserving all actionable information."
)


@dataclass
class CompressedSection:
    schema_version: int = 1
    section_name: str = ""
    cycle_id: str = ""
    compressed_at: str = ""
    raw_length_chars: int = 0
    compressed_length_chars: int = 0
    compression_ratio: float = 0.0
    raw_content: str = ""
    compressed_content: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


def compress_section(
    section_name: str,
    raw_content: str,
    model: str = "claude-haiku-4-5-20251001",
    max_compressed_tokens: int = 200,
) -> Optional[CompressedSection]:
    """
    Call Claude Haiku to compress raw_content into a shorter digest.
    Returns None on any failure or when flag is disabled.
    Logs spine record for every API call made.
    """
    try:
        from feature_flags import is_enabled  # noqa: PLC0415
        if not is_enabled("enable_context_compressor_shadow"):
            return None

        if not raw_content or not raw_content.strip():
            return None

        import anthropic  # noqa: PLC0415
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

        response = client.messages.create(
            model=model,
            max_tokens=max_compressed_tokens,
            system=_COMPRESSION_SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Compress the following '{section_name}' section "
                        f"to under {max_compressed_tokens} tokens:\n\n"
                        f"{raw_content}"
                    ),
                }
            ],
        )

        compressed = response.content[0].text if response.content else ""
        input_tok = response.usage.input_tokens
        output_tok = response.usage.output_tokens

        # Haiku pricing: $1.00/$5.00 per million input/output
        cost = (input_tok * 1.0 + output_tok * 5.0) / 1_000_000

        section = CompressedSection(
            schema_version=1,
            section_name=section_name,
            cycle_id="",
            compressed_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            raw_length_chars=len(raw_content),
            compressed_length_chars=len(compressed),
            compression_ratio=(
                round(len(compressed) / len(raw_content), 4) if raw_content else 0.0
            ),
            raw_content=raw_content,
            compressed_content=compressed,
            model=model,
            input_tokens=input_tok,
            output_tokens=output_tok,
            estimated_cost_usd=round(cost, 6),
        )

        # Spine attribution
        try:
            import cost_attribution as _ca  # noqa: PLC0415
            _ca.log_spine_record(
                module_name="context_compiler",
                layer_name="context_compiler",
                ring="shadow",
                model=model,
                purpose="section_compression",
                input_tokens=input_tok,
                output_tokens=output_tok,
                estimated_cost_usd=round(cost, 6),
            )
        except Exception:
            pass

        return section

    except Exception as exc:  # noqa: BLE001
        log.warning("[COMPILER] compress_section failed: %s", exc)
        return None


def compress_and_log(
    section_name: str,
    raw_content: str,
    cycle_id: str,
) -> None:
    """
    Call compress_section() and log result to shadow log.
    Never raises. Never mutates prod prompt.
    """
    try:
        result = compress_section(section_name=section_name, raw_content=raw_content)
        if result is None:
            return
        result.cycle_id = cycle_id
        _SHADOW_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_SHADOW_LOG, "a") as fh:
            fh.write(json.dumps(asdict(result)) + "\n")
        log.info(
            "[COMPILER] %s compressed %.0f→%.0f chars (ratio=%.2f)",
            section_name,
            result.raw_length_chars,
            result.compressed_length_chars,
            result.compression_ratio,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[COMPILER] compress_and_log failed: %s", exc)
