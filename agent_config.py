#!/usr/bin/env python3
"""
agent_config.py — Shared LLM gateway wrapper for all UC-2/3/4 agents
=====================================================================
Single import point.  Every agent calls:

    from agent_config import call_llm, embed, get_gateway

and adds two lines to its .env to control model + provider:

    RISK_SCORER_V1_CHAT_MODEL=qwen2.5:14b-instruct
    RISK_SCORER_V1_CHAT_PROVIDER=ollama          # or openai / claude / gemini

Resolution order (mirrors llm_gateway behaviour):
  1. {AGENT_UPPER}_{PURPOSE_UPPER}_MODEL   e.g. RISK_SCORER_V1_CHAT_MODEL
  2. {AGENT_UPPER}_MODEL                   e.g. RISK_SCORER_V1_MODEL
  3. fallback_model arg                    e.g. os.getenv("CHAT_MODEL")

Provider resolution order:
  1. {AGENT_UPPER}_{PURPOSE_UPPER}_PROVIDER
  2. {AGENT_UPPER}_LLM_PROVIDER
  3. {AGENT_UPPER}_PROVIDER
  4. global LLM_PROVIDER from .env

Agent names used in this pipeline
──────────────────────────────────
  risk_scorer_v1
  classifier_v1
  true_failure_rca_v1
  false_failure_rca_v1
  selector_healer_v1

Example .env additions (copy to your .env):
──────────────────────────────────────────
  # Risk scorer
  RISK_SCORER_V1_CHAT_MODEL=qwen2.5:14b-instruct
  RISK_SCORER_V1_CHAT_PROVIDER=ollama

  # Classifier  (embeddings + LLM verdict)
  CLASSIFIER_V1_CHAT_MODEL=llama3:8b
  CLASSIFIER_V1_EMBEDDING_MODEL=mxbai-embed-large:latest
  CLASSIFIER_V1_CHAT_PROVIDER=ollama

  # True failure RCA  (needs strong reasoning)
  TRUE_FAILURE_RCA_V1_CHAT_MODEL=qwen2.5:14b-instruct
  TRUE_FAILURE_RCA_V1_CHAT_PROVIDER=ollama

  # False failure RCA
  FALSE_FAILURE_RCA_V1_CHAT_MODEL=llama3:8b
  FALSE_FAILURE_RCA_V1_CHAT_PROVIDER=ollama

  # Selector healer
  SELECTOR_HEALER_V1_CHAT_MODEL=llama3:8b
  SELECTOR_HEALER_V1_EMBEDDING_MODEL=mxbai-embed-large:latest
  SELECTOR_HEALER_V1_CHAT_PROVIDER=ollama
"""

from __future__ import annotations

import os
from typing import List, Optional
from dotenv import load_dotenv

load_dotenv()

# Global fallbacks (from .env, same as existing scripts)
_GLOBAL_CHAT_MODEL      = os.getenv("CHAT_MODEL",       "llama3:8b")
_GLOBAL_EMBED_MODEL     = os.getenv("EMBEDDING_MODEL",  "mxbai-embed-large:latest")
_GLOBAL_PROVIDER        = os.getenv("LLM_PROVIDER",     "ollama").lower()


def get_gateway(agent_name: str, purpose: str = "chat"):
    """
    Return an initialised LLMGateway for the correct provider.

    Reads {AGENT_UPPER}_{PURPOSE_UPPER}_PROVIDER (or fallbacks) from .env
    so each agent can point at a different provider without code changes.
    """
    from llm_gateway import get_llm_gateway
    base_gw  = get_llm_gateway()                                   # global provider
    provider = base_gw.resolve_provider_for_agent(
        agent_name, purpose, fallback_provider=_GLOBAL_PROVIDER
    )
    return get_llm_gateway(provider)                               # provider-specific singleton


def _resolve_model(agent_name: str, purpose: str, fallback: Optional[str] = None) -> str:
    from llm_gateway import get_llm_gateway
    base_gw = get_llm_gateway()
    default = fallback or (_GLOBAL_EMBED_MODEL if purpose == "embedding" else _GLOBAL_CHAT_MODEL)
    return base_gw.resolve_model_for_agent(agent_name, purpose, fallback_model=default) or default


def call_llm(
    agent_name: str,
    prompt:     str,
    system:     str = "",
    purpose:    str = "chat",
) -> str:
    """
    Route a chat call through the correct provider + model for this agent.

    Usage:
        from agent_config import call_llm
        raw = call_llm("risk_scorer_v1", prompt, system=system_prompt)
    """
    gw    = get_gateway(agent_name, purpose)
    model = _resolve_model(agent_name, purpose)
    try:
        return gw.chat(prompt, system, model_override=model) or ""
    except Exception as exc:
        print(f"  ⚠ [{agent_name}] LLM call failed ({gw.provider}/{model}): {exc}")
        return ""


def embed(agent_name: str, text: str) -> List[float]:
    """
    Route an embedding call through the correct provider + model for this agent.

    Usage:
        from agent_config import embed
        vector = embed("classifier_v1", "my text")
    """
    gw    = get_gateway(agent_name, purpose="embedding")
    model = _resolve_model(agent_name, purpose="embedding")
    try:
        return gw.generate_embedding(text, model_override=model) or []
    except Exception as exc:
        print(f"  ⚠ [{agent_name}] Embedding failed ({gw.provider}/{model}): {exc}")
        return []


def log_agent_config(agent_name: str) -> None:
    """Print the resolved provider + models for this agent (useful for debugging)."""
    chat_provider = get_gateway(agent_name, "chat").provider
    emb_provider  = get_gateway(agent_name, "embedding").provider
    chat_model    = _resolve_model(agent_name, "chat")
    emb_model     = _resolve_model(agent_name, "embedding")
    print(f"  [{agent_name}] chat={chat_provider}/{chat_model}  embed={emb_provider}/{emb_model}")
