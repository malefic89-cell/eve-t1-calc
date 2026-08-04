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
| `sso.py` | EVE SSO (PKCE) + mapping character data onto `Settings`. Pure half is unit-tested; token store in `data/sso.json`. |
| `app.py` | FastAPI app, global `State S`, bootstrap thread, recompute pipeline. |
| `static/index.html` | Entire frontend: table, filters, pagination, tooltips, modals. |

`data/` (SDE, cache, settings) and `.venv/` are gitignored — never commit them.

## Character import (EVE SSO) — variant A only so far

Optional by construction: the import **writes into the same `Settings` fields a
manual edit does**, through `put_settings`, so no calculation code knows where a
value came from and "works without a character" needs no separate code path.
Keep it that way — do not branch the maths on whether a character is connected.

- **PKCE, so there is no client secret** anywhere in the project. The client id is
  public by design (stored in `data/sso.json`, overridable via
  `EVE_CALC_SSO_CLIENT_ID`). The **refresh token is a secret**: `data/` is
  gitignored, `SSOState.public()` excludes it, and a test asserts that.
- The token endpoint takes **form encoding only** since 2025 — not JSON, not query
  params. A refresh may return a **different** refresh token (CCP is enabling
  rotation for native apps), so `sso_import` saves it *before* using the access
  token; losing it would strand the connection.
- `character_from_access_token` decodes the JWT **without verifying the
  signature**. Safe only because the token came straight from the SSO token
  endpoint over TLS in the same request and we never accept one from elsewhere.
  If that ever changes, verification against CCP's JWKS becomes mandatory.
- Skills use `active_skill_level`, not `trained_skill_level` (alpha clones have
  trained levels they cannot use), and a skill absent from the payload maps to
  **0** rather than being omitted — otherwise a stale manual value survives.
- Standings resolve the hub owner from the SDE (`station_owner`: Jita 4-4 →
  Caldari Navy 1000035 → Caldari State 500001), so changing the trade hub keeps
  working. Skill IDs come from `type_id_by_name` for the same reason.
- **ESI reports base standings, at full precision** — verified 2026-07-31 by
  importing and reconciling against a real charge. The client showed 7.89 / 9.44;
  ESI returned 7.892620134 / 9.444335456, and those reproduce the 24,881.59 ISK
  broker fee from the Logic Circuit measurement to 0.0013 ISK, where the rounded
  pair misses by 3.83. An effective (Connections-inflated) value would be higher
  and could not reconcile, so no reverse correction is needed. **Importing is
  strictly more accurate than typing standings from the client** — the client
  only shows hundredths.
- Importing also catches settings that drifted from reality: the first import
  corrected Advanced Industry from an assumed 5 to the actual 4, which had been
  overstating every ISK/h by 3.41%. Manual defaults are guesses; treat a large
  diff at import time as the settings having been wrong, not the import.
- Only `esi-skills.read_skills.v1` and `esi-characters.read_standings.v1` are
  requested. Blueprints (variant B, real per-blueprint ME/TE instead of the
  assumed 10/20) are not implemented; that needs a decision on what happens to
  manually-set `blueprint_overrides`.

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
- **A job cannot be installed past 30 days**, so runs per job are capped at
  `ceil(30 days / modified time per run)` (`calc.max_runs_per_job`) — it depends
  on TE, skills and structure, not on the blueprint. The accepted maximum lands
  one run *over* 30 days (424 × 6120 s = 30.03 days installs), hence ceil.
  Items whose single run already exceeds 30 days are exempt, so the floor of 1
  is a real allowance. A BPO itself has unlimited runs; only the job is capped.
  Rows past the cap get the `>30d` badge and are **not** hidden by the BPC/susp
  filter — the cause is the user's own Runs setting, not bad data.
  `Settings.runs` allows up to 1e6 as a sanity bound only; at Runs=1000, 694 of
  1845 products exceed the in-game cap.
  **Verified in game 2026-07-30** (regression test exists): Damage Control I at
  an NPC station, TE 20 / Industry 5 / Advanced Industry 5 — the client showed
  8m10s per run and capped Runs at **5295**. That single number settled both
  open questions:
  - the cap is computed from the **exact** per-run time (489.6 s), *not* the
    whole seconds displayed — `ceil(30d/490)` would give 5290, five short. Pass
    `t_run`, never `round(t_run)`; `t_run_s` is for display only. An earlier
    version used the rounded value on the theory that the badge should be
    checkable by dividing the displayed seconds — that was wrong, don't restore it.
  - it is `ceil`, not `floor`: 5295 × 489.6 = 30.005 days is accepted while 5294
    still fits inside 30 days, so the game grants the run that crosses the line.
  Second in-game data point, Data Analyzer I at an NPC station: **2648**, which
  confirms `ceil` again on a different base time (1800 s). It does *not* re-test
  the exact-time basis — at an NPC station both bases predict 2648.
  **The exact-time basis is settled, verified 2026-08-05** (regression test
  exists): the same Damage Control I in a structure with −20% time, TE 20 /
  Industry 5 / Advanced Industry 5 — 6m32s per run and a cap of **6618**. Exact
  (391.68 s) predicts 6618; the displayed 392 s predicts 6613. So the basis no
  longer rests on one reading. Note this is not the first measurement repeated:
  the structure bonus moves the per-run time to a value whose fraction rounds the
  *other* way (489.6 up, 391.68 down), so surviving the first reading did not
  make a hypothesis free to survive this one.
  Third data point, Antimatter Charge S at an NPC station, **verified 2026-08-05**
  (regression test exists): 4m36s per run and a cap of **9392**. Its value is not
  the cap — 276 s is whole, so both bases agree — but that it is the only reading
  at **non-maximal skills** (Industry 2, Advanced Industry untrained). Every other
  reading is Industry 5 / Advanced Industry 5, where the 4%/level and 3%/level
  terms enter together and cannot be told apart; this one separates them.
  This is the same display-vs-precision split as the fee percentages above.
  Beware when reconciling old readings: **the skills in `Settings` are not
  necessarily the skills of the character holding the blueprint.** A reading that
  seems to contradict the settings may just be the other character — check the
  displayed per-run time against each skill set before concluding the code is wrong.
- **Broker fee floor is 1%** on NPC stations (changed from 0.5% at some point);
  sales tax base 7.5%, −11%/Accounting level. The rate coefficients live in
  `calc.BROKER_FEE_*`; `GET /api/fees` previews them for the settings modal so
  the page never carries a second copy of the formula.
- **Standings affect the broker fee and nothing else here** (verified against
  CCP support + EVE Uni wiki, 2026-07): −0.03%/point faction, −0.02%/point corp,
  and negative standing *raises* the fee. In game they also cut the NPC
  reprocessing tax (5% → 0% at 6.67 standing), which this app does not model, and
  they have **no** effect on industry job cost — so `calc.job_cost` is right to
  ignore them. Don't "fix" that by adding a standing term.
- **The client rounds every percentage it displays to hundredths but charges the
  full-precision value** — calibrate against ISK amounts, never against a shown
  `%`. Verified 2026-07-30 on a Logic Circuit sell order: price 2,316,000 ISK,
  broker fee charged 24,881.59 ISK = 1.0743346%, while the window said "1.07%"
  (taking that at face value misses by 100 ISK). Sales tax behaves the same:
  displayed 3.37, charged 3.375.
  **Standings are also carried beyond the two decimals shown**, so entering them
  from the client leaves a residual: the rate can be off by at most
  0.03%×0.005 + 0.02%×0.005 = 0.00025 points, i.e. 2.5 ISK per million of order
  value. Confirmed rather than assumed — 7.8925/9.4445 both display as
  7.89/9.44 and reproduce the charged 24,881.59 exactly. Four regression tests
  in `TestFees` pin this; don't "tighten" the rounded-standings tolerance to an
  exact match. The real values later arrived from an ESI import —
  7.892620134 / 9.444335456, inside the interval predicted here — and they hit
  the charge to 0.0013 ISK. So an imported character is measurably more accurate
  than typing standings off the client; see the character-import section.
- **The broker fee uses UNMODIFIED (base) standings.** `Connections`,
  `Diplomacy` and `Social` raise *effective* standing but do not touch the fee —
  EVE Uni's Tax page states it outright ("the broker's fee is based on unmodified
  standings and does not take any standing skills into account"), and only
  `Broker Relations` reduces it. So `broker_fee_rate` takes no skill argument
  beyond Broker Relations, and that is correct — **do not add a Connections
  term**. The settings fields expect base standings; the in-game character sheet
  shows the *effective* value, which is higher, so pasting it understates the
  fee. Both field tooltips and a visible line under the Standings grid say so.
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
- **Rigs are re-categorised out of `Module` into `Rigs (Small|Medium|Large|Capital)`**
  (`SDE._category_of`), because Module held 812 T1 products in 150 groups — 326
  of them rigs — and the group filter was unusable. Module is now 486.
  Requires **both** a `Rig *` group and the `rigSize` attribute (1547): ships
  carry `rigSize` too (399 of them, saying which rig size they accept), so
  keying on the attribute alone files every frigate under "Rigs (Small)". A rig
  missing the attribute stays in Module rather than disappearing.
  `/api/categories` sorts via `_category_sort_key` so the four sit together
  under "Rigs" in size order — plain alphabetical gives Capital, Large, Medium,
  Small. Group names are left exactly as the SDE has them (`Rig Armor`, …).
  Beware: `"rig" in group_name.lower()` also matches **F-rig-ate** — match on
  the `"Rig "` prefix.
- **`ISK/h` is the line ceiling, `ISK/h real` the achievable rate.** The old
  column divides per-job profit by job time only, so on an illiquid item it is
  optimistic by orders of magnitude — the order scenarios take the patient price
  without charging any waiting time (measured: 636M vs 495k ISK/h on an item
  whose output needs 117 days to sell). `calc.market_limited_iph` takes
  `min(units/job_hours, daily_volume/24)` and multiplies by profit per unit.
  Do it as a **rate**, never `min()` of two ISK/h values: on a loss-making item
  min() would rank the slower operation as worse, when producing slower loses
  less per hour. Applied to all four scenarios — book depth limits a single job,
  daily volume limits repeating it. Still optimistic: daily volume is the whole
  market's turnover, competitors included, and there is no competition model.
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
- **Tooltip coverage is expected, not optional.** Every table column, every
  settings field and every section heading (`<h3>`) carries a `?` where it adds
  value. Section tooltips explain what the group as a whole does and the
  non-obvious cross-cutting facts (e.g. no skill reduces materials; structure
  bonuses multiply rather than add); field tooltips cover that one parameter.
  When a section has a single field, put the `?` on the heading only — two
  identical icons side by side is noise. Prefer `data-tip` in static markup;
  JS-built blocks (the detail modal) are fine since they render once per open.
  What must **not** happen is a `?` inside a block that re-renders while the
  user may be hovering it — the fee preview rebuilds on every keystroke, so its
  `?` is static markup between two JS-filled spans, or `mouseout` never fires
  and the tooltip stays stranded. Keep `"` out of tip text either way: the
  attribute is double-quoted, so use «guillemets».
- After changing app.py/calc.py, restart the server (it holds modules in
  memory); static/index.html needs no restart, just a browser refresh.
- PowerShell here-strings: keep double quotes out of `git commit -m @'...'@`
  bodies — PS 5.1 mangles them when passing to git.
- **This console is cp1251.** Python printing any character outside it — `→`,
  `×`, `—`, box drawing — dies with
  `UnicodeEncodeError: 'charmap' codec can't encode character '→'`, and the
  traceback replaces the output you wanted. Export `PYTHONIOENCODING=utf-8`
  before every Python one-liner that prints scenario labels, tooltip text or
  anything from `index.html`. Cheapest to just always set it.
- **Writing files from Python: `encoding="utf-8", newline=""`.** Text mode
  translates `\n` to `\r\n` on this platform, which silently corrupts anything
  byte-sensitive — a generated patch gets CRLF line endings and `git apply`
  then reports "patch does not apply" with context that looks identical.
- Splitting one file's changes across commits (two features edited in the same
  region) is easier by temporarily reverting the later feature's lines on disk,
  committing, then restoring from a backup copy, than by hand-editing hunks out
  of a patch. Adjacent lines merge into one hunk at any `-U` setting, so
  `git apply --cached` with selected hunks cannot separate them.
