"""
gemini.py
Embeddings and LLM generation for Ask AI.
Includes exponential backoff retries for reliability.
"""
import env  # noqa: F401 - load .env BEFORE reading GEMINI_API_KEY below

import os
import json
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

# Updated model with explicit dimensionality support
EMBED_MODEL = "gemini-embedding-001"
EMBED_DIM = 768

# Read from .env, defaulting to safe fallbacks
CHAT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash-lite")
TIMEOUT_SECONDS = int(os.environ.get("GEMINI_TIMEOUT_SECONDS", 15))
MAX_RETRIES = 3

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_pool = ThreadPoolExecutor(max_workers=4)


def _execute_with_retry(url, body):
    """
    Helper to execute a Gemini API request with exponential backoff.
    Handles 429s, 500s, and network timeouts, and always surfaces the
    API's JSON error body so 4xx failures are diagnosable.
    """
    headers = {"Content-Type": "application/json"}
    data = body.encode("utf-8")

    for attempt in range(MAX_RETRIES):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
                return json.loads(resp.read())

        except urllib.error.HTTPError as e:
            # Read the body once so we can print Google's actual message
            try:
                body_text = e.read().decode("utf-8", errors="replace")
            except Exception:
                body_text = "<unreadable>"
            detail = f"HTTP {e.code} {e.reason}"
            if body_text:
                detail += f" - {body_text[:600]}"

            if e.code in (429, 500, 503):
                if attempt == MAX_RETRIES - 1:
                    raise RuntimeError(f"Gemini API error: {detail}") from e
                sleep_time = 2 ** attempt  # 1s, 2s, 4s
                print(f"Gemini API {detail}. Retrying in {sleep_time}s... "
                      f"(Attempt {attempt + 1}/{MAX_RETRIES})")
                time.sleep(sleep_time)
            else:
                # 400, 401, 403, 404 - fail fast with the real body
                raise RuntimeError(f"Gemini API error: {detail}") from e

        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(f"Gemini network error: {e}") from e
            sleep_time = 2 ** attempt
            print(f"Gemini Network/Timeout error: {e}. Retrying in {sleep_time}s... "
                  f"(Attempt {attempt + 1}/{MAX_RETRIES})")
            time.sleep(sleep_time)


def _embed(text, task_type):
    """Internal helper to embed text with a specific task type."""
    url = f"{API_BASE}/models/{EMBED_MODEL}:embedContent?key={GEMINI_API_KEY}"
    body = json.dumps({
        "model": f"models/{EMBED_MODEL}",
        "content": {"parts": [{"text": text}]},
        "taskType": task_type,
        # REST API expects camelCase (snake_case is the Python-SDK spelling).
        "outputDimensionality": EMBED_DIM,
    })

    data = _execute_with_retry(url, body)
    values = data["embedding"]["values"]

    # Safety check: ensure the dimension matches what we expect
    if len(values) != EMBED_DIM:
        raise ValueError(
            f"Embedding dimension mismatch: got {len(values)}, expected {EMBED_DIM}"
        )
    return values


def embed_query(text):
    """Embed a user's search query."""
    return _embed(text, "RETRIEVAL_QUERY")


def embed_document(text):
    """Embed a document chunk for ingestion (used by ingest_rag.py)."""
    return _embed(text, "RETRIEVAL_DOCUMENT")


def generate_answer(question, context):
    prompt = f"""You are the LeadWise AI assistant. Answer the user's question using ONLY the context provided below. 
If the answer is not in the context, say "I don't have that information, but you can contact us at contact@lead-wise-ai.com".

Context:
{context}

Question: {question}
Answer:"""

    url = f"{API_BASE}/models/{CHAT_MODEL}:generateContent?key={GEMINI_API_KEY}"
    body = json.dumps({"contents": [{"parts": [{"text": prompt}]}]})

    try:
        # We use the thread pool to enforce a hard timeout, just in case the network hangs
        # despite the urllib timeout.
        future = _pool.submit(_execute_with_retry, url, body)
        resp = future.result(timeout=(TIMEOUT_SECONDS * MAX_RETRIES) + 10)

        return resp["candidates"][0]["content"]["parts"][0]["text"]

    except FutureTimeout:
        print("Gemini Gen error: Hard thread timeout exceeded.")
        return "Sorry, I am having trouble answering right now."
    except Exception as e:
        print(f"Gemini Gen error after retries: {e}")
        return "Sorry, I am having trouble answering right now."