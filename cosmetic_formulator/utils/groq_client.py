"""
Thin wrapper around the Groq API (OpenAI-compatible /chat/completions endpoint).

The AI is only ever used to EXPLAIN and SUGGEST on top of numbers that the app
has already computed deterministically (pH, cost, regulatory limits, known
incompatibility rules). It is never used as the source of truth for
regulatory or safety facts - that keeps hallucination risk out of the parts
of the tool that actually matter.
"""

import random
import time

import requests

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Groq-hosted models currently available for chat completions (checked against
# console.groq.com/docs/models). Groq deprecates models on short notice, so if
# you get a 400/404 "model not found" error, check that page and update this
# list - the AVAILABLE_MODELS list is the only place you need to edit.
AVAILABLE_MODELS = [
    "openai/gpt-oss-120b",   # Production model - flagship, best quality
    "openai/gpt-oss-20b",    # Production model - smaller/faster/cheaper
    "groq/compound",         # Production system - can auto-use web search/code execution tools
    "groq/compound-mini",    # Production system - lighter version of the above
    "qwen/qwen3.6-27b",      # Preview model - strong reasoning, but Groq preview models can be pulled without much notice
]

SYSTEM_PROMPT = """You are a senior cosmetic formulation scientist assisting an R&D chemist.
You will be given:
1. A proposed formula (ingredients + % concentration)
2. Deterministically computed results: estimated pH/viscosity/stability, a regulatory
   compliance check for one or more regions, a rule-based ingredient compatibility
   check, and a cost breakdown.

Your job is ONLY to interpret and add qualitative judgment on top of these computed
results - do not invent new numeric regulatory limits, costs, or pH values that
were not given to you. If asked about a regulatory limit not present in the data
provided, say the tool's sample database does not cover it and recommend checking
the official regional regulator (EU CosIng / EC database, US FDA, India BIS/CDSCO).

Be concise, practical, and specific to cosmetic chemistry (emulsification, phase
behavior, preservation efficacy, sensory/texture, stability testing recommendations).
Always end with a short disclaimer that this is a formulation-assistance draft, not
a substitute for lab stability testing, a certified regulatory affairs review, or a
qualified cosmetic chemist / toxicologist sign-off.
"""

# Retry policy for transient errors (rate limits, server hiccups, timeouts).
MAX_RETRIES = 4
BASE_DELAY_SECONDS = 1.5
MAX_DELAY_SECONDS = 20
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class GroqError(Exception):
    """Raised for errors the caller should show to the user as-is (already friendly)."""
    pass


class GroqRateLimitError(GroqError):
    """Raised specifically when Groq is still rate-limiting after all retries -
    lets callers (e.g. the UI) show a distinct, more actionable message if they want."""
    pass


def _parse_retry_after(resp) -> float | None:
    """Groq sends a Retry-After header (seconds) on 429s when available."""
    header_val = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if header_val is None:
        return None
    try:
        return max(0.0, float(header_val))
    except (TypeError, ValueError):
        return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    """Exponential backoff with jitter, honoring the server's Retry-After if given."""
    if retry_after is not None:
        return min(retry_after + random.uniform(0, 0.5), MAX_DELAY_SECONDS)
    delay = min(BASE_DELAY_SECONDS * (2 ** attempt), MAX_DELAY_SECONDS)
    return delay + random.uniform(0, 0.5)


def call_groq(api_key: str, model: str, user_message: str, temperature: float = 0.4,
              system_override: str = None, on_retry=None) -> str:
    """Send a chat completion request to Groq and return the text reply.

    Automatically retries on rate limits (429) and transient server errors
    (500/502/503/504) or network timeouts, with exponential backoff (honoring
    the Retry-After header when Groq sends one). After MAX_RETRIES failed
    attempts, raises a clear, user-friendly GroqRateLimitError/GroqError
    rather than a raw HTTP error.

    system_override lets callers (e.g. the formula generator) swap in a
    different system prompt than the default formulation-chat one.

    on_retry, if given, is called as on_retry(attempt, max_attempts, wait_seconds,
    reason) before each retry sleep - lets the UI show live "retrying..." status.
    """
    if not api_key:
        raise GroqError("No Groq API key configured. Add one in the sidebar, or set GROQ_API_KEY in Secrets/.env.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": 3000,
        "messages": [
            {"role": "system", "content": system_override or SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }

    last_exception = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=60)
        except requests.exceptions.Timeout:
            last_exception = GroqError("Groq didn't respond in time (timeout).")
            if attempt < MAX_RETRIES:
                wait = _backoff_delay(attempt, None)
                if on_retry:
                    on_retry(attempt + 1, MAX_RETRIES + 1, wait, "timeout")
                time.sleep(wait)
                continue
            raise last_exception
        except requests.exceptions.RequestException as e:
            raise GroqError(f"Network error contacting Groq: {e}")

        if resp.status_code == 200:
            data = resp.json()
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError):
                raise GroqError(f"Unexpected Groq response shape: {data}")

        if resp.status_code == 401:
            raise GroqError("Groq rejected the API key (401 Unauthorized). Double-check the key in the sidebar or your Secrets/.env.")

        if resp.status_code == 404:
            raise GroqError(f"Model \"{model}\" was not found (404) - it may have been deprecated. Try a different model from the dropdown.")

        if resp.status_code in RETRYABLE_STATUS_CODES:
            retry_after = _parse_retry_after(resp)
            reason = "rate limit" if resp.status_code == 429 else f"server error {resp.status_code}"
            if attempt < MAX_RETRIES:
                wait = _backoff_delay(attempt, retry_after)
                if on_retry:
                    on_retry(attempt + 1, MAX_RETRIES + 1, wait, reason)
                time.sleep(wait)
                last_exception = None
                continue
            if resp.status_code == 429:
                raise GroqRateLimitError(
                    "Groq is still rate-limiting requests after several retries. "
                    "This usually clears within a minute or two - wait a bit and try again, "
                    "or check your plan's rate limits at console.groq.com."
                )
            raise GroqError(f"Groq's servers are having trouble (HTTP {resp.status_code}) even after retrying. Try again shortly.")

        # Non-retryable 4xx (e.g. 400 bad request) - surface immediately, no point retrying.
        raise GroqError(f"Groq API error {resp.status_code}: {resp.text[:300]}")

    if last_exception:
        raise last_exception
    raise GroqError("Groq request failed for an unknown reason after retrying.")
