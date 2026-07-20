# Current App Recap

## Restart

- Load `C:\Users\JackOswald\OneDrive - ISOThrive Inc\codex-shared\AGENTS.md` first.
- Use this compact recap on `Let's go`; consult `APP_RECAP.md` only for deeper history.
- Treat `prior/SESSION_CONTEXT.md` and `prior/CHECKPOINT_*.md` as historical notes only.

## Current Goal

Maintain a trustworthy TBM 960 mission-planning brief whose weather, route, climb/descent, fuel, alternate, and hazard calculations remain auditable and conservative.

## Follow-Up Review Completion — July 20, 2026

- Release `v2026.07.20.11` completes the independently confirmed Claude follow-up punch list, treats the operator landing minimum as a destination-arrival floor, labels predicted touchdown fuel as gross `FOB at Landing`, and makes the route-profile legend an in-place hazard-layer control.
- Python 3.14 verification passed: Ruff clean, all active modules compiled, the PIM snapshot exactly matched a fresh 28-table PDF parse, 151 tests passed, and Streamlit AppTest reported zero exceptions/errors/warnings.
- The five new P1 findings are closed: full PIREP intensity scoring, fail-closed snapshot validation, undecodable-ceiling conservatism, final-leg destination ring fuel, and visible rejection of unresolved per-leg alternates.
- Confirmed P2/P3 work includes great-circle track evolution, raw CWA/vertical-visibility units, FL100 alternate performance, wind-temperature distance caps, calm fallback policy, current/preview NASR retention, workflow CI dispatch, exact numeric multi-leg timing, clearer source/provenance labels, and UI trust/readability improvements.
- Production and development dependency locks are separated, NOAA heterogeneous results are shape-checked, and duplicated interpolation/integration paths now use shared helpers.
- The main-deployment version gate compares the complete GitHub push range rather than assuming a single-commit push.
- Every release boundary now evicts all cached repo modules before application imports, preventing Streamlit in-process deployments from binding stale exports; targeted compatibility guards remain as defense in depth.
- The hosted app is private at `https://codex-weather-brief.streamlit.app`; unauthenticated probes correctly redirect to Streamlit sign-in.
- Pip is `26.1.2` in both the local project environment (Python 3.10) and the isolated release-verification environment (Python 3.13.7).
- Screenshot inbox was empty at close-out.

## UI Regression Release — July 20, 2026

- Release `v2026.07.20.12` introduced the UI fixes at commit `6fc55d9`; close-out build `v2026.07.20.13` carries the same app code plus finalized session recaps.
- New missions default to a rounded ETD at least 15 minutes ahead, and the past-time warning allows the active five-minute selection window instead of flagging the current minute as stale.
- Route-profile hazard category names are anchored at each band's upper-left, shrink only when needed, and use collision-aware lanes shared with the altitude annotations.
- Local verification passed: Ruff clean, all active Python modules compiled, 157 tests passed, Streamlit AppTest reported no exceptions, and the local server returned HTTP 200. GitHub CI run `29768433237` passed every gate in 41 seconds.
- The private hosted endpoint responds with its expected `303` sign-in redirect. Signed-in visual confirmation of the `v2026.07.20.13` six-card row and overlapping hazard labels remains the first next-session check because this Codex session had no live in-app browser backend.

## Current Capabilities

- Daher PIM tables are hash-validated, fail closed, load from a reproducible snapshot, and interpolate across published temperature and weight columns with visible clamp warnings.
- Climb defaults to 124 KIAS below a selectable 10,000 ft MSL transition and 170 KIAS/M0.40 above it. The engine preserves cumulative Daher climb time/fuel/distance bands and applies route-, altitude-, temperature-, and wind-aware groundspeed integration.
- Airborne ETE covers takeoff to landing and is conservatively rounded; taxi, vectors, holds, and operational delays remain separate future allowances.
- NOAA/AWC terminal, wind, hazard, PIREP/AIREP, and advisory data carry feed-health, age, provenance, route coverage, and forecast-validity information.
- Mission planning includes multi-region winds, multi-leg fuel stops, per-leg weather/alternates, chained ETAs, reserve floors, legal-alternate checks, forecast-quality comparisons, and post-missed range insets.
- FAA waypoint resolution uses live NASR data with a complete date-aware current/preview offline bundle and a weekly refresh workflow.
- The route map uses local corridor geometry or Albers regional/national projection; the vertical profile clamps off-route hazards, identifies visually enlarged thin bands, places collision-aware category labels at band upper-left corners, and toggles consistently colored hazard layers locally from its legend without rerunning Streamlit.
- Tail profiles accept BOW, payload, and fuel; calculate takeoff/climb and representative mid-cruise weights; support versioned JSON import/export; and capture actual-versus-book time/fuel calibration deltas.
- AVWX remains a documented future enrichment/validation source rather than a silent replacement for NOAA/AWC data.

## Important Calculation Note

- The climb model selects/interpolates a Daher cumulative climb table by starting climb weight, then differences that cumulative curve into altitude bands. It does not independently subtract each band’s fuel and re-interpolate the next band at a reduced weight; doing so may double-count weight reduction already embodied in the manufacturer’s cumulative table and needs authoritative Daher clarification first.

## Current Known Limits

- No tail-specific CG station/arm envelope calculation or tank-capacity enforcement.
- Saved calibration deltas are comparison data and are not automatically applied to predictions.
- Tail-specific calibration remains explicitly recorded-only; applying it requires a future pilot opt-in and sufficient validated samples.
- Legal alternate logic still relies on the pilot’s approach-availability confirmation rather than direct Part 97 procedure ingestion.
- Route hazard geometry has edge-crossing breakpoints but not a full corridor-width buffer.
- GFA/FIP/GTG gridded products remain un-ingested until a stable documented source is available.
- Direction-unknown alternate fuel intentionally uses still-air TAS until a specific alternate course/wind model is added.

## Next Work

Use `TODO.md` as the ordered list. Current priorities:

1. Visually confirm the `v2026.07.20.13` mission-summary and overlapping hazard-label layouts in the hosted app after deployment.
2. Add optional taxi/vector/hold allowances and an opt-in mode for applying validated tail calibration deltas.
3. Add tail-specific CG stations/arms and envelope validation when authoritative loading data is available.
4. Continue hazard geometry toward explicit route-corridor buffering and richer forecast-validity breakpoints.
5. Deepen route PIREP/AIREP forecast-quality comparisons.
6. Revisit GFA/FIP/GTG only if AWC publishes a stable public API or another documented source is adopted.
