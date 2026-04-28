# save as check_form_elements.py and run it
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

client = QdrantClient(url="http://localhost:6333")

results, _ = client.scroll(
    collection_name="SCRUM_70_ui_memory",
    scroll_filter=Filter(must=[
        FieldCondition(key="project_key", match=MatchValue(value="SCRUM-70"))
    ]),
    limit=200,
    with_payload=True,
)

print(f"Total records: {len(results)}\n")

# Print only input/button/textarea elements — skip nav links
for r in results:
    p = r.payload
    kind = p.get("element_type", "")
    sel  = p.get("selector", "")
    page = p.get("page_url", "")
    tag  = p.get("details", {}).get("tagName", "")

    # Skip pure nav links
    if tag == "a" and kind == "interactive":
        continue

    print(f"{kind:12} | {sel:35} | {page}")