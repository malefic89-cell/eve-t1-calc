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
- **Fuzzwork SDE URL** is `latest-sqlite.db.gz` (gzip, not the old .bz2), and
  `industryActivityProducts` uses `typeID`/`productTypeID` columns.
- The categories list must be derived from loaded products, not a raw SDE
  join on blueprint typeID (that returns only "Blueprint").
- Daily volume = 7-day average and is fractional internally; display rounds
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
