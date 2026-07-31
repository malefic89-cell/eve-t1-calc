# EVE T1 Manufacturing Profit Calculator

Local web app that finds profitable T1 manufacturing opportunities using
Jita 4-4 prices. FastAPI backend + single-page frontend.

## Run

```powershell
cd eve-t1-calc
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python app.py
```

Open http://127.0.0.1:8000

**First run** downloads the Fuzzwork SDE (~120 MB compressed, ~600 MB on disk)
and fetches all Jita market orders — expect a few minutes. Progress is shown
in the UI. Subsequent starts reuse the SDE and disk-cached ESI responses
(orders 15 min, history 24 h).

Click **Fetch volumes** to pull trade history for daily-volume data
(one ESI call per item, takes a while; cached 24 h).

## What it computes

For every manufacturable T1 item, a 2×2 matrix of scenarios:

|                      | Sell instantly (to buy orders) | Own sell order |
|----------------------|-------------------------------|----------------|
| **Buy instantly** (from sell orders, volume-weighted) | inst→inst | inst→order |
| **Own buy orders** (at best bid, +broker fee)         | ord→inst  | ord→order  |

Each cell: profit/run, margin %, ISK/hour (with TE + skill + structure time
bonuses). Job cost = EIV × system cost index × (1 + structure tax) + 4% SCC.
Material quantities use the exact EVE rounding: `max(runs, ceil(round(base ×
runs × (1−ME%) × (1−struct%) × (1−rig%), 2)))`.

Broker fee: `3% − 0.3%×BrokerRelations − 0.03%×faction − 0.02%×corp`, floor 1%.
Standings are the **unmodified** ones — Connections and Diplomacy do not reduce
the fee. Sales tax: `7.5% × (1 − 0.11×Accounting)`.

## Settings

⚙ Settings in the UI: skills, standings, blueprint ME/TE, structure bonuses
and tax, manufacturing system (searchable). Persisted to `data/settings.json`.

## Importing a character (optional)

Skills and standings can be pulled from the game instead of typed. This is
entirely optional — with no character connected everything stays manual and works
exactly as before.

One-time setup, because an EVE application can only be registered by its owner:

1. Go to <https://developers.eveonline.com/applications> and create an application.
2. Connection type: **Authentication & API Access**.
3. Callback URL: `http://127.0.0.1:8000/api/sso/callback` — must match character
   for character. Override it with `EVE_CALC_SSO_REDIRECT` if you serve the app
   from somewhere else; a VPS needs its own application with its own URL.
4. Scopes: `esi-skills.read_skills.v1` and `esi-characters.read_standings.v1`.
   Nothing else is requested.
5. Copy the **Client ID** into ⚙ Settings → Character. There is no client secret
   to copy: the app uses the PKCE flow, which does not have one.

Then press *Connect character*, authorise in the tab that opens, and press
*Import*. The import writes into the ordinary settings fields, so you can still
edit them afterwards, and it reports what each value changed from.

The refresh token lands in `data/sso.json`, which is gitignored. Treat it as a
password: it grants read access to those two scopes until revoked at
<https://community.eveonline.com/support/third-party-applications/>.
`EVE_CALC_SSO_CLIENT_ID` overrides the stored client id if you prefer the
environment.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

## Structure

- `calc.py` — pure calculation core (fully unit-tested)
- `sde.py` — SDE download + SQLite queries (manufacturing = activityID 1)
- `esi.py` — ESI client: disk cache, pagination, error-limit handling, User-Agent
- `config.py` — persisted character settings
- `app.py` — FastAPI endpoints + background bootstrap/refresh threads
- `static/index.html` — UI

## Extending (planned)

- **T2 / invention**: add activityID 8 queries in `sde.py`; invention chance +
  datacore cost feed into `calc.py` (fee/pricing helpers are activity-agnostic).
- **Reactions**: activityID 11, `reaction` cost index from the same
  `/industry/systems/` payload already cached.
- **ESI OAuth skill import**: extend `ESIClient` with a token provider and
  write imported skill levels into `config.Settings`.
