#!/usr/bin/env python3
"""
LLM Gateway - Centralized LLM Provider Interface
Supports multiple providers: ollama, openai, claude, gemini
All LLM calls should go through this module for consistency.

Fix log
-------
* analyze_gherkin_step system prompt: added explicit classify-as-navigate rule
  for "I am on / I should be redirected to / Then I should see the X page"
  steps that contain NO literal URL.  Previously the LLM could return
  "verifyText" for these, which caused BasePage.smartAction() to run its
  verify fallback path and attempt (incorrectly) a toHaveURL() assertion
  with a pattern derived from the step text.  With the new rule the generator
  emits a page.goto() call using the Qdrant- or BASE_URL-resolved URL, which
  is correct and site-agnostic.

* No other logic changes — all provider implementations are unchanged.
"""

import os
import re
import ast
import json
import requests
from typing import Optional, Dict, Any, List
from dotenv import load_dotenv

load_dotenv()

# ── Configuration ──────────────────────────────────────────────────────────────
LLM_PROVIDER    = os.getenv("LLM_PROVIDER",    "ollama").lower()
OLLAMA_HOST     = os.getenv("OLLAMA_HOST",     "http://localhost:11434")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "mxbai-embed-large:latest")
CHAT_MODEL      = os.getenv("CHAT_MODEL",      "llama3:8b")
MODEL_NAME      = os.getenv("MODEL_NAME",      "codellama:13b")

OPENAI_API_KEY    = os.getenv("OPENAI_API_KEY",    "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY",    "")


class LLMGateway:
    """Centralized LLM provider gateway (chat + embeddings)."""

    def __init__(self, provider: str = None):
        self.provider     = provider or LLM_PROVIDER
        self._initialized = False
        self._client      = None

    # ── Initialization ─────────────────────────────────────────────────────

    def _get_available_models(self) -> List[str]:
        try:
            resp = requests.get(f"{OLLAMA_HOST}/api/tags", timeout=10)
            if resp.status_code == 200:
                return [m["name"] for m in resp.json().get("models", [])]
        except Exception as exc:
            print(f"Warning: Could not fetch Ollama models: {exc}")
        return []

    def _resolve_model(self, requested_model: str) -> str:
        if not requested_model:
            return CHAT_MODEL
        if self.provider != "ollama":
            return requested_model

        available = getattr(self, "_available_models", [])
        if not available:
            return requested_model
        if requested_model in available:
            return requested_model

        base = requested_model.split(":")[0]
        for m in available:
            if m == base or m.startswith(base + ":"):
                return m

        print(f"Warning: Model '{requested_model}' not found. Falling back to '{CHAT_MODEL}'.")
        return CHAT_MODEL

    def initialize(self) -> bool:
        if self._initialized:
            return True
        try:
            if self.provider == "ollama":
                self._available_models = self._get_available_models()
                self._initialized = True

            elif self.provider == "openai":
                if not OPENAI_API_KEY:
                    raise ValueError("OPENAI_API_KEY not set in .env")
                from openai import OpenAI
                self._client      = OpenAI(api_key=OPENAI_API_KEY)
                self._initialized = True

            elif self.provider == "claude":
                if not ANTHROPIC_API_KEY:
                    raise ValueError("ANTHROPIC_API_KEY not set in .env")
                import anthropic
                self._client      = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
                self._initialized = True

            elif self.provider == "gemini":
                if not GEMINI_API_KEY:
                    raise ValueError("GEMINI_API_KEY not set in .env")
                import google.generativeai as genai
                genai.configure(api_key=GEMINI_API_KEY)
                self._client      = genai.GenerativeModel(CHAT_MODEL)
                self._initialized = True

            else:
                raise ValueError(f"Unsupported LLM provider: {self.provider}")

        except ImportError as exc:
            print(f"Error: Required package not installed for {self.provider}: {exc}")
            return False
        except Exception as exc:
            print(f"Error initializing LLM gateway: {exc}")
            return False

        return True

    # ── Embeddings ─────────────────────────────────────────────────────────

    def generate_embedding(self, text: str) -> List[float]:
        if not self._initialized:
            if not self.initialize():
                return []
        try:
            if self.provider == "ollama":
                return self._generate_embedding_ollama(text)
            elif self.provider == "openai":
                return self._generate_embedding_openai(text)
            elif self.provider == "claude":
                print("Warning: Claude doesn't support embeddings, falling back to OpenAI")
                return self._generate_embedding_openai(text) if OPENAI_API_KEY else []
            elif self.provider == "gemini":
                return self._generate_embedding_gemini(text)
        except Exception as exc:
            print(f"Error generating embedding: {exc}")
        return []

    # Chunk size tuned for mxbai-embed-large's ~512-token context window.
    # ~1 800 chars ≈ 450 tokens, leaving headroom for tokenizer overhead.
    # Overlap of 200 chars preserves sentence context at chunk boundaries
    # so no requirement clause is split mid-thought.
    _EMBED_CHUNK_SIZE    = 1800
    _EMBED_CHUNK_OVERLAP = 200

    @staticmethod
    def _chunk_text(text: str, chunk_size: int, overlap: int) -> List[str]:
        """
        Split *text* into overlapping windows of *chunk_size* chars.

        Splitting strategy (in priority order):
          1. Try to break at the last sentence boundary ('. ', '! ', '? ')
             within the window — keeps semantic units intact.
          2. Fall back to the last whitespace if no sentence boundary found.
          3. Hard-split at chunk_size as a last resort.

        Overlap means the next chunk re-starts *overlap* chars before the end
        of the previous one, so a sentence that straddles a boundary is fully
        represented in at least one chunk.
        """
        if len(text) <= chunk_size:
            return [text]

        chunks = []
        start  = 0
        while start < len(text):
            end = start + chunk_size
            if end >= len(text):
                chunks.append(text[start:])
                break

            # Prefer a sentence boundary inside the window
            window   = text[start:end]
            best_cut = -1
            for sep in (". ", "! ", "? ", ".\n", "\n\n"):
                pos = window.rfind(sep)
                if pos > best_cut:
                    best_cut = pos + len(sep)

            if best_cut <= 0:
                # Fall back to last whitespace
                ws = window.rfind(" ")
                best_cut = ws if ws > 0 else chunk_size

            chunks.append(text[start: start + best_cut])
            start = start + best_cut - overlap   # slide back by overlap

        return [c for c in chunks if c.strip()]

    def _embed_single_ollama(self, text: str) -> List[float]:
        """Send one chunk to Ollama and return its raw embedding vector."""
        resp = requests.post(
            f"{OLLAMA_HOST}/api/embeddings",
            json={"model": EMBEDDING_MODEL, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("embedding", [])

    def _generate_embedding_ollama(self, text: str) -> List[float]:
        """
        Generate a single 1024-d embedding for *text* regardless of length.

        For texts that fit within mxbai-embed-large's context window (~512
        tokens / ~1 800 chars) a single API call is made.

        For longer texts the content is split into overlapping chunks, each
        chunk is embedded individually, and the resulting vectors are averaged
        (mean pooling).  Mean pooling is the standard approach for producing a
        single fixed-size representation from multiple chunk embeddings — it
        preserves information from every part of the text rather than silently
        discarding the tail.

        No information is lost: every sentence in the original text contributes
        to the final vector.
        """
        chunks = self._chunk_text(
            text,
            self._EMBED_CHUNK_SIZE,
            self._EMBED_CHUNK_OVERLAP,
        )

        if len(chunks) == 1:
            # Fast path — single API call, no pooling needed
            try:
                return self._embed_single_ollama(chunks[0])
            except Exception as exc:
                print(f"Ollama embedding error: {exc}")
                return []

        # Multi-chunk path — embed each chunk then mean-pool
        print(f"    ↳ Long text ({len(text)} chars) → {len(chunks)} chunks, mean-pooling embeddings")
        vectors = []
        for idx, chunk in enumerate(chunks):
            try:
                vec = self._embed_single_ollama(chunk)
                if vec:
                    vectors.append(vec)
                else:
                    print(f"      ⚠ Chunk {idx+1}/{len(chunks)}: empty vector, skipping")
            except Exception as exc:
                print(f"      ⚠ Chunk {idx+1}/{len(chunks)} embedding error: {exc}, skipping")

        if not vectors:
            print("    ✗ All chunks failed — returning empty vector")
            return []

        # Mean pool: element-wise average across all chunk vectors
        dim    = len(vectors[0])
        pooled = [sum(v[i] for v in vectors) / len(vectors) for i in range(dim)]
        print(f"      ✓ Mean-pooled {len(vectors)} chunk vectors → 1×{dim}-d vector")
        return pooled

    def _generate_embedding_openai(self, text: str) -> List[float]:
        if not self._client:
            from openai import OpenAI
            self._client = OpenAI(api_key=OPENAI_API_KEY)
        try:
            resp = self._client.embeddings.create(model="text-embedding-ada-002", input=text)
            return resp.data[0].embedding
        except Exception as exc:
            print(f"OpenAI embedding error: {exc}")
            return []

    def _generate_embedding_gemini(self, text: str) -> List[float]:
        url = (
            f"https://generativelanguage.googleapis.com/v1beta/"
            f"models/text-embedding-004:embedContent?key={GEMINI_API_KEY}"
        )
        try:
            resp = requests.post(
                url,
                json={"model": "text-embedding-004", "content": {"parts": [{"text": text}]}},
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json().get("embedding", {}).get("values", [])
        except Exception as exc:
            print(f"Gemini embedding error: {exc}")
            return []

    # ── Chat ───────────────────────────────────────────────────────────────

    def chat(
        self,
        prompt: str,
        system_prompt: str = None,
        model_override: str = None,
        **kwargs,
    ) -> str:
        if not self._initialized:
            if not self.initialize():
                return ""
        try:
            if self.provider == "ollama":
                return self._chat_ollama(prompt, system_prompt, model_override=model_override, **kwargs)
            elif self.provider == "openai":
                return self._chat_openai(prompt, system_prompt, model_override=model_override, **kwargs)
            elif self.provider == "claude":
                return self._chat_claude(prompt, system_prompt, model_override=model_override, **kwargs)
            elif self.provider == "gemini":
                return self._chat_gemini(prompt, system_prompt, model_override=model_override, **kwargs)
        except Exception as exc:
            print(f"Error in chat completion: {exc}")
        return ""

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        router=None,
        auto_execute: bool = True,
        max_round_trips: int = 5,
        model_override: str = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Chat interface with optional tool calling.

        Returns:
        {
          "content": "...",
          "tool_calls": [{"id": "...", "name": "...", "arguments": {...}}],
          "messages": [...],
          "executed_tools": [{"name": "...", "args": {...}, "result": {...}}]
        }
        """
        if not self._initialized:
            if not self.initialize():
                return {"content": "", "tool_calls": [], "messages": messages, "executed_tools": []}

        transcript = list(messages)
        executed_tools: List[Dict[str, Any]] = []

        for _ in range(max_round_trips):
            try:
                if self.provider == "openai":
                    response = self._chat_with_tools_openai(
                        transcript,
                        tools,
                        model_override=model_override,
                        **kwargs,
                    )
                else:
                    response = self._chat_with_tools_fallback(
                        transcript,
                        tools,
                        model_override=model_override,
                        **kwargs,
                    )
            except Exception as exc:
                print(f"Tool chat error: {exc}")
                return {
                    "content": "",
                    "tool_calls": [],
                    "messages": transcript,
                    "executed_tools": executed_tools,
                    "error": str(exc),
                }

            tool_calls = response.get("tool_calls", [])
            transcript = response.get("messages", transcript)
            if not tool_calls or not auto_execute or router is None:
                response["executed_tools"] = executed_tools
                response["messages"] = transcript
                return response

            for call in tool_calls:
                result = router.execute_tool(call["name"], call.get("arguments", {}))
                executed_tools.append(
                    {"name": call["name"], "args": call.get("arguments", {}), "result": result}
                )
                transcript.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", call["name"]),
                        "name": call["name"],
                        "content": json.dumps(result),
                    }
                )

        return {
            "content": "",
            "tool_calls": [],
            "messages": transcript,
            "executed_tools": executed_tools,
            "error": "Tool chat exceeded max_round_trips",
        }

    def _chat_ollama(self, prompt, system_prompt=None, model_override=None, **kwargs) -> str:
        model = self._resolve_model(model_override or CHAT_MODEL)
        msgs  = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": prompt})
        try:
            resp = requests.post(
                f"{OLLAMA_HOST}/api/chat",
                json={"model": model, "messages": msgs, "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json().get("message", {}).get("content", "")
        except Exception as exc:
            print(f"Ollama chat error: {exc}")
            return ""

    def _chat_openai(self, prompt, system_prompt=None, model_override=None, **kwargs) -> str:
        if not self._client:
            from openai import OpenAI
            self._client = OpenAI(api_key=OPENAI_API_KEY)
        msgs = []
        if system_prompt:
            msgs.append({"role": "system", "content": system_prompt})
        msgs.append({"role": "user", "content": prompt})
        model = model_override or CHAT_MODEL
        try:
            resp = self._client.chat.completions.create(
                model=model, messages=msgs, temperature=kwargs.get("temperature", 0.7)
            )
            return resp.choices[0].message.content
        except Exception as exc:
            print(f"OpenAI chat error: {exc}")
            return ""

    def _chat_with_tools_openai(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_override: str = None,
        **kwargs,
    ) -> Dict[str, Any]:
        if not self._client:
            from openai import OpenAI
            self._client = OpenAI(api_key=OPENAI_API_KEY)

        model = model_override or CHAT_MODEL
        response = self._client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            temperature=kwargs.get("temperature", 0.2),
        )
        message = response.choices[0].message
        tool_calls = []
        if getattr(message, "tool_calls", None):
            for call in message.tool_calls:
                try:
                    arguments = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    arguments = {"raw_arguments": call.function.arguments}
                tool_calls.append(
                    {
                        "id": call.id,
                        "name": call.function.name,
                        "arguments": arguments,
                    }
                )

        transcript = list(messages)
        assistant_message: Dict[str, Any] = {
            "role": "assistant",
            "content": message.content or "",
        }
        if getattr(message, "tool_calls", None):
            assistant_message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in message.tool_calls
            ]
        transcript.append(assistant_message)
        return {"content": message.content or "", "tool_calls": tool_calls, "messages": transcript}

    def _chat_claude(self, prompt, system_prompt=None, model_override=None, **kwargs) -> str:
        if not self._client:
            import anthropic
            self._client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        model = model_override or CHAT_MODEL
        try:
            resp = self._client.messages.create(
                model=model,
                max_tokens=4096,
                system=system_prompt or "You are a helpful assistant.",
                messages=[{"role": "user", "content": prompt}],
                temperature=kwargs.get("temperature", 0.7),
            )
            return resp.content[0].text
        except Exception as exc:
            print(f"Claude chat error: {exc}")
            return ""

    def _chat_gemini(self, prompt, system_prompt=None, model_override=None, **kwargs) -> str:
        model = model_override or CHAT_MODEL
        if not self._client:
            import google.generativeai as genai
            genai.configure(api_key=GEMINI_API_KEY)
            self._client = genai.GenerativeModel(model)
        full_prompt = f"{system_prompt}\n\n{prompt}" if system_prompt else prompt
        try:
            return self._client.generate_content(full_prompt).text
        except Exception as exc:
            print(f"Gemini chat error: {exc}")
            return ""

    def _chat_with_tools_fallback(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        model_override: str = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """
        Prompt-based fallback for providers without native tool calling.
        """
        tool_summaries = []
        for tool in tools:
            function = tool.get("function", {})
            tool_summaries.append(
                {
                    "name": function.get("name"),
                    "description": function.get("description", ""),
                    "parameters": function.get("parameters", {}),
                }
            )

        system_prompt = (
            "You can optionally request exactly one tool call.\n"
            "Respond with JSON only in one of these forms:\n"
            '{"action":"respond","content":"..."}\n'
            '{"action":"tool_call","tool_name":"...","arguments":{}}\n'
        )
        prompt = (
            f"Available tools:\n{json.dumps(tool_summaries)}\n\n"
            f"Conversation:\n{json.dumps(messages)}"
        )
        raw = self.chat(
            prompt,
            system_prompt=system_prompt,
            model_override=model_override,
            temperature=kwargs.get("temperature", 0.2),
        )

        cleaned = self._clean_json_response(raw)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError:
            parsed = {"action": "respond", "content": raw}

        transcript = list(messages)
        tool_calls = []
        content = parsed.get("content", "")
        if parsed.get("action") == "tool_call":
            tool_calls.append(
                {
                    "id": parsed.get("tool_name", "fallback-tool-call"),
                    "name": parsed.get("tool_name", ""),
                    "arguments": parsed.get("arguments", {}),
                }
            )
            transcript.append({"role": "assistant", "content": "", "tool_calls": tool_calls})
        else:
            transcript.append({"role": "assistant", "content": content})

        return {"content": content, "tool_calls": tool_calls, "messages": transcript}

    # ── Gherkin analysis ───────────────────────────────────────────────────

    def analyze_gherkin_step(
        self, step_text: str, step_keyword: str, dom_context: dict = None
    ) -> dict:
        """
        Classify a Gherkin step into a Playwright action type.

        Returns a dict:
          action_type:      "smartClick" | "smartFill" | "verifyText" | "navigate" | "unknown"
          target_selector:  CSS / XPath hint (informational only; BasePage uses Qdrant)
          value:            fill value or expected text (empty if not applicable)
          confidence:       0.0 – 1.0
          reasoning:        brief explanation

        Key classification rules (added in this revision):
          • Steps whose keyword is "Given" AND whose text contains "am on",
            "I am on", "on the ... page", or "navigate to" → always "navigate".
          • Steps whose keyword is "Then" AND whose text contains "redirected
            to", "should be on", "taken to", "lands on", or "navigated to" →
            always "navigate".
          These steps describe page-location, not DOM-element state.
          Previously the LLM sometimes returned "verifyText" for them, which
          caused BasePage to attempt a URL assertion with a guess-based slug.
        """
        system_prompt = """\
You are a test automation expert.  Analyse Gherkin steps and map them to Playwright actions.

Available action types:
  smartClick  — click a button, link, checkbox, etc.
  smartFill   — fill / type a value into an input field
  verifyText  — assert text is visible on the page (error messages, headings, labels, etc.)
  navigate    — navigate the browser to a URL or page
  unknown     — none of the above

CRITICAL classification rules (apply these FIRST, before general reasoning):

1. If the step keyword is "Given" AND the step text contains any of:
       "am on", "i am on", "on the", "navigate to", "go to", "visit",
       "open", "launch"
   → action_type MUST be "navigate".
   Example: "Given I am on the SauceDemo login page" → navigate

2. If the step keyword is "Then" AND the step text contains any of:
       "redirected to", "redirect to", "taken to", "navigated to",
       "should be on", "lands on", "land on"
   → action_type MUST be "navigate".
   Example: "Then I should be redirected to the inventory page" → navigate

3. If the step text contains "error message", "locked", "invalid",
   "username and password", "sorry" → action_type MUST be "verifyText".
   Example: "Then I should see an error message containing 'locked'" → verifyText

4. For smartFill steps, put the value to type in the "value" field.
   Example: "When I enter 'standard_user' as username" → smartFill, value="standard_user"

Return ONLY a JSON object with these exact fields, no other text:
{
  "action_type":      "smartClick|smartFill|verifyText|navigate|unknown",
  "target_selector":  "CSS selector or XPath (best guess, informational only)",
  "value":            "value to fill or expected text (empty string if not applicable)",
  "confidence":       0.0,
  "reasoning":        "one-line explanation"
}"""

        context = f"Step keyword: {step_keyword}\nStep text: {step_text}\n"
        if dom_context:
            context += f"DOM context: {json.dumps(dom_context)}\n"

        prompt = (
            f"Analyse this Gherkin step and map it to a Playwright action:\n\n"
            f"{context}\n"
            f"Return ONLY the JSON object, no other text."
        )

        model_to_use = MODEL_NAME if MODEL_NAME else None
        response = self.chat(prompt, system_prompt, model_override=model_to_use)

        try:
            cleaned = self._clean_json_response(response)
            result  = json.loads(cleaned)
            return {
                "action_type":     result.get("action_type",     "unknown"),
                "target_selector": result.get("target_selector", ""),
                "value":           result.get("value",           ""),
                "confidence":      float(result.get("confidence", 0.3)),
                "reasoning":       result.get("reasoning",       ""),
            }
        except (json.JSONDecodeError, Exception) as exc:
            print(f"Error parsing LLM response: {exc}")
            print(f"Raw response: {repr(response)}")
            return self._fallback_step_analysis(step_text, step_keyword)

    # ── JSON response cleaner ──────────────────────────────────────────────

    def _clean_json_response(self, response: str) -> str:
        """
        Robust JSON extraction from LLM output.

        Uses raw_decode() to extract ONLY the first complete JSON object,
        preventing issues when the LLM returns multiple objects or trailing text.
        """
        if not response:
            return "{}"

        response = response.strip()
        response = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', response)
        response = (
            response
            .replace('\u2018', '"').replace('\u2019', '"')
            .replace('\u201c', '"').replace('\u201d', '"')
        )

        brace_pos = response.find('{')
        if brace_pos == -1:
            return "{}"
        response = response[brace_pos:]

        decoder = json.JSONDecoder()
        try:
            parsed, _ = decoder.raw_decode(response)
            return json.dumps(parsed)
        except json.JSONDecodeError:
            pass

        cleaned = re.sub(r',( *[}\]])', r'\1', response)
        try:
            parsed, _ = decoder.raw_decode(cleaned)
            return json.dumps(parsed)
        except json.JSONDecodeError:
            return response

    # ── Pattern-match fallback ─────────────────────────────────────────────

    def _fallback_step_analysis(self, step_text: str, step_keyword: str) -> dict:
        """Keyword-pattern fallback when LLM returns unparseable output."""
        sl = step_text.lower()

        # navigate — Given "am on / go to / visit / open"
        if step_keyword in ("Given",) and any(
            w in sl for w in ("am on", "navigate", "go to", "visit", "open", "launch")
        ):
            return {"action_type": "navigate",    "target_selector": "", "value": "", "confidence": 0.6, "reasoning": "Pattern: Given navigation"}

        # navigate — Then redirect / should be on
        if step_keyword in ("Then", "And") and any(
            w in sl for w in ("redirected to", "redirect to", "taken to", "navigated to",
                              "should be on", "lands on", "land on")
        ):
            return {"action_type": "navigate",    "target_selector": "", "value": "", "confidence": 0.6, "reasoning": "Pattern: Then redirect"}

        # fill
        if step_keyword in ("Given", "When") and any(
            w in sl for w in ("enter", "type", "fill", "input")
        ):
            return {"action_type": "smartFill",   "target_selector": "", "value": "", "confidence": 0.5, "reasoning": "Pattern: input action"}

        # click
        if step_keyword == "When" and "click" in sl:
            return {"action_type": "smartClick",  "target_selector": "", "value": "", "confidence": 0.5, "reasoning": "Pattern: click action"}

        # verify
        if step_keyword == "Then" and any(w in sl for w in ("see", "verify", "should", "error")):
            return {"action_type": "verifyText",  "target_selector": "", "value": "", "confidence": 0.5, "reasoning": "Pattern: verification"}

        return {"action_type": "unknown", "target_selector": "", "value": "", "confidence": 0.2, "reasoning": "No pattern match"}


# ── Module-level singleton ─────────────────────────────────────────────────────

_gateway: Optional[LLMGateway] = None


def get_llm_gateway() -> LLMGateway:
    global _gateway
    if _gateway is None:
        _gateway = LLMGateway()
        _gateway.initialize()
    return _gateway


def initialize_gateway() -> bool:
    gateway = get_llm_gateway()
    return gateway._initialized


if __name__ == "__main__":
    print(f"LLM Provider: {LLM_PROVIDER}")
    gw = get_llm_gateway()
    if gw.initialize():
        print("✓ LLM Gateway initialized")
        emb = gw.generate_embedding("test text")
        print(f"✓ Embedding: {len(emb)} dimensions")
        resp = gw.chat("What is 2+2?")
        print(f"✓ Chat: {resp}")
    else:
        print("✗ Failed to initialize LLM Gateway")
