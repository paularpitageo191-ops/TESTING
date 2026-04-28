#!/usr/bin/env python3
"""
Pipeline validation gate — run between dom_capture and quality_alignment.
Fails fast if _ui_memory is missing required element types.

Usage:
  python3 validate_pipeline.py --project SCRUM-70 \
    --require-selectors "#userEmail,#submit" \
    --require-pages "https://demoqa.com/text-box"
"""
import os
import sys
import argparse
import re
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue
from dotenv import load_dotenv

load_dotenv()

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")

def sanitize(name: str) -> str:
    return re.sub(r'[^a-zA-Z0-9_]', '_', name).strip('_') or 'collection'

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--require-selectors", default="",
                        help="Comma-separated selectors that must exist, e.g. '#userEmail,#submit'")
    parser.add_argument("--require-pages", default="",
                        help="Comma-separated page URLs that must have elements")
    args = parser.parse_args()

    collection = sanitize(f"{args.project}_ui_memory")
    client = QdrantClient(url=QDRANT_URL)

    # Check collection exists
    existing = {c.name for c in client.get_collections().collections}
    if collection not in existing:
        print(f"✗ Collection '{collection}' does not exist — run dom_capture --vectorize first")
        sys.exit(1)

    # Scroll all points
    results, _ = client.scroll(
        collection_name=collection,
        scroll_filter=Filter(must=[
            FieldCondition(key="project_key", match=MatchValue(value=args.project))
        ]),
        limit=500,
        with_payload=True,
    )

    all_selectors = {r.payload.get("selector", "") for r in results}
    all_pages     = {r.payload.get("page_url", "") for r in results}
    
    print(f"\n✓ Collection '{collection}' found — {len(results)} points")
    print(f"  Pages covered : {sorted(p for p in all_pages if p)}")
    print(f"  Element types : input={sum(1 for r in results if r.payload.get('element_type')=='input')} "
          f"button={sum(1 for r in results if r.payload.get('element_type')=='button')}")

    failed = False

    # Check required selectors
    if args.require_selectors:
        for sel in args.require_selectors.split(","):
            sel = sel.strip()
            if not sel:
                continue
            # Check both exact and partial match
            found = any(sel in s for s in all_selectors)
            if found:
                print(f"  ✓ selector '{sel}' present")
            else:
                print(f"  ✗ selector '{sel}' MISSING — DOM capture may not have reached this page")
                failed = True

    # Check required pages
    if args.require_pages:
        for page_url in args.require_pages.split(","):
            page_url = page_url.strip()
            if not page_url:
                continue
            found = any(page_url in p for p in all_pages)
            if found:
                print(f"  ✓ page '{page_url}' covered")
            else:
                print(f"  ✗ page '{page_url}' NOT captured — add it to --urls in dom_capture")
                failed = True

    if failed:
        print("\n✗ Validation FAILED — fix dom_capture before running quality_alignment")
        sys.exit(1)
    else:
        print("\n✓ Validation PASSED — safe to run quality_alignment")

if __name__ == "__main__":
    main()