"""
Multi-provider AI client (Groq + Google Gemini, both OpenAI-compatible
chat-completions endpoints) with retry/backoff and automatic cross-provider
fallback.

The module keeps the name "groq_client" and the "Groq"-prefixed exception
names for backward compatibility with the rest of the app, even though it
now supports more than one provider - PROVIDERS below is the source of
truth for what's actually configured.

The AI is only ever used to EXPLAIN and SUGGEST on top of numbers that the
app has already computed deterministically (pH, cost, regulatory limits,
known incompatibility rules). It is never used as the source of truth for
regulatory or safety facts - that keeps hallucination risk out of the parts
of the tool that actually matter.
"""

import random
import time

import requests

# --------------------------------------------------------------------------
# Provider registry
# --------------------------------------------------------------------------
# Both providers expose an OpenAI-compatible /chat/completions endpoint that
# accepts the same request shape and Bearer-token auth, so one call function
# (call_llm below) works for either. Adding a third OpenAI-compatible
# provider later just means adding an entry here.
#
# ⚠️ MODEL IDS GO STALE FAST - especially Gemini's. Google retired
# gemini-2.0-flash on 2026-03-31 (about 8 months after release) and had
# shipped FOUR more Flash generations (3, 3.1, 3.5, 3.6) plus a fifth
# (3.7) by August 2026. If you get a 404 "model not found" error, the
# model below has likely been deprecated - check the current lineup at:
#   Groq:   https://console.groq.com/docs/models
#   Gemini: https://ai.google.dev/gemini-api/docs/models (or the
#           OpenAI-compatibility page, which shows a live example model ID)
# and update the "models" list for that provider below. No other code
# needs to change - the dropdown and every prompt reference the model
# string dynamically from this list.
PROVIDERS = {
    "Groq": {
        "chat_url": "https://api.groq.com/openai/v1/chat/completions",
        "models": [
            "openai/gpt-oss-120b",   # Production model - flagship, best quality
            "openai/gpt-oss-20b",    # Production model - smaller/faster/cheaper
            "groq/compound",         # Production system - can auto-use web search/code execution tools
            "groq/compound-mini",    # Production system - lighter version of the above
            "qwen/qwen3.6-27b",      # Preview model - strong reasoning, but preview models can be pulled without much notice
        ],
        "key_env": "GROQ_API_KEY",
        "signup_url": "https://console.groq.com",
        "free_tier_note": "Free tier available; no credit card required. Rate limits vary by model.",
    },
    "Google Gemini": {
        "chat_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "models": [
            "gemini-3.6-flash",       # Current free-tier default (GA/stable, released 2026-07-21) - best all-around choice
            "gemini-3.5-flash-lite",  # Free tier, highest rate limits, best for high-volume/cost-sensitive use
            "gemini-3.5-flash",       # Free tier, previous flagship Flash generation
            "gemini-3.7-flash",       # Newest (released 2026-08-13), most capable, still free-tier eligible
        ],
        "key_env": "GEMINI_API_KEY",
        "signup_url": "https://aistudio.google.com/apikey",
        "free_tier_note": "Genuinely free tier, no credit card required (as of this writing: ~15 requests/min, "
                           "~1,500/day on Flash models - varies by model and changes over time, check "
                           "aistudio.google.com for current limits). Google's terms state free-tier prompts may "
                           "be used to improve their products - keep that in mind for sensitive formulas.",
    },
}

# Kept for backward compatibility - existing code that imports AVAILABLE_MODELS
# gets Groq's list specifically.
AVAILABLE_MODELS = PROVIDERS["Groq"]["models"]

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
    """Raised for errors the caller should show to the user as-is (already
    friendly). Used generically across all providers despite the name."""
    pass


class GroqRateLimitError(GroqError):
    """Raised specifically when a provider is still rate-limiting after all
    retries - lets callers (e.g. the fallback chain) detect this case
    specifically vs. other failures like a bad key."""
    pass


def _parse_retry_after(resp) -> float | None:
    header_val = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
    if header_val is None:
        return None
    try:
        return max(0.0, float(header_val))
    except (TypeError, ValueError):
        return None


def _backoff_delay(attempt: int, retry_after: float | None) -> float:
    if retry_after is not None:
        return min(retry_after + random.uniform(0, 0.5), MAX_DELAY_SECONDS)
    delay = min(BASE_DELAY_SECONDS * (2 ** attempt), MAX_DELAY_SECONDS)
    return delay + random.uniform(0, 0.5)


def call_llm(provider: str, api_key: str, model: str, user_message: str, temperature: float = 0.4,
             system_override: str = None, on_retry=None) -> str:
    """Send a chat completion request to the given provider and return the text reply.

    Works for any provider registered in PROVIDERS (currently Groq and
    Google Gemini) since both expose an OpenAI-compatible endpoint with the
    same Bearer-auth request/response shape.

    Automatically retries on rate limits (429) and transient server errors
    (500/502/503/504) or network timeouts, with exponential backoff (honoring
    the Retry-After header when sent). After MAX_RETRIES failed attempts,
    raises a clear GroqRateLimitError/GroqError rather than a raw HTTP error.
    """
    if provider not in PROVIDERS:
        raise GroqError(f"Unknown AI provider \"{provider}\".")
    if not api_key:
        raise GroqError(f"No {provider} API key configured. Add one in the sidebar, or set "
                         f"{PROVIDERS[provider]['key_env']} in Secrets/.env.")

    chat_url = PROVIDERS[provider]["chat_url"]
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": 4096,
        "messages": [
            {"role": "system", "content": system_override or SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }

    last_exception = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(chat_url, headers=headers, json=payload, timeout=60)
        except requests.exceptions.Timeout:
            last_exception = GroqError(f"{provider} didn't respond in time (timeout).")
            if attempt < MAX_RETRIES:
                wait = _backoff_delay(attempt, None)
                if on_retry:
                    on_retry(attempt + 1, MAX_RETRIES + 1, wait, "timeout")
                time.sleep(wait)
                continue
            raise last_exception
        except requests.exceptions.RequestException as e:
            raise GroqError(f"Network error contacting {provider}: {e}")

        if resp.status_code == 200:
            data = resp.json()
            try:
                return data["choices"][0]["message"]["content"]
            except (KeyError, IndexError):
                raise GroqError(f"Unexpected {provider} response shape: {data}")

        if resp.status_code == 401 or resp.status_code == 403:
            raise GroqError(f"{provider} rejected the API key (HTTP {resp.status_code}). Double-check the key in the sidebar or your Secrets/.env.")

        if resp.status_code == 404:
            raise GroqError(f"Model \"{model}\" was not found on {provider} (404) - it may have been deprecated. Try a different model from the dropdown.")

        if resp.status_code in RETRYABLE_STATUS_CODES:
            retry_after = _parse_retry_after(resp)
            reason = "rate limit" if resp.status_code == 429 else f"server error {resp.status_code}"
            if attempt < MAX_RETRIES:
                wait = _backoff_delay(attempt, retry_after)
                if on_retry:
                    on_retry(attempt + 1, MAX_RETRIES + 1, wait, f"{provider} {reason}")
                time.sleep(wait)
                last_exception = None
                continue
            if resp.status_code == 429:
                raise GroqRateLimitError(
                    f"{provider} is still rate-limiting requests after several retries. "
                    "This usually clears within a minute or two - wait a bit and try again, "
                    f"check a fallback provider in the sidebar, or check your plan's limits at {PROVIDERS[provider]['signup_url']}."
                )
            raise GroqError(f"{provider}'s servers are having trouble (HTTP {resp.status_code}) even after retrying. Try again shortly.")

        raise GroqError(f"{provider} API error {resp.status_code}: {resp.text[:300]}")

    if last_exception:
        raise last_exception
    raise GroqError(f"{provider} request failed for an unknown reason after retrying.")


def call_groq(api_key: str, model: str, user_message: str, temperature: float = 0.4,
              system_override: str = None, on_retry=None) -> str:
    """Backward-compatible direct call to Groq specifically."""
    return call_llm("Groq", api_key, model, user_message, temperature, system_override, on_retry)


def call_with_fallback(provider_chain, user_message: str, temperature: float = 0.4,
                        system_override: str = None, on_retry=None, on_fallback=None) -> str:
    """
    Try providers in priority order, falling back to the next one if the
    current one fails (rate limit, missing key, bad key, etc.) - this is
    what actually prevents a single provider's rate limit from blocking the
    user, rather than just retrying the same provider harder.

    provider_chain: list of (provider_name, api_key, model) tuples, in the
                     order they should be tried. Entries with no api_key are
                     skipped (not counted as a real attempt).
    on_fallback: optional callback(from_provider, to_provider, reason) fired
                 right before switching to the next provider, for live UI feedback.
    """
    usable_chain = [(p, k, m) for (p, k, m) in provider_chain if k]
    if not usable_chain:
        raise GroqError("No AI provider is configured - add at least one API key in the sidebar.")

    last_error = None
    for i, (provider_name, api_key, model) in enumerate(usable_chain):
        try:
            return call_llm(provider_name, api_key, model, user_message, temperature, system_override, on_retry)
        except GroqError as e:
            last_error = e
            if i < len(usable_chain) - 1:
                next_provider = usable_chain[i + 1][0]
                if on_fallback:
                    on_fallback(provider_name, next_provider, str(e))
            continue

    raise last_error


def make_call_fn(provider_chain, on_fallback=None):
    """Bundle a provider chain into a single callable that formula_ai.py can
    use without needing to know anything about providers/keys/fallback -
    just call it like call_fn(prompt, temperature=..., system_override=..., on_retry=...)."""
    def _call(user_message, temperature=0.4, system_override=None, on_retry=None):
        return call_with_fallback(provider_chain, user_message, temperature, system_override, on_retry, on_fallback)
    return _call
