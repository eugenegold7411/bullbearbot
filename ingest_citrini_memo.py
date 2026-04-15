#!/usr/bin/env python3
"""
ingest_citrini_memo.py — Extract structured macro positions from a Citrini Research PDF memo.

Usage:
    python ingest_citrini_memo.py path/to/memo.pdf [--dry-run]

Writes to:  data/macro_intelligence/citrini_positions.json
Never runs automatically. Manual only.

Requires: pypdf or pdfplumber (tries both), anthropic SDK.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_BASE_DIR  = Path(__file__).parent
_OUT_FILE  = _BASE_DIR / "data" / "macro_intelligence" / "citrini_positions.json"
_MODEL     = "claude-sonnet-4-6"
_MAX_CHARS = 80_000   # ~20K tokens — enough for a dense research memo

_EXTRACT_PROMPT = """Extract all active macro trade positions from this Citrini Research memo.

Return ONLY valid JSON in this exact format:
{
  "memo_date": "YYYY-MM-DD or 'Month YYYY' string from the document",
  "memo_title": "exact title from the document",
  "active_trades": [
    {
      "symbol": "TICKER or instrument name (e.g. 'HGZ6', 'TLT', 'DXY')",
      "direction": "long" | "short" | "neutral",
      "thesis_summary": "1-2 sentence summary of the core thesis",
      "entry_notes": "entry level, catalyst, or timing notes if mentioned",
      "active": true
    }
  ],
  "watchlist_themes": [
    {
      "theme": "theme name (e.g. 'China reopening', 'AI infrastructure')",
      "symbols": ["TICKER1", "TICKER2"],
      "rationale": "1 sentence on why this theme is on the radar"
    }
  ],
  "macro_view": {
    "us_growth": "expanding | slowing | recessionary | uncertain",
    "rates_view": "1 sentence on rate trajectory thesis",
    "dollar_view": "bullish | bearish | neutral with 1 sentence context",
    "key_risks": ["risk1", "risk2", "risk3"]
  }
}

If a section is not present in the memo, use an empty array [] or "unknown" string.
Extract ALL explicitly mentioned trade positions, not just the author's favorites.
"""


def _extract_pdf_text(pdf_path: Path) -> str:
    """Try pdfplumber first (better tables), fall back to pypdf."""
    # Try pdfplumber
    try:
        import pdfplumber  # noqa: PLC0415
        with pdfplumber.open(str(pdf_path)) as pdf:
            pages = []
            for page in pdf.pages:
                text = page.extract_text()
                if text:
                    pages.append(text)
        text = "\n\n".join(pages)
        if text.strip():
            print(f"  [PDF] Extracted {len(text):,} chars via pdfplumber ({len(pages)} pages)")
            return text
    except ImportError:
        pass
    except Exception as exc:
        print(f"  [PDF] pdfplumber failed: {exc} — trying pypdf")

    # Fall back to pypdf
    try:
        from pypdf import PdfReader  # noqa: PLC0415
        reader = PdfReader(str(pdf_path))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(p for p in pages if p.strip())
        if text.strip():
            print(f"  [PDF] Extracted {len(text):,} chars via pypdf ({len(reader.pages)} pages)")
            return text
    except ImportError:
        pass
    except Exception as exc:
        print(f"  [PDF] pypdf failed: {exc}")

    raise RuntimeError(
        "No PDF library available. Install one:\n"
        "  pip install pdfplumber   (recommended)\n"
        "  pip install pypdf"
    )


def _call_claude(text: str) -> dict:
    """Call Claude Sonnet to extract structured positions from memo text."""
    import anthropic  # noqa: PLC0415

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    truncated = text[:_MAX_CHARS]
    if len(text) > _MAX_CHARS:
        print(f"  [CLAUDE] Text truncated {len(text):,} → {_MAX_CHARS:,} chars")

    print(f"  [CLAUDE] Calling {_MODEL} to extract positions...")
    resp = client.messages.create(
        model=_MODEL,
        max_tokens=4000,
        system="You are a financial analyst extracting structured data from research memos. "
               "Return only valid JSON. No markdown, no prose.",
        messages=[{
            "role": "user",
            "content": f"{_EXTRACT_PROMPT}\n\nMEMO TEXT:\n\n{truncated}",
        }],
    )
    raw = resp.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    result = json.loads(raw)
    # Record token usage
    usage = resp.usage
    print(f"  [CLAUDE] Tokens: input={usage.input_tokens}  output={usage.output_tokens}")
    return result


def _print_summary(data: dict) -> None:
    """Print a human-readable summary of what was extracted."""
    print("\n" + "=" * 60)
    print(f"  Memo: {data.get('memo_title', '(untitled)')}")
    print(f"  Date: {data.get('memo_date', '(unknown)')}")

    trades = [t for t in data.get("active_trades", []) if t.get("active")]
    print(f"\n  Active trades ({len(trades)}):")
    for t in trades:
        sym  = t.get("symbol", "?")
        dir_ = t.get("direction", "?").upper()
        ths  = t.get("thesis_summary", "")[:70]
        print(f"    {sym:<12} [{dir_:<7}] {ths}")

    themes = data.get("watchlist_themes", [])
    print(f"\n  Watchlist themes ({len(themes)}):")
    for th in themes:
        name = th.get("theme", "?")
        syms = ", ".join(th.get("symbols", [])[:4])
        print(f"    {name}: {syms}")

    mv = data.get("macro_view", {})
    if mv:
        print(f"\n  Macro view:")
        print(f"    Growth: {mv.get('us_growth','?')}")
        print(f"    Rates:  {mv.get('rates_view','?')[:70]}")
        print(f"    Dollar: {mv.get('dollar_view','?')[:70]}")
        risks = mv.get("key_risks", [])
        if risks:
            print(f"    Risks:  {', '.join(risks[:3])}")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest a Citrini Research PDF memo into citrini_positions.json"
    )
    parser.add_argument("pdf", help="Path to the Citrini memo PDF")
    parser.add_argument("--dry-run", action="store_true",
                        help="Extract and print but do not write to disk")
    args = parser.parse_args()

    pdf_path = Path(args.pdf).expanduser()
    if not pdf_path.exists():
        print(f"ERROR: File not found: {pdf_path}")
        sys.exit(1)
    if pdf_path.suffix.lower() != ".pdf":
        print(f"WARNING: File does not have .pdf extension: {pdf_path}")

    print(f"\nIngesting: {pdf_path.name}")

    # Extract text
    try:
        text = _extract_pdf_text(pdf_path)
    except Exception as exc:
        print(f"ERROR extracting PDF: {exc}")
        sys.exit(1)

    if len(text.strip()) < 100:
        print("ERROR: Extracted text is too short — is this a scanned/image PDF?")
        sys.exit(1)

    # Claude extraction
    try:
        result = _call_claude(text)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Claude returned non-JSON: {exc}")
        sys.exit(1)
    except Exception as exc:
        print(f"ERROR calling Claude: {exc}")
        sys.exit(1)

    # Print summary
    _print_summary(result)

    if args.dry_run:
        print("\n[DRY RUN] Not writing to disk.")
        return

    # Check for existing file
    _OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _OUT_FILE.exists():
        existing = json.loads(_OUT_FILE.read_text())
        existing_date = existing.get("memo_date", "")
        new_date      = result.get("memo_date", "")
        print(f"\nExisting file: memo_date={existing_date!r}")
        print(f"New data:      memo_date={new_date!r}")
        confirm = input("Overwrite? [y/N] ").strip().lower()
        if confirm != "y":
            print("Aborted. Existing file unchanged.")
            return

    _OUT_FILE.write_text(json.dumps(result, indent=2))
    print(f"\nWritten to: {_OUT_FILE}")
    print("Done. Run the bot — macro backdrop will include Citrini positions.")


if __name__ == "__main__":
    main()
