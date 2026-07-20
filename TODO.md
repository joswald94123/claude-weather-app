# TODO

## UI Regression Release — v2026.07.20.12

- Source screenshot: `C:\Users\JackOswald\OneDrive - ISOThrive Inc\ScanSnap\Screenshot 2026-07-20 110530.png`.
- Reproduced on hosted `v2026.07.20.11`, build `e243733bde8e`, with route `KSTS -> KAUS` at desktop width.
- Done locally: the `FOB at Landing` card now shows concise gross-touchdown and margin copy, while a caption below the row retains the full alternate/reserve, landing-minimum, and pilot-floor audit trail.
- Done locally: first-load ETD now matches the `Now +15 min` behavior, and current-minute selections receive a five-minute grace window before the stale-time warning appears.
- Done locally from the 11:27 screenshot: hazard category names return to each band's upper-left and share collision bookkeeping with altitude labels, including smaller type and lower label lanes when bands overlap.
- Verification: Ruff clean, all active modules compiled, 157 tests passed, Streamlit AppTest was exception-free, and the local server returned HTTP 200.
- After deployment, visually verify close-out build `v2026.07.20.13` at desktop and narrower responsive widths; it contains the same UI code introduced in `.12` plus finalized recaps.

## Claude Follow-Up Review — Completed July 20, 2026

- Closed all five confirmed P1 findings and all directly actionable P2/P3 punch-list items.
- Added regression coverage for every safety-relevant sibling path identified by the review, including snapshot loading, layered PIREPs, malformed ceilings, final-leg fuel, unresolved alternates, long great-circle legs, raw advisory units, stale caches, and fallback-cycle retention.
- Consolidated the duplicated performance interpolators and weather integration engines, adopted the shared multi-leg timing helper, preserved exact numeric airborne time between calculations, replaced NOAA result type suppressions with checked accessors, and replaced the sleep-based concurrency assertion with deterministic synchronization.
- Split production and development locks; CI now checks compilation, Ruff, a fresh 28-table PIM reparse/diff, and the full test suite. NASR refresh branches explicitly dispatch that CI after a token-authenticated push.
- Hardened the release-version gate for multi-commit and rewritten-history pushes by comparing GitHub's complete pushed range.
- Added a narrowly scoped Streamlit hot-reload guard so a cached pre-deployment route module cannot break imports after an in-process Git update.
- Remaining items below require new authoritative aircraft data, a new external data source, or an explicit product decision; they are not incomplete findings from this review.

## Recently Completed UI And Risk Model

1. Separate known mission risk from weather/data confidence.
   - The mission UI now uses `Known mission risk` and `Weather/data confidence`.
   - Failed or empty feeds reduce confidence and appear in confidence reasons instead of counting as observed risk.

2. Add pilot-adjustable relative-risk preferences.
   - Fuel-margin high/caution thresholds and broad route-exposure thresholds are adjustable.
   - Weather/feed confidence remains separate from known-risk severity.

3. Add a user-defined fuel reserve floor for risk scoring.
   - The pilot can enter the floor in gallons or pounds.
   - The table shows calculated reserve, pilot floor, effective requirement, and margin.

4. Unify UI trust thresholds and weather freshness.
   - Fuel-margin tones now use the configured high/caution thresholds, and independently keyed widgets warn and normalize invalid ordering.
   - Reserve margin appears in the route hero.
   - NOAA fetch age is visible and `Refresh Weather` clears the cache before refetching.
   - Empty advisory feeds are treated as legitimate clear-weather results; empty requested terminal/wind feeds still reduce confidence.
   - Reverse-route clears destination-specific alternate fields and explains the reset rather than silently retaining incompatible leg assumptions.
   - ETD controls prevent past dates, flag past times, and offer `Now +15 min`.
   - Compatibility shims name dropped inputs, while mission/hazard calculation exceptions show concise retryable errors.
   - Visible builds use `vYYYY.MM.DD.N · build <sha>`; the bump script advances the daily deployment sequence and CI requires a version change on every `main` push.
   - The mission matrix defaults to compact overall-hazard output with a detailed-hazard toggle, and deprecated Streamlit width arguments have been removed.
   - Preview route validation is reused by the actual pass when inputs match, and outbound/return mission construction now shares one parameterized call.

## Recently Completed Mission Planning

4. Mark intermediate airport waypoints as fuel stops.
   - The app now segments fuel, reserves, ETD/ETA, and ETE across each fuel-stop leg.
   - Fuel-stop waypoints are visually distinct on the route map and vertical profile.

5. Resolve alternate airport identifiers and calculate destination-to-alternate distance.
   - A resolved alternate ICAO now overrides the manual distance field.
   - Alternate terminal weather appears in Weather Inputs.

## Next Mission Planning Follow-Ups

0. Performance integrity and takeoff-to-landing time fidelity.
   - Done: all 28 mapped Daher PIM pages now fail closed unless their complete 17-row altitude sequence parses successfully.
   - Done: vertical interpolation refuses mismatched altitude bands instead of silently pairing rows by position.
   - Done: climb planning defaults to 124 KIAS below a pilot-selectable 10,000 ft MSL transition and 170 KIAS/M0.40 above it, using the distinct PIM schedule data on each side.
   - Done: displayed airborne ETE and fuel burn round conservatively; the UI defines airborne ETE as takeoff-to-landing excluding taxi, vectors, holds, and operational delay.
   - Done: normal cold starts load a reproducible checked-in JSON snapshot only when its source-PDF SHA-256 matches; the builder reparses and validates every mapped page.
   - Done: tail-specific BOW, payload, and fuel inputs calculate takeoff/climb and representative mid-cruise weights; versioned JSON import/export preserves profiles across Community Cloud dormancy.
   - Done: actual-versus-modeled airborne-time and fuel entries calculate and save per-tail calibration deltas for comparison with book performance.
   - Next: add optional taxi/vector/hold time allowances and an explicitly opted-in mode that applies validated tail calibration deltas to predictions.

1. Legal alternate-required logic from destination TAF and approach availability.
   - Done: the Mission Plan now shows fixed-wing Part 91 destination alternate-required status using destination approach availability plus the ETA +/- 1 hour TAF 2,000 ft / 3 SM check.
   - The logic stays separate from fuel planning buffers and pilot reserve preferences.
2. Forecast-vs-actual quality checks by phase of flight.
   - Done: recent METARs are compared with the applicable TAF period for departure, arrival, and the selected alternate.
   - Missing PIREP/AIREP data and missing observations are not treated as quality failures by themselves.
   - Do not treat missing PIREP/AIREP data as a high-risk factor by itself.
   - Highlight PIREPs/AIREPs only when available reports indicate significant differences from the forecast.
   - Apply the same rule to recent METARs: highlight material differences from the relevant TAF or forecast, not the mere presence or absence of an observation.
   - Compare TAFs and other forecasts against recent METARs, and against PIREPs/AIREPs when available, then apply forecast-quality concerns to the correct departure, enroute, arrival, or alternate phase of flight.
3. Hazard and legal-alternate feed correctness.
   - Done: structured PIREP icing/turbulence intensity and altitude fields replace substring classification; NEG/NIL observations do not create hazards.
   - Done: CWA classification uses phenomenon fields, G-AIRMET snapshots bind to forecast time, and AIRSIGMET numeric altitudes remain raw feet.
   - Done: clear-sky TAF periods count as unlimited ceiling, while `vertVis`/`OVX` count as obscured ceilings.
   - Done: `SVR`, freezing fog, hail, and vicinity-thunderstorm risk tokens are normalized explicitly.
4. Long-route wind-source fidelity.
   - Done: routes over 500 NM fetch and merge departure, midpoint, and destination FB regions instead of trusting one midpoint region.
   - Done: stations beyond 300 NM are excluded, invalid directions are rejected, and cruise integration uses at least one bin per 75 NM.
   - Done: FB `DATA BASED ON` and `FOR USE` headers populate issue/validity metadata, including validity windows crossing midnight.
   - Done: cycle selection is rechecked relative to the returned product issue time, and ETDs outside the product window are flagged.
   - Done: mixed regional fetch failures are `Partial`, and the UI reports the percentage of route wind bins within the 300 NM station cap.
5. Lopsided alternate-destination range rings around the destination airport.
   - Done: the Mission Plan now has waypoint-specific range inset maps for the destination and intermediate fuel stops.
   - Done: ring geometry uses expected FOB at each waypoint, subtracts the missed-approach allowance plus modeled post-missed climb/descent fuel, and leaves the remainder as `alt_cruise_fuel_gal`.
   - Done: each ring samples forecast winds by bearing and altitude during climb, cruise, and descent, so the shape is not forced to be circular.
   - Done: the `Range Ring Calcs` tab shows the per-waypoint FOB, altitude, alt fuel components, and alt range-distance summary.
   - Done: low-altitude rings sample the retained sub-FL180 PIM cruise rows rather than clamping all rings to FL190.
   - Done: AVWX airport elevations prefer `elevation_ft`, and the range inset labels explicitly state that the output is advisory rather than legal/reserve protection.
   - Done: direction-unknown alternate fuel uses still-air TAS rather than incorrectly carrying the mission-route headwind or tailwind onto the alternate course.
   - These rings are contextual reach graphics, not reserve/legal protection.
   - Show each altitude up to 20,000 ft AGL with a distinct dashed, dotted, or hybrid line style.
6. Stop-specific fuel uplift and optional alternate choices for each fuel-stop leg.
   - Done: the sidebar accepts per-stop uplift gallons and per-leg alternate ICAOs, and the fuel-stop table shows start fuel, uplift, next start fuel, alternate route, and alternate distance.
   - Done: NOAA terminal weather includes every marked fuel stop and entered per-leg alternate.
   - Done: the final destination's legal-alternate window uses chained leg ETAs plus fuel-stop ground time rather than the nonstop ETA.
   - Done: an intermediate leg without an alternate is labeled `Not specified — alternate fuel excluded` instead of silently appearing as a zero-distance alternate.
   - Done: each leg shows a legal-alternate and forecast-quality result at its accumulated ETA; pilots explicitly confirm approach availability for intermediate destinations, with unconfirmed stops handled conservatively.
   - Done: strict ETE/timing chaining and uplift/alternate fallback policy live in tested route-planning helpers, including cross-timezone and two-stop regressions.
   - Next: move the remaining per-leg mission-brief orchestration out of the UI if another non-UI consumer is added.
7. Custom alternate route fixes if direct destination-to-alternate distance is not enough for real planning.
   - Done: the sidebar accepts optional FAA-resolved fixes between destination and alternate and uses that custom alternate route distance when available.
8. Visible running version in persistent UI chrome.
   - Done: the app shows `Running version` in the sidebar and route hero pills from hosting commit environment variables or local Git.
   - This is intended to confirm whether local Streamlit and Streamlit Community Cloud are serving the expected pushed build.
9. Tail-specific loading/profile persistence and calibration workflow.
   - Done: computed loading weights, session persistence, portable JSON import/export, and actual-vs-modeled calibration delta capture.
   - Next: CG-envelope calculations require tail-specific stations/arms and remain outside the supplied Claude plan.
10. Continue adaptive route/hazard geometry toward explicit corridor-width buffering and richer forecast-validity breakpoints.
11. Revisit GFA/FIP/GTG only if AWC publishes a stable public API or the app vendors a documented gridded-data source.
12. Future enhancement: import private contract/discount fuel prices from CSA, Avfuel, Signature Aviation, and similar programs.
    - Prefer an official vendor API or scheduled CSV/Excel price export.
    - Consider automated mailbox ingestion for vendor-delivered price sheets.
    - Keep credentials outside the app and show vendor, FBO, effective time, retrieval age, and tax/fee inclusion.
    - Do not automate authenticated portals without reviewing vendor permission, terms, and MFA handling.
13. FAA resilience and cache lifecycle.
    - Done: cycle-specific parsed indexes remain cached without retaining duplicate raw ZIP bytes.
    - Done: missing/unreadable NASR CSV members produce actionable errors with regression coverage.
    - Done: FAA network failures are cached for five minutes so offline reruns do not repeat a timeout per route token.
    - Done: the complete 2026-07-09 airport/navaid/fix cycle is vendored as a compressed fallback, built reproducibly from official FAA downloads and explicitly labeled in waypoint provenance.
    - Done: the fallback also retains the published preview cycle and selects current versus preview using the planned flight date.
    - Done: a weekly Thursday workflow detects new FAA current/preview cycles, validates a rebuilt bundle, bumps the release, and prepares a reviewable pull request.
14. Stage 7 map and retrieval resilience.
    - Done: malformed, unreadable, or incomplete state-boundary archives degrade gracefully without blocking route and range overlays.
    - Done: projection regression coverage verifies that equal geographic distances use a uniform onscreen scale.
    - Done: CI uses the current Node 24 generations of the official checkout and Python setup actions.
    - Done: independent live NOAA requests run concurrently with one HTTP session per worker; injected sessions remain sequential, and regression coverage verifies overlap and cleanup.
    - Done: malformed fallback schemas, missing dated cycles, and combined live/fallback FAA failures have explicit regression coverage.
   - Done: the route map has an accessible title and compact legend for route, fuel-stop, destination, and range symbols while sharing the tested uniform projection.
   - Done: regional/lower-48 maps use an Albers projection; vertical-profile hazards clamp to the plot and disclose minimum-size enlargement.
   - Done: CI compiles every active Python module and runs Ruff correctness checks; detour and zero-ring branches have direct regression coverage.
15. Future AVWX validation and enrichment (`docs/AVWX_FUTURE_ENHANCEMENT.md`).
    - Start with a plan/entitlement probe and terminal METAR/TAF shadow comparison.
    - Treat AVWX primarily as a second parser and station-selection check because its weather may share NOAA upstream data.
    - Keep discrepancies provenance-visible; do not silently merge feeds or change mission calculations.
    - Later evaluate route station/advisory audits, structured PIREP/AIR-SIGMET comparison, NOTAM advisories, and separate NBM guidance.
