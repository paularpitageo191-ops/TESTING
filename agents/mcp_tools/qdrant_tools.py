#!/usr/bin/env python3
"""Stateless Qdrant MCP tools."""

import os
import uuid
from typing import Any, Dict, List, Optional

import requests
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http.exceptions import UnexpectedResponse
from qdrant_client.models import Distance, PointStruct, VectorParams

from llm_gateway import get_llm_gateway

load_dotenv()

QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")


def _response_payload(ok: bool, data: Any = None, error: str = "") -> Dict[str, Any]:
    return {"ok": ok, "data": data, "error": error}


def _client() -> QdrantClient:
    return QdrantClient(url=QDRANT_URL)


def _infer_vector_size(text: str = "probe") -> int:
    vector = get_llm_gateway().generate_embedding(text)
    return len(vector) if vector else 1024


def _ensure_collection(collection_name: str, vector_size: int) -> None:
    client = _client()
    try:
        client.get_collection(collection_name)
    except (UnexpectedResponse, ValueError):
        client.create_collection(
            collection_name=collection_name,
            vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
        )


def vector_search(
    collection_name: str,
    query: str,
    limit: int = 5,
    project_key: Optional[str] = None,
) -> Dict[str, Any]:
    """Semantic search against a Qdrant collection."""
    if not collection_name:
        return _response_payload(False, error="collection_name is required")
    if not query:
        return _response_payload(False, error="query is required")

    vector = get_llm_gateway().generate_embedding(query)
    if not vector:
        return _response_payload(False, error="Failed to generate embedding for query")

    body: Dict[str, Any] = {
        "vector": vector,
        "limit": limit,
        "with_payload": True,
    }
    if project_key:
        body["filter"] = {"must": [{"key": "project_key", "match": {"value": project_key}}]}

    try:
        response = requests.post(
            f"{QDRANT_URL}/collections/{collection_name}/points/search",
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        hits = response.json().get("result", [])
        return _response_payload(
            True,
            data={
                "collection_name": collection_name,
                "query": query,
                "matches": [
                    {
                        "id": hit.get("id"),
                        "score": hit.get("score"),
                        "payload": hit.get("payload", {}),
                    }
                    for hit in hits
                ],
            },
        )
    except Exception as exc:
        return _response_payload(False, error=str(exc))


def vector_upsert(
    collection_name: str,
    text: str,
    payload: Optional[Dict[str, Any]] = None,
    point_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Upsert a single vector point into Qdrant."""
    if not collection_name:
        return _response_payload(False, error="collection_name is required")
    if not text:
        return _response_payload(False, error="text is required")

    vector = get_llm_gateway().generate_embedding(text)
    if not vector:
        return _response_payload(False, error="Failed to generate embedding for text")

    payload = payload or {}
    payload.setdefault("text", text)
    point_id = point_id or str(uuid.uuid4())

    try:
        _ensure_collection(collection_name, len(vector) or _infer_vector_size())
        _client().upsert(
            collection_name=collection_name,
            points=[PointStruct(id=point_id, vector=vector, payload=payload)],
        )
        return _response_payload(
            True,
            data={
                "collection_name": collection_name,
                "point_id": point_id,
                "payload": payload,
            },
        )
    except Exception as exc:
        return _response_payload(False, error=str(exc))

