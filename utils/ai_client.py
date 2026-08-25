"""
Gemini-only AI client with dual-key support and fully automatic fallback.

Design: the app never asks the user to pick a model. It always tries models
in a fixed priority order (starting with gemini-3.5-flash-lite, confirmed to
work well) and automatically advances - within a key, across models; across
keys when a key itself is invalid or exhausted - without any user action.

Fallback triggers on THREE kinds of failure, not just HTTP-level ones:
  1. Transient errors (429 rate limit, 500/502/503/504, timeouts) - retried
     with backoff on the SAME (key, model) a few times first.
  2. An invalid/rejected key (401/403) - skips straight to the next key,
     since retrying other models with the same bad key would fail identically.
  3. "Other generation issues" - the model responded successfully (200 OK)
     but produced unusable content (e.g. not valid JSON for a formula
     request). This is checked via an optional validate_response callback
     supplied by the caller; a failure here advances to the next model in
     the chain, same as a hard error would - this is what lets a
     model-compatibility problem (not just a network/rate-limit problem)
     trigger an automatic switch to another model.

The AI is only ever used to EXPLAIN and SUGGEST on top of numbers the app
has already computed deterministically (pH, cost, regulatory limits, known
incompatibility rules) or to draft/refine a formula that is independently
re-validated afterward - it is never treated as the source of truth for
regulatory or safety facts.
"""

import random
import time

import requests

GEMINI_CHAT_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"

# Fixed automatic priority order - no user selection. gemini-3.5-flash-lite
# is first because it's confirmed to work well; the rest are automatic
# fallbacks if it's unavailable, incompatible, or rate-limited.
#
# ⚠️ MODEL IDS GO STALE FAST. Google retired gemini-2.0-flash on 2026-03-31
# (about 8 months after release) and had shipped FOUR more Flash generations
# by August 2026. If every model here starts 404ing, check the current
# lineup at https://ai.google.dev/gemini-api/docs/models and update this
# list - nothing else needs to change, every call references it dynamically.
GEMINI_MODELS = [
    "gemini-3.5-flash-lite",  # default - confirmed working well
    "gemini-3.6-flash",       # current GA/stable flagship Flash
    "gemini-3.5-flash",       # previous flagship Flash generation
    "gemini-3.7-flash",       # newest, most capable
]

GEMINI_SIGNUP_URL = "https://aistudio.google.com/apikey"
GEMINI_FREE_TIER_NOTE = (
    "Genuinely free tier, no credit card required (as of this writing: ~15 requests/min, "
    "~1,500/day on Flash models - varies by model and changes over time, check "
    "aistudio.google.com for current limits). Google's terms state free-tier prompts may "
    "be used to improve their products - keep that in mind for sensitive formulas."
)

# Models known to autonomously use live web search as part of generating a
# response (no special "tools" parameter needed). Currently empty: the
# previous implementation relied on Groq's compound models specifically,
# which have been removed. Gemini's OpenAI-compatibility layer's support for
# search grounding wasn't verified before this cutover, so the worldwide
# web-search feature (utils/formula_ai.py's search_worldwide_ingredients)
# is intentionally left dormant rather than risk ungrounded "search
# results" from a model that isn't actually searching. The app already
# degrades gracefully to the static ingredient database when this set is
# empty. Re-enable by adding a confirmed-search-capable Gemini model ID here.
WEB_SEARCH_CAPABLE_MODELS = set()

# Env var names for the two optional keys.
GEMINI_KEY_ENV_VARS = ["GEMINI_API_KEY", "GEMINI_API_KEY_2"]

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

# Retry policy for transient errors on a single (key, model) attempt, before
# the outer chain moves to the next one. Rate limits (429) get a SMALLER
# retry budget than genuine server hiccups (500/502/503/504) or timeouts,
# since a rate limit rarely clears within seconds - it's faster and more
# effective to advance to the next model/key than to keep waiting on the
# same one.
MAX_RETRIES = 4
MAX_RATE_LIMIT_RETRIES = 1
BASE_DELAY_SECONDS = 1.5
MAX_DELAY_SECONDS = 20
RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}
# Extra same-model retries specifically for "responded but content was
# unusable" (e.g. bad JSON) - kept small since this isn't a network issue,
# just enough to absorb an occasional one-off formatting slip before the
# outer chain gives up on this model and tries the next one.
MAX_CONTENT_RETRIES = 1


class AIError(Exception):
    """Raised for errors the caller should show to the user as-is (already friendly)."""
    pass


class AIRateLimitError(AIError):
    """Raised when a key/model is still rate-limited after all retries."""
    pass


class AIAuthError(AIError):
    """Raised for 401/403 - an invalid/rejected API key. Distinguishing this
    lets the fallback chain skip straight to the next KEY (retrying other
    models with the same bad key would just fail the same way every time)."""
    pass


class AIModelNotFoundError(AIError):
    """Raised for 404 - the model ID doesn't exist / has been deprecated.
    Lets the fallback chain try a sibling model with the SAME key next."""
    pass


class AIGenerationError(AIError):
    """Raised when a model responds successfully (200 OK) but produces
    content that fails the caller's validity check (e.g. unparseable JSON
    for a formula request) - even after a couple of same-model retries.
    Treated like any other AIError by the fallback chain: try the next
    model next, since this usually means that specific model isn't a good
    fit for the task, not that something is broken."""
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


def call_llm(api_key: str, model: str, user_message: str, temperature: float = 0.4,
             system_override: str = None, on_retry=None, key_label: str = "Gemini",
             validate_response=None) -> str:
    """Send a chat completion request to Gemini and return the text reply.

    Retries on rate limits (429), transient server errors (500/502/503/504),
    timeouts, and (a small number of times) on content that fails
    validate_response - all with exponential backoff, honoring Retry-After
    when sent. Raises a specific AIError subclass on final failure so the
    caller's fallback chain (see call_gemini_with_fallback) knows whether to
    try a sibling model (same key) or move to the next key entirely.

    validate_response, if given, is called as validate_response(content) ->
    (is_valid: bool, reason: str) after a successful HTTP response - lets
    the caller reject a 200 OK response whose content isn't actually usable.
    """
    if not api_key:
        raise AIError(f"No {key_label} API key configured.")

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
    content_retry_count = 0

    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = requests.post(GEMINI_CHAT_URL, headers=headers, json=payload, timeout=60)
        except requests.exceptions.Timeout:
            last_exception = AIError(f"{key_label} ({model}) didn't respond in time (timeout).")
            if attempt < MAX_RETRIES:
                wait = _backoff_delay(attempt, None)
                if on_retry:
                    on_retry(attempt + 1, MAX_RETRIES + 1, wait, "timeout")
                time.sleep(wait)
                continue
            raise last_exception
        except requests.exceptions.RequestException as e:
            raise AIError(f"Network error contacting {key_label}: {e}")

        if resp.status_code == 200:
            data = resp.json()
            try:
                content = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError):
                raise AIError(f"Unexpected {key_label} ({model}) response shape: {data}")

            if validate_response:
                is_valid, reason = validate_response(content)
                if not is_valid:
                    if content_retry_count < MAX_CONTENT_RETRIES:
                        content_retry_count += 1
                        if on_retry:
                            on_retry(content_retry_count, MAX_CONTENT_RETRIES + 1, 0,
                                      f"{key_label} ({model}) returned unusable content ({reason})")
                        continue
                    raise AIGenerationError(f"{key_label} ({model}) returned unusable content even after retrying: {reason}")
            return content

        if resp.status_code == 401 or resp.status_code == 403:
            raise AIAuthError(f"{key_label} rejected the API key (HTTP {resp.status_code}). Double-check the key in the sidebar or your Secrets/.env.")

        if resp.status_code == 404:
            raise AIModelNotFoundError(f"Model \"{model}\" was not found on {key_label} (404) - it may have been deprecated. Trying an alternate model automatically.")

        if resp.status_code in RETRYABLE_STATUS_CODES:
            retry_after = _parse_retry_after(resp)
            is_rate_limit = resp.status_code == 429
            reason = "rate limit" if is_rate_limit else f"server error {resp.status_code}"
            max_for_this_error = MAX_RATE_LIMIT_RETRIES if is_rate_limit else MAX_RETRIES
            if attempt < max_for_this_error:
                wait = _backoff_delay(attempt, retry_after)
                if on_retry:
                    on_retry(attempt + 1, max_for_this_error + 1, wait, f"{key_label} ({model}) {reason}")
                time.sleep(wait)
                last_exception = None
                continue
            if is_rate_limit:
                raise AIRateLimitError(
                    f"{key_label} ({model}) is still rate-limiting requests after retrying. "
                    f"Trying an alternate model/key automatically if configured."
                )
            raise AIError(f"{key_label} ({model})'s servers are having trouble (HTTP {resp.status_code}) even after retrying.")

        raise AIError(f"{key_label} ({model}) API error {resp.status_code}: {resp.text[:300]}")

    if last_exception:
        raise last_exception
    raise AIError(f"{key_label} ({model}) request failed for an unknown reason after retrying.")


def call_gemini_with_fallback(api_keys, user_message: str, temperature: float = 0.4,
                               system_override: str = None, on_retry=None, on_fallback=None,
                               validate_response=None) -> str:
    """
    Try each configured key in order, and within each key, try GEMINI_MODELS
    in priority order (gemini-3.5-flash-lite first) - fully automatic, no
    user model selection. Advances to the next model on ANY failure
    (transient error, deprecated model, or unusable content per
    validate_response); advances straight to the next KEY on an auth error,
    since sibling models would fail identically with the same bad key.

    api_keys: list of api key strings, in priority order. Empty/None entries
              are skipped (not counted as a real attempt).
    on_fallback: optional callback(from_label, to_label, reason) fired right
                 before switching to the next attempt, for live UI feedback.
    """
    usable_keys = [k for k in api_keys if k]
    if not usable_keys:
        raise AIError("No Gemini API key is configured - add at least one key in the sidebar.")

    multi_key = len(usable_keys) > 1
    attempts = []
    for idx, api_key in enumerate(usable_keys, start=1):
        key_label = f"Gemini Key {idx}" if multi_key else "Gemini"
        for model in GEMINI_MODELS:
            attempts.append((key_label, api_key, model))

    last_error = None
    skip_key_label = None

    for i, (key_label, api_key, model) in enumerate(attempts):
        if key_label == skip_key_label:
            continue
        label = f"{key_label} ({model})"
        try:
            return call_llm(api_key, model, user_message, temperature, system_override,
                             on_retry, key_label=key_label, validate_response=validate_response)
        except AIAuthError as e:
            last_error = e
            skip_key_label = key_label
            continue
        except AIError as e:
            last_error = e
            next_label = next(
                (f"{kl} ({m})" for kl, _, m in attempts[i + 1:] if kl != skip_key_label),
                None,
            )
            if on_fallback and next_label:
                on_fallback(label, next_label, str(e))
            continue

    raise last_error


def make_call_fn(api_keys, on_fallback=None):
    """Bundle the configured keys into a single callable that formula_ai.py
    can use without needing to know anything about keys/models/fallback -
    just call it like call_fn(prompt, temperature=..., system_override=..., on_retry=..., validate_response=...)."""
    def _call(user_message, temperature=0.4, system_override=None, on_retry=None, validate_response=None):
        return call_gemini_with_fallback(api_keys, user_message, temperature, system_override,
                                          on_retry, on_fallback, validate_response)
    return _call


# --------------------------------------------------------------------------
# Native API search grounding (for Worldwide ingredient research)
# --------------------------------------------------------------------------
# The OpenAI-compatible endpoint used everywhere else in this module does
# NOT support Google Search grounding for chat completions as of this
# writing - Google's own docs list "tools" grounding via extra_body as
# available only for the image-generation endpoint. Genuine live web search
# requires Gemini's NATIVE generateContent API instead, which has a
# different request/response shape (contents/parts, not
# messages/choices) - hence this separate, simpler function rather than a
# variant of call_llm. Search grounding is available on Gemini 3+ models,
# which is all of GEMINI_MODELS, so no separate "search-capable" allowlist
# is needed here.
GEMINI_NATIVE_API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
SEARCH_MODEL = GEMINI_MODELS[0]


def call_gemini_search(api_key: str, user_message: str, system_override: str = None,
                        model: str = None, key_label: str = "Gemini") -> str:
    """Single attempt at a Google Search-grounded generation via Gemini's
    native API. Raises an AIError subclass on failure (same exception types
    as call_llm, so callers can handle both uniformly) - no automatic
    within-call retry here since this is used for a best-effort enhancement
    step (see search_gemini_with_fallback below for the key-level fallback
    that wraps this)."""
    model = model or SEARCH_MODEL
    url = f"{GEMINI_NATIVE_API_BASE}/{model}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_message}]}],
        "tools": [{"google_search": {}}],
        "generationConfig": {"temperature": 0.4, "maxOutputTokens": 4096},
    }
    if system_override:
        payload["systemInstruction"] = {"parts": [{"text": system_override}]}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
    except requests.exceptions.Timeout:
        raise AIError(f"{key_label} search didn't respond in time (timeout).")
    except requests.exceptions.RequestException as e:
        raise AIError(f"Network error contacting {key_label} for search: {e}")

    if resp.status_code == 200:
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise AIError(f"{key_label} search returned no candidates: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        if not text:
            raise AIError(f"{key_label} search returned an empty response.")
        return text

    if resp.status_code in (401, 403):
        raise AIAuthError(f"{key_label} rejected the API key for search (HTTP {resp.status_code}).")
    if resp.status_code == 404:
        raise AIModelNotFoundError(f"Search model \"{model}\" was not found on {key_label} (404).")
    if resp.status_code == 429:
        raise AIRateLimitError(f"{key_label} search is rate-limited (429).")
    raise AIError(f"{key_label} search error {resp.status_code}: {resp.text[:300]}")


def search_gemini_with_fallback(api_keys, user_message: str, system_override: str = None, on_fallback=None) -> str:
    """Try each configured key in turn for a search-grounded request (using
    the fixed SEARCH_MODEL for all attempts, since search grounding is a
    property of the API call, not something that varies meaningfully across
    Gemini 3.x models the way plain generation does). This is a best-effort
    enhancement, not the core formula generation path - callers should catch
    AIError and gracefully continue without search results on failure."""
    usable_keys = [k for k in api_keys if k]
    if not usable_keys:
        raise AIError("No Gemini API key is configured.")

    multi_key = len(usable_keys) > 1
    last_error = None
    for idx, api_key in enumerate(usable_keys, start=1):
        key_label = f"Gemini Key {idx}" if multi_key else "Gemini"
        try:
            return call_gemini_search(api_key, user_message, system_override, key_label=key_label)
        except AIError as e:
            last_error = e
            if on_fallback and idx < len(usable_keys):
                on_fallback(key_label, f"Gemini Key {idx + 1}", str(e))
            continue
    raise last_error
