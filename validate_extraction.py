"""Throwaway script — run 5-10 real family receipts through the extraction prompt.

Usage:
    1. Put your receipt photos into ./sample_receipts/ (jpg/png/jpeg/heic/webp).
    2. Make sure GEMINI_API_KEY is in .env or exported in the shell.
    3. python -u validate_extraction.py

This is build-order step 1 from architecture.md. It exists to answer one
question before any other code is written: "can Gemini actually read the
real-world receipts this family will send it?"
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".webp", ".bmp", ".tiff"}


def main() -> int:
    root = Path(__file__).parent / "sample_receipts"
    if not root.is_dir():
        print(f"[FAIL] sample_receipts/ directory not found at: {root}", file=sys.stderr)
        return 1

    files = sorted(p for p in root.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    if not files:
        print(f"[WARN] No receipt images found in {root}. Drop .jpg/.png receipts there and re-run.", file=sys.stderr)
        return 2

    if not os.environ.get("GEMINI_API_KEY"):
        print("[FAIL] GEMINI_API_KEY not set. Add it to .env or `export GEMINI_API_KEY=...`", file=sys.stderr)
        return 3

    # Lazy import so the script fails fast on the key check before importing SDK.
    from app.services.extraction import extract_receipt

    print(f"Found {len(files)} receipt images to validate.\n")
    results: list[dict] = []
    for path in files:
        print("=" * 60)
        print(f"Image: {path.name}  ({path.stat().st_size:,} bytes)")
        try:
            image_bytes = path.read_bytes()
            result = extract_receipt(image_bytes, mime_hint=None)
            data = json.loads(result.model_dump_json())
            results.append({"file": path.name, "result": data})
            print(json.dumps(data, indent=2, default=str))
        except Exception as e:
            print(f"[ERROR] {e}")
            results.append({"file": path.name, "error": repr(e)})

    print("=" * 60)
    print(f"\nDone. {len(results)} images processed.")
    # Summary table
    print("\nSUMMARY:")
    print(f"{'File':<30} {'Overall conf':>13} {'# items':>8} {'Vendor':<20}")
    for r in results:
        file = r["file"]
        res = r.get("result") or {}
        conf = res.get("overall_confidence", "err") if not r.get("error") else "ERR"
        n_items = len(res.get("line_items") or []) if res else 0
        vendor = (res.get("vendor") or "-") if res else "-"
        print(f"{file:<30} {str(conf):>13} {n_items:>8} {str(vendor)[:20]:<20}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
