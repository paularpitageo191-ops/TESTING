import argparse
from qdrant_client import QdrantClient


def main():
    parser = argparse.ArgumentParser(description="Inspect Qdrant collection payloads")
    parser.add_argument("--collection", required=True, help="Collection name")
    parser.add_argument("--limit", type=int, default=5, help="Number of points to fetch")
    parser.add_argument("--url", default="http://localhost:6333", help="Qdrant URL")

    args = parser.parse_args()

    client = QdrantClient(url=args.url)

    print(f"\n🔍 Inspecting collection: {args.collection}")
    print(f"Fetching {args.limit} records...\n")

    try:
        records, _ = client.scroll(
            collection_name=args.collection,
            limit=args.limit,
            with_payload=True
        )

        if not records:
            print("⚠ No records found in collection.")
            return

        for i, r in enumerate(records, 1):
            print(f"--- Record {i} ---")
            print(r.payload)
            print()

    except Exception as e:
        print(f"❌ Error: {e}")


if __name__ == "__main__":
    main()