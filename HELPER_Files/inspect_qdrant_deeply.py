import argparse
from collections import Counter
from qdrant_client import QdrantClient


def main():
    parser = argparse.ArgumentParser(description="Inspect Qdrant collection payloads")
    parser.add_argument("--collection", required=True, help="Collection name")
    parser.add_argument("--limit", type=int, default=5, help="Number of points to fetch")
    parser.add_argument("--url", default="http://localhost:6333", help="Qdrant URL")

    args = parser.parse_args()

    client = QdrantClient(url=args.url)

    print(f"\n🔍 Inspecting collection: {args.collection}")

    # 🔥 Count total points
    try:
        count = client.count(collection_name=args.collection).count
        print(f"✓ Total points in collection: {count}")
    except Exception as e:
        print(f"⚠ Could not count points: {e}")

    print(f"\nFetching {args.limit} sample records...\n")

    try:
        records, _ = client.scroll(
            collection_name=args.collection,
            limit=args.limit,
            with_payload=True
        )

        if not records:
            print("⚠ No records returned from scroll()")
            return

        project_keys = []
        missing_text = 0

        for i, r in enumerate(records, 1):
            p = r.payload or {}

            pk = p.get("project_key")
            txt = p.get("text")

            project_keys.append(pk)

            if not txt:
                missing_text += 1

            print(f"--- Record {i} ---")
            print(f"project_key: {pk}")
            print(f"text exists: {bool(txt)}")
            print(f"keys: {list(p.keys())}")
            print()

        # 🔥 Summary
        print("\n📊 Summary:")
        print(f"  Missing text fields: {missing_text}/{len(records)}")

        counter = Counter(project_keys)
        print("  Project key distribution:")
        for k, v in counter.items():
            print(f"    {k}: {v}")

    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    main()