"""
Thin wrapper around the Groq API (OpenAI-compatible /chat/completions endpoint).

The AI is only ever used to EXPLAIN and SUGGEST on top of numbers that the app
has already computed deterministically (pH, cost, regulatory limits, known
incompatibility rules). It is never used as the source of truth for
regulatory or safety facts - that keeps hallucination risk out of the parts
of the tool that actually matter.
"""

import requests

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# A small set of currently-available Groq-hosted models.
# Users can add/edit this list as Groq's lineup changes.
AVAILABLE_MODELS = [
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "mixtral-8x7b-32768",
    "gemma2-9b-it",
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


class GroqError(Exception):
    pass


def call_groq(api_key: str, model: str, user_message: str, temperature: float = 0.4) -> str:
    """Send a single-turn chat completion request to Groq and return the text reply."""
    if not api_key:
        raise GroqError("No Groq API key provided. Enter it in the sidebar to enable AI insights.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "temperature": temperature,
        "max_tokens": 1200,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    }

    try:
        resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=60)
    except requests.exceptions.RequestException as e:
        raise GroqError(f"Network error contacting Groq: {e}")

    if resp.status_code == 401:
        raise GroqError("Groq rejected the API key (401 Unauthorized). Double-check the key.")
    if resp.status_code == 429:
        raise GroqError("Groq rate limit hit (429). Wait a moment and try again.")
    if resp.status_code >= 400:
        raise GroqError(f"Groq API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError):
        raise GroqError(f"Unexpected Groq response shape: {data}")
