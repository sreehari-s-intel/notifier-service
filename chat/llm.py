"""
chat/llm.py
Ollama LLM integration for natural language log analysis.
"""

import json
import logging
import requests

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a log analysis assistant. You help users query and understand application logs stored in Elasticsearch.

When a user asks a question about logs, you will:
1. First, analyze their question to determine what time range and search criteria they need.
2. Then, after receiving the log data, provide a clear, concise summary answering their question.

When analyzing the user's question to build a query, respond with a JSON object like:
{
    "source": "elasticsearch|documents|auto",
    "time_from": "ISO 8601 datetime or relative like '1h', '30m', '2d'",
    "time_to": "ISO 8601 datetime or 'now'",
    "search_terms": ["keyword1", "keyword2"],
    "fields": ["message", "log.level"],
    "indices": ["qpool-*", "eventlog-*", "sol-*"],
    "analysis_type": "count|list|summary|trend",
    "max_results": 50
}

Relative time formats: '5m' = 5 minutes, '1h' = 1 hour, '2d' = 2 days, '1w' = 1 week.

If the user doesn't specify a time, default to the last 1 hour.
If the user mentions specific services or indices (like qpool, eventlog, sol, jenkins), include the relevant values in indices.
If the user asks about uploaded documents/files, set source to "documents".
Only respond with the JSON when asked to parse a query. When summarizing results, respond naturally."""

PARSE_PROMPT = """Parse the following user question about logs into a structured query.
The current date/time context will be provided. Respond ONLY with a valid JSON object.

User question: {question}

Respond with JSON only, no markdown, no explanation."""

SUMMARIZE_PROMPT = """Based on the following log data retrieved from Elasticsearch, answer the user's question concisely.

User's question: {question}

Log data ({hit_count} results found):
{log_data}

Provide a clear, helpful answer. If there are patterns, highlight them. If the data is empty, say so clearly."""

DOCUMENT_SUMMARIZE_PROMPT = """You are given excerpts retrieved from uploaded documents using RAG.
Answer the user's question directly and clearly.

User's question: {question}

Retrieved excerpts ({chunk_count}):
{chunk_data}

Instructions:
- Provide a straightforward answer focused on the question.
- Summarize key findings, inferred root cause, and supporting evidence.
- If evidence is weak or conflicting, state that explicitly.
- Do not just list raw excerpts unless absolutely necessary."""


def call_ollama(prompt: str, system: str = SYSTEM_PROMPT, model: str = "llama3.2",
                ollama_url: str = "http://localhost:11434", timeout_seconds: int = 120) -> str:
    """Call Ollama API for chat completion."""
    try:
        base_url = ollama_url.rstrip("/")
        resp = requests.post(
            f"{base_url}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            timeout=timeout_seconds,
        )

        if resp.status_code == 200:
            data = resp.json()
            return data.get("message", {}).get("content", "")

        # Ollama may return 404 for unknown model, or older/non-standard servers
        # may not expose /api/chat. Try to provide a precise hint first.
        error_text = ""
        try:
            payload = resp.json()
            error_text = str(payload.get("error", ""))
        except Exception:
            error_text = (resp.text or "").strip()

        if resp.status_code == 404 and "model" in error_text.lower():
            raise RuntimeError(
                f"Ollama model '{model}' not found. Pull it first: ollama pull {model}"
            )

        if resp.status_code == 404:
            logger.warning("/api/chat not available at %s; falling back to /api/generate", base_url)
            fallback_resp = requests.post(
                f"{base_url}/api/generate",
                json={
                    "model": model,
                    "system": system,
                    "prompt": prompt,
                    "stream": False,
                },
                timeout=timeout_seconds,
            )

            if fallback_resp.status_code == 200:
                fallback_data = fallback_resp.json()
                return fallback_data.get("response", "")

            try:
                fb_payload = fallback_resp.json()
                fb_error = str(fb_payload.get("error", fallback_resp.text))
            except Exception:
                fb_error = fallback_resp.text

            if fallback_resp.status_code == 404 and "model" in fb_error.lower():
                raise RuntimeError(
                    f"Ollama model '{model}' not found. Pull it first: ollama pull {model}"
                )

            raise RuntimeError(
                f"Ollama endpoint returned {fallback_resp.status_code} on /api/generate: {fb_error}"
            )

        raise RuntimeError(
            f"Ollama endpoint returned {resp.status_code} on /api/chat: {error_text}"
        )
    except requests.exceptions.ConnectionError:
        logger.error(f"Cannot connect to Ollama at {ollama_url}")
        raise RuntimeError(f"Cannot connect to Ollama at {ollama_url}. Is it running?")
    except Exception as e:
        logger.error(f"Ollama API error: {e}")
        raise RuntimeError(f"LLM error: {e}")


def parse_user_query(question: str, model: str = "llama3.2",
                     ollama_url: str = "http://localhost:11434", timeout_seconds: int = 60) -> dict:
    """Use LLM to parse a natural language question into structured ES query params."""
    prompt = PARSE_PROMPT.format(question=question)
    raw = call_ollama(
        prompt,
        system=SYSTEM_PROMPT,
        model=model,
        ollama_url=ollama_url,
        timeout_seconds=timeout_seconds,
    )

    # Try to extract JSON from the response
    raw = raw.strip()
    if raw.startswith("```"):
        # Strip markdown code fences
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: try to find JSON in the response
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(raw[start:end])
        logger.warning(f"Could not parse LLM response as JSON: {raw}")
        # Return a default query
        return {
            "source": "auto",
            "time_from": "1h",
            "time_to": "now",
            "search_terms": [question],
            "indices": [],
            "analysis_type": "summary",
            "max_results": 50,
        }


def summarize_results(question: str, hits: list[dict], model: str = "llama3.2",
                      ollama_url: str = "http://localhost:11434", timeout_seconds: int = 120) -> str:
    """Use LLM to generate a natural language summary of ES results."""
    # Format log data for the LLM
    log_entries = []
    for hit in hits[:12]:  # Keep prompt smaller to reduce latency and timeout risk
        entry = f"[{hit.get('timestamp', '?')}] {hit.get('message', '')[:220]}"
        log_entries.append(entry)

    log_data = "\n".join(log_entries) if log_entries else "(No results found)"

    prompt = SUMMARIZE_PROMPT.format(
        question=question,
        hit_count=len(hits),
        log_data=log_data,
    )

    return call_ollama(
        prompt,
        system=SYSTEM_PROMPT,
        model=model,
        ollama_url=ollama_url,
        timeout_seconds=timeout_seconds,
    )


def summarize_document_results(question: str, chunks: list[dict], model: str = "llama3.2",
                               ollama_url: str = "http://localhost:11434", timeout_seconds: int = 120) -> str:
    """Use LLM to answer user prompt directly from retrieved document chunks."""
    chunk_lines = []
    for i, chunk in enumerate(chunks[:10], 1):
        filename = str(chunk.get("filename", "unknown"))
        text = str(chunk.get("text", "")).replace("\n", " ").strip()
        if len(text) > 320:
            text = text[:320] + "..."
        chunk_lines.append(f"[{i}] {filename}: {text}")

    chunk_data = "\n".join(chunk_lines) if chunk_lines else "(No relevant excerpts found)"
    prompt = DOCUMENT_SUMMARIZE_PROMPT.format(
        question=question,
        chunk_count=len(chunks),
        chunk_data=chunk_data,
    )

    return call_ollama(
        prompt,
        system=SYSTEM_PROMPT,
        model=model,
        ollama_url=ollama_url,
        timeout_seconds=timeout_seconds,
    )
