# AI Cosmetic Formulation Assistant

A Streamlit R&D tool for cosmetic formulators: build a formula, check ingredient
compatibility, estimate pH/viscosity/stability, check it against a sample
EU/US/India regulatory dataset, calculate batch cost, and get AI-generated
narrative insight via Groq.

## Run it

```bash
cd cosmetic_formulator
python -m venv venv && source venv/bin/activate   # optional but recommended
pip install -r requirements.txt
streamlit run app.py
```

Then open the local URL Streamlit prints (usually http://localhost:8501).

## Groq API key

1. Get a free key at https://console.groq.com
2. Paste it into the sidebar "Groq API key" field (it's a password-type field,
   never written to disk, and only used for the "6. AI Assistant" tab)
3. Pick a model from the dropdown (defaults to `llama-3.3-70b-versatile`)

Everything except the AI Assistant tab (formula building, compatibility rules,
property estimates, regulatory check, cost calculator) works with **no API
key at all** - the Groq key only unlocks the natural-language reasoning layer.

## How it's built

```
app.py                        Streamlit UI, ties everything together
data/ingredients.csv           ~45 common cosmetic ingredients: category,
                                cost/kg, sustainability score, typical pH
                                range, and EU/US/India allowed status + limits
data/incompatibilities.json    Known ingredient-pair interaction rules
utils/groq_client.py           Groq chat-completions wrapper (AI narrative only)
utils/property_estimator.py    Deterministic pH / viscosity / stability heuristics
utils/compatibility_checker.py Rule-based pairwise incompatibility lookup
utils/regulatory_checker.py    Region-by-region allowed/limit checking
utils/cost_calculator.py       Batch costing + same-category substitute finder
```

### Design principle: AI explains, rules decide

All the numbers that actually matter for safety/compliance/cost - regulatory
limits, pH ranges, ingredient prices, incompatibility flags - come from the
local CSV/JSON data and plain Python math, **not** from the LLM. Groq is only
called (Tab 6) to add qualitative, plain-English interpretation on top of
numbers the app already computed, and the system prompt explicitly tells it
not to invent new regulatory figures. This keeps the tool useful without
inheriting an LLM's hallucination risk on the facts that matter most.

## ⚠️ Important limitations

- **`data/ingredients.csv` is a small illustrative sample (~45 ingredients),
  not a complete or continuously updated regulatory database.** A few entries
  (e.g. the EU's 2024/996 retinol and alpha-arbutin limits) were checked
  against public sources at the time of writing; most others are reasonable
  placeholders for demo purposes. Before using this for a real product:
  - Replace/extend the CSV with a verified feed from **EU CosIng**
    (Regulation (EC) 1223/2009 + annexes), **US FDA** cosmetic rules/OTC
    monographs, and **India BIS/CDSCO** standards.
  - Have a qualified regulatory affairs professional sign off.
- The compatibility rules are a curated set of well-known interactions, not
  an exhaustive chemical reactivity database.
- pH/viscosity/stability are directional heuristics - always confirm with a
  calibrated pH meter, viscometer, and real stability protocol (accelerated
  aging, freeze-thaw cycling, centrifuge testing).
- Cost figures are illustrative market-rate approximations, not live supplier
  quotes.

## Deploy to Streamlit Community Cloud (free hosting)

1. Push this folder's contents to a GitHub repo (repo root = `app.py`,
   `requirements.txt`, `data/`, `utils/`):
   ```bash
   cd cosmetic_formulator
   git init
   git add .
   git commit -m "Cosmetic formulation assistant"
   gh repo create cosmetic-formulator --public --source=. --push
   # or: create an empty repo on github.com, then
   # git remote add origin https://github.com/<you>/cosmetic-formulator.git
   # git push -u origin main
   ```
2. Go to **[share.streamlit.io](https://share.streamlit.io)** and sign in with GitHub.
3. Click **"Create app"** → choose your repo/branch → set **Main file path** to `app.py` → **Deploy**.
4. It builds `requirements.txt` automatically and gives you a public URL like
   `https://<something>.streamlit.app`.

**Optional - bake in a shared Groq key** so visitors don't need their own:
in the app's Streamlit Cloud dashboard go to **Settings → Secrets** and add:
```toml
GROQ_API_KEY = "gsk_..."
```
`app.py` already checks `st.secrets` for this and pre-fills the sidebar field
with it if present (visitors can still override it with their own key).
Without this, each visitor just pastes their own key in the sidebar - nothing
is stored server-side either way.

**Note on public hosting**: since the app is public, anyone with the URL can
use it (and, if you set a shared secret key, can spend your Groq quota via
the AI Assistant tab). If you want to restrict access, Community Cloud
supports app-level viewer permissions/private apps on paid tiers - see
[Streamlit's docs](https://docs.streamlit.io/deploy/streamlit-community-cloud).

## Extending it

- **Add ingredients**: append rows to `data/ingredients.csv` following the
  same columns.
- **Add compatibility rules**: append to `pair_rules` in
  `data/incompatibilities.json`.
- **Add a region**: add a new entry to `REGIONS` in
  `utils/regulatory_checker.py` and matching `*_allowed` / `*_max_percent` /
  `*_notes` columns to the CSV.
- **Swap AI providers**: `utils/groq_client.py` is a ~60-line wrapper; the
  same pattern works for any OpenAI-compatible endpoint.
