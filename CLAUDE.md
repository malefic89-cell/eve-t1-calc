# eve-t1-calc

Local web app: T1 manufacturing profit calculator for EVE Online.
FastAPI backend + single-page frontend (`static/index.html`, vanilla JS, no build step).

## Run / test

```powershell
.\.venv\Scripts\python app.py          # server on http://127.0.0.1:8000
.\.venv\Scripts\python -m pytest -q    # unit tests (calc.py math)
```

- First start downloads the SDE (~140 MB gz) and fetches ~417 pages of Jita
  orders; poll `GET /api/status` until `"status":"ready"`.
- Volume history auto-fetches after bootstrap (1738 ESI calls, cached 24 h).
- The server is often already running from a previous session — check
  `/api/status` before starting, kill python processes under this path to restart.

## Architecture

| File | Role |
|---|---|
| `calc.py` | Pure math (materials, job cost, fees, order-book pricing). Fully unit-tested; keep it side-effect free. |
| `sde.py` | SDE download + SQLite queries (products, materials, systems). |
| `esi.py` | ESI client, on-disk cache in `data/cache/`, error-limit handling. |
| `config.py` | `Settings` dataclass persisted to `data/settings.json` (skills, standings, structure, runs, per-blueprint overrides). |
| `app.py` | FastAPI app, global `State S`, bootstrap thread, recompute pipeline. |
| `static/index.html` | Entire frontend: table, filters, pagination, tooltips, modals. |

`data/` (SDE, cache, settings) and `.venv/` are gitignored — never commit them.

## Privacy rule (project-wide, applies to every change)

**No personal data in tracked files — ever.** Not the user's email, real name,
EVE character or corp names, GitHub handle, domain, IP addresses, tokens or
API keys. This covers code, User-Agent strings, comments, docs, deploy configs,
test fixtures and commit messages alike.

- The ESI contact CCP asks for comes from the `EVE_CALC_CONTACT` env var
  (`esi.ESI_CONTACT`), set on the machine — see `deploy/eve-t1-calc.service`.
  Never hardcode a default value for it.
- Docs use placeholders: `<ваш-аккаунт>`, `calc.example.com`, `you@example.com`.
- `data/` (settings, ESI cache) is gitignored and holds the real values — that
  is the only place they belong.
- Before committing, check: `git grep -nIE "@[a-z0-9.-]+\.(com|net|org|ru)"`.
  `tests/test_esi.py::test_user_agent_carries_no_personal_data` guards the UA.

Personal data that already reached git history (the commit author email) is a
separate problem — rewriting history needs a force-push, so ask first.

## Hard-won facts (do not re-derive)

- **Job cost formula** (verified in-game to the ISK, regression test exists):
  `EIV × SCI × (1 − structure job cost bonus) + EIV × facility_tax + EIV × 4% SCC`.
  Facility tax applies to **full EIV**, not to EIV×SCI.
- **Material rounding is per job**: `max(runs, ceil(round(qty, 2)))`, bonuses
  multiplicative — never simplify to per-run × runs.
- **Broker fee floor is 1%** on NPC stations (changed from 0.5% at some point);
  sales tax base 7.5%, −11%/Accounting level.
- **ESI history returns 400** for type_ids that never trade — `esi._get`
  treats all 4xx as `ESIError`, and the history fetch pool must survive
  per-item failures. Don't "clean up" that error handling.
- **`ESIError.status` separates "no data" from "no answer"**: 4xx carries the
  status and `history()` caches `[]` for it; a network error or exhausted
  5xx/420 retries carries `status=None` and must **propagate** — caching `[]`
  there would zero the item's volume and drop its p5/p95 for the whole 24 h
  TTL. `_record_history(tid, None, ...)` likewise keeps the cached stats
  instead of overwriting them; only `[]` (a real "never traded") is recorded.
- **`PUT /api/settings` merges** into the current settings. A field absent
  from the payload keeps its value — building a fresh `Settings` from the
  payload alone reset omitted fields to dataclass defaults and wiped
  `blueprint_overrides` whenever the settings modal saved a stale snapshot.
  Keep the frontend sending only the fields its modal owns.
- **Fuzzwork SDE URL** is `latest-sqlite.db.gz` (gzip, not the old .bz2), and
  `industryActivityProducts` uses `typeID`/`productTypeID` columns.
- The categories list must be derived from loaded products, not a raw SDE
  join on blueprint typeID (that returns only "Blueprint").
- Daily volume is per **calendar** day, not per traded day: `calc.avg_daily_volume`
  sums the 7-day window ending **yesterday UTC** and divides by 7, because ESI
  history omits days with no trades. Dividing by the entry count overstated
  illiquid items enormously (measured: 845 of 2195 types, up to 214k/day vs a
  real 1.5k/day) and defeated both the Vol/day filter and the `low liq` badge.
  The window ends yesterday since ESI publishes a day's aggregate only after it
  closes; use UTC — the local date can be a day ahead. `today` is a parameter
  so `calc` stays deterministic. Volume is fractional internally; display rounds
  to whole items. Money cells use compact `fmtC` (12.3k/4.56M) with the exact
  value in `title` — don't apply `fmtC`'s decimals to item counts.

## Workflow conventions (user preferences)

- The user tests changes in the browser and in-game, then says
  "закоммить"/"коммить" — commit and push only on that request.
- Verify formula changes against in-game numbers when the user provides them;
  add a regression test for each verified value.
- UI language is English; help tooltips ("?" icons) are in Russian — keep new
  UI elements consistent with that split, and add a tooltip to anything new.
- After changing app.py/calc.py, restart the server (it holds modules in
  memory); static/index.html needs no restart, just a browser refresh.
- PowerShell here-strings: keep double quotes out of `git commit -m @'...'@`
  bodies — PS 5.1 mangles them when passing to git.
