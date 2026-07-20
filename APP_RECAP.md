# App Recap

## Session Restart

- Load `C:\Users\JackOswald\OneDrive - ISOThrive Inc\codex-shared\AGENTS.md` as part of startup context for this repo.
- Then read `APP_RECAP_CURRENT.md` first for `Let's go`; use this full `APP_RECAP.md` only for deeper history or if the compact recap is missing.
- Preferred restart path: double-click `Let's go.cmd` in this repo so Codex starts with this repo already bound as the working project.
- `prior/SESSION_CONTEXT.md` and `prior/CHECKPOINT_*.md` are historical only unless explicitly requested.

## Goal

Build this app to be as good as or better than ForeFlight overall for mission calculations, while also providing weather and hazard detail that is easier to inspect and trust.

## Current State

### Close-Out Verification

- On April 26, 2026, the interrupted `Let's quit` sequence was recreated from repo evidence: screenshot inbox check found no new root-level images, `Let's go.cmd` matched the shared launcher installer output, `python -m pytest -q` passed with 79 tests, and both the app repo and `codex-shared` were clean and aligned with `origin/main` after fetch.
- Verified app repo head for that close-out: `a313235 Add waypoint fuel range insets`.

### Completed Core Work

- Repo startup flow standardized with a repo-local `AGENTS.md` that points to the versioned shared repo file `C:\Users\JackOswald\OneDrive - ISOThrive Inc\codex-shared\AGENTS.md`
- Repo now includes a double-clickable `Let's go.cmd` launcher that starts Codex in the correct project root through the shared `codex-shared` launcher flow
- Git global `core.excludesfile` now points to `C:\Users\JackOswald\OneDrive - ISOThrive Inc\codex-shared\.gitignore_global`
- Repo-local `.gitignore` now explicitly ignores `screenshots/` so the visual inbox and processed-file log stay local-only even if shared global Git config is unavailable
- Shared standards are now versioned and published in the private GitHub repo `joswald94123/codex-shared`
- `APP_RECAP.md` now explicitly tells restart sessions to load the shared AGENTS instructions first and treat `prior/` notes as historical only
- Live NOAA METAR, TAF, FD windtemp, G-AIRMET, AIRSIGMET, and TCF ingestion
- Route wind interpolation from NOAA FD windtemp data with explicit heuristic fallback messaging
- Mission table with direction, wind, ETE, ETA, and fuel by flight level
- Route-aware cruise flight levels down to `FL190` eastbound and `FL200` westbound
- Per-segment hazard timing using segment ETA instead of ETD-only validity
- Climb/descent hazard exposure modeling using segment altitude bands instead of cruise-altitude-only checks
- Departure and arrival time labels with correct seasonal timezone abbreviations
- Built-in TBM 960 performance profiles with cruise-mode selection in the sidebar
- Default built-in profile now seeded from official Daher TBM 960 PIM ISA tables
- Repo now vendors the Daher PIM PDF and parses the official source tables directly instead of relying only on hand-entered ISA rows
- Altitude-band climb/descent performance and fuel modeling driven by the active profile
- Profile-aware cruise TAS and fuel interpolation by flight level using published Daher data only
- Mission calculations now interpolate across official Daher cruise/climb ISA-deviation tables using forecast NOAA FD temperatures when available
- Fixed a deployed `NameError` in the temperature-altitude interpolation path and added regression coverage for interpolated temperature samples so Streamlit and local behavior stay aligned
- IAS-to-TAS conversion now uses a standard-atmosphere model instead of the old fixed-per-thousand-feet shortcut
- Route groundspeed now uses full wind-vector projection, so crosswind reduces along-track progress instead of being ignored
- Internal climb/descent wind sampling now follows the aircraft along the route in 3D instead of using one phase-average wind
- Default built-in descent profile now uses `220 KIAS`
- Descent TAS now derives from IAS using pressure altitude plus sampled forecast temperature, then converts to segment groundspeed with forecast winds for more accurate descent distance and timing
- Repo now vendors FAA atmosphere/performance reference PDFs in `assets\reference_docs\` to support the complex IAS/TAS/descent calculation path
- Departure, destination, and intermediate route points now validate through live FAA NASR current/preview cycle data instead of relying only on a static local waypoint dataset
- Mission calculations now support ordered intermediate waypoints and carry full multi-leg route geometry through distance, wind, hazard, map, and vertical-profile calculations
- NOAA fetches now retain per-feed health for METAR, TAF, FD windtemp, G-AIRMET, AIRSIGMET, and TCF instead of silently turning failed feeds into clear weather
- Weather Inputs now shows a data-confidence card plus NOAA feed-health details, including failed, empty-but-valid, row counts, request parameters, and error messages
- FD windtemp forecast cycle now selects `06`, `12`, or `24` from the requested ETD instead of always requesting `fcst=06`
- Live AWC endpoint check on April 24, 2026 confirmed `level=low` covers the current TBM flight-level set through FL390, `level=high` returns FL450/FL530, and `level=all` returns HTTP 400, so the app keeps `low` for the current FL190-FL310 planning envelope
- Terminal METAR/TAF risk scoring now flags flight category, visibility, ceiling, surface wind/gusts, weather strings such as thunderstorms/freezing precipitation, and TAF LLWS instead of treating terminal products as display-only evidence
- Route sampling now interpolates along great-circle legs instead of straight linear latitude/longitude between waypoints
- Hazard scoring and vertical-profile hazard spans now sample across each scored route interval, reducing false negatives from narrow polygons that cross a segment away from its midpoint
- G-AIRMET parsing and route scoring now include IFR, mountain obscuration, strong surface wind, and LLWS categories in addition to icing, turbulence, convective, and AIRSIGMET/TCF hazards
- Startup/taxi fuel is now explicit in the sidebar and defaults to 8 gallons, rounded from the PIM taxi fuel allowance of 50 lb; the mission burn/fuel-at-destination math uses that configurable fixed fuel value
- Reserve/alternate fuel planning is now explicit: alternate distance, final reserve minutes, final landing minimum, required destination fuel, reserve margin, and fuel status are calculated per flight level
- Cruise and climb calculations now support selected/interpolated published Daher PIM source-weight columns instead of being locked to the default 7,100 lb cruise and 7,394 lb climb baselines
- Composite mission posture now combines NOAA feed health, terminal METAR/TAF risk, route hazards, and fuel reserve margin while keeping data confidence separate
- Hazard scoring now uses adaptive route bins with waypoint and climb/descent breakpoints plus route/polygon edge-crossing checks
- NOAA ingestion now includes CWA polygons and localized PIREP/AIREP hazard reports; GFA/FIP/GTG was evaluated and left not ingested because current AWC Data API docs do not list a public endpoint for those gridded layers
- Known mission risk and weather/data confidence are now separated in the model and UI: failed/empty feeds reduce confidence and appear in confidence reasons instead of inflating observed mission risk
- Pilot-adjustable risk preferences now cover fuel-margin high/caution thresholds and broad route-exposure thresholds
- Pilot reserve floor override now supports gallons or pounds while preserving calculated reserve requirement, pilot floor, effective requirement, reserve margin, and fuel status
- Intermediate airport waypoints can now be marked as fuel stops, splitting fuel, reserve, ETD/ETA, and ETE rows across each fuel-stop leg
- Alternate-airport ICAO resolution now calculates a direct destination-to-alternate route distance and includes alternate terminal weather in Weather Inputs
- PIREP/AIREP retrieval now uses AWC's spatially constrained query shape; empty PIREP/AIREP results do not lower overall data confidence by themselves
- Production modules and regression tests now have English docstrings on every top-level function/class boundary so a competent Python coder can follow the code and verification surface after future restarts
- Legal alternate-required logic now evaluates the fixed-wing Part 91 destination alternate exception from destination approach availability and destination TAF ceiling/visibility in the ETA +/- 1 hour window
- Forecast-vs-actual quality checks now compare recent METARs with the applicable TAF period for departure, arrival, and the selected alternate without treating missing observations as a failure
- Post-missed fuel-range inset maps now display for the destination and intermediate fuel stops using expected FOB at each waypoint, missed/climb/descent fuel only, forecast winds sampled by bearing and altitude through climb/cruise/descent, and a `Range Ring Calcs` tab with the `alt_` fuel and distance fields
- Fuel-stop segmentation now supports stop-specific uplift quantities and per-leg alternate airport choices
- Alternate routing now supports FAA-resolved custom fixes between the destination and selected alternate instead of direct-only distance
- The Streamlit UI now shows `Running version` in the sidebar and route hero pills from hosting commit environment variables or local Git so local and Streamlit Cloud builds can be identified directly

### Recent UI Work

- Streamlit UI refreshed with a stronger visual shell instead of the old flat single-page layout
- New route hero and improved empty state now make the app readable before and after a route is fully entered
- Main output is now split into dedicated `Mission Plan`, `Hazards`, and `Weather Inputs` tabs
- Mission summary cards now surface departure timing, distance, focus flight level, fuel at destination, hazard posture, performance model, and wind source
- Default cruise mode now starts on `Max Cruise` instead of `Recommended Cruise`
- Mission Plan now includes a cropped route-context map with state outlines plus departure, destination, and course overlay for geographic context
- Route-context map rendering now preserves aspect ratio so state outlines are not stretched to fit the card
- Route-context map now switches between corridor, regional, and lower-48 views based on route length/span so long trips keep national context
- Hazards now includes a side-profile route rendering that shows climb/cruise/descent plus route-relevant hazard bands with forecast base/top altitudes
- Vertical profile now has hazard-type visibility toggles and dedicated header/footer layout rules to reduce text collisions
- Mission matrix now highlights the current focus flight level so table scanning is easier
- A dedicated `Performance Tables` tab now exposes the official Daher PIM cruise, climb, and descent tables for direct in-app review
- Sidebar aircraft controls now include official Daher climb-schedule and descent-rate selections
- `Performance Tables` now lets you browse extracted source-table families by cruise mode, ISA deviation, weight, climb schedule, and descent rate
- Fixed a deployed `Performance Tables` tab `NameError` caused by missing Daher metadata imports in the Streamlit UI
- Hazard detail now has its own tab with summary cards plus per-segment exposure review
- Raw NOAA weather evidence now has its own tab for trust, troubleshooting, and later calibration work
- Raw NOAA weather evidence now includes per-airport METAR and TAF terminal risk cards beside the raw products
- Mission and hazard tables now surface IFR, mountain obscuration, surface wind, and LLWS route exposure columns/cards, with a surface/terrain vertical-profile toggle
- Sidebar wording was tightened around `Mission Setup`, `Aircraft`, and `Flight Plan`
- A reserved `Next: Profile Workspace` placeholder now exists in the UI so future `Settings -> Profile` work has a clear landing zone without another layout rewrite
- `Flight Plan` now includes an intermediate-waypoint text box between departure and destination so routes can follow airports, VORs, and FAA fixes instead of assuming direct only
- The waypoint entry area now warns when the entered route appears non-linear or backtracking relative to the overall trip
- `Reverse` now swaps the intermediate waypoint order along with departure and destination so the route stays coherent after a direction flip
- Route-context map and vertical profile now annotate intermediate waypoints at their cumulative route positions
- The upper mission brief hero now shows only departure and destination ICAOs so long routed waypoint chains do not overwhelm the header

### Authoritative Performance Source

- Use `C:\Users\JackOswald\OneDrive - ISOThrive Inc\Personal\Flying\Turboprops\N256DX TBM 960\Manuals\PIM TBM960E0R1 (DRAFT).pdf` as the primary Daher source for built-in performance data
- Keep the vendored repo copy at `assets\manuals\PIM_TBM960E0R1_DRAFT.pdf` so the app and tests can parse the official source locally
- Use `daher_pim_tables.py` as the structured extraction layer that preserves all published cruise/climb weights, ISA-deviation slices, climb schedules, and descent-rate columns
- Use the repo-local `assets\cb_2023_us_state_20m.zip` official U.S. Census 2023 20m state boundary KML as the static map boundary source for route context
- Current built-in rows are seeded from:
  - `Table 5.10.5` for climb (`124 KIAS`, ISA)
  - `Table 5.11.5` for `MXCR` cruise (ISA, `7,100 lb`)
  - `Table 5.11.34` for `RCR` cruise (ISA, `7,100 lb`)
  - `Table 5.11.49` for `LRCR` cruise (ISA, `7,100 lb`)
  - `Table 5.12.1` for descent (`230 KCAS`, `1,500 fpm`)
- The hard-coded built-in TBM 960 profile data comes from official Daher manuals and should be treated as the durable baseline for this app
- No manual performance entry is needed for the built-in baseline; keep the app on published Daher data unless a later session deliberately adds an optional tail-specific calibration mode alongside it

### Authoritative Route And Weather Reference Sources

- Use the FAA NASR Subscription index at `https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/` as the entry point for live airport, navaid, and fix validation
- The app currently resolves route identifiers from the linked FAA/NFDC current-cycle and preview-cycle `APT`, `NAV`, and `FIX` CSV ZIP downloads so new or retired waypoints are picked up with chart-cycle changes
- Keep reusable FAA reference material in `assets\reference_docs\` for future atmosphere, airspeed, and performance-method validation:
  - `FAA_PHAK_Chapter_8_Aerodynamics.pdf`
  - `FAA_PHAK_Chapter_11_Airplane_Performance.pdf`
  - `FAA_Aircraft_Dynamics_Model.pdf`

### Recent Calibration Reality Check

ForeFlight comparison on `KSTS -> KPSP` for the March 6, 2026 evening departure showed:

- Distance is effectively correct
- Mid-level tailwind and time estimates are reasonably close
- Fuel burn is still too flat and too optimistic in this app
- The altitude tradeoff shape is still too compressed compared with ForeFlight
- `FL310` wind in this app is materially higher than ForeFlight for that specific case

## Main Remaining Gap

The biggest immediate weakness is now tail-specific loading/profile persistence plus continued route/hazard geometry refinement. Known mission risk, data confidence, reserve floors, fuel-stop segmentation, legal alternate assessment, alternate routing, and contextual fuel-range inset maps now exist, but the app still lacks tail-specific loading, CG, custom profile persistence, and explicit corridor-width hazard buffering.

Current simplifications still in use:

- The official Daher baseline is intentionally fixed in code and is acceptable as the long-term default
- There is no user-editable storage or CSV import yet for optional alternate/custom profiles
- No tail-number-specific profile management yet
- No tail-number-specific loading, CG, or custom profile persistence yet; current gross-weight controls select/interpolate the published source-weight columns only
- No direct user control yet for torque/power targets beyond the published cruise-mode and climb/descent schedule families
- No profile version history or verification workflow yet
- Fuel-stop segmentation supports per-stop uplift, leg alternates, and contextual range insets, but it does not yet enforce tank capacity or tail-specific fuel limits
- Legal alternate-required logic depends on an explicit destination approach-availability checkbox; the app does not yet ingest Part 97 approach procedure data directly

## Recommended Next Major Track

Continue the approved review/work-plan sequence from `C:\Users\JackOswald\OneDrive - ISOThrive Inc\Personal\Flying\Weather\CODEX Assistant to CODEX\CODEX_Weather_Brief_Review_and_Work_Plan_2026-04-24.md`.

Immediate next implementation slices:

- Add tail-specific loading/profile persistence and calibration workflow.
- Continue route/hazard geometry toward explicit corridor-width buffering and richer forecast-validity breakpoints.
- Revisit GFA/FIP/GTG only if AWC publishes a stable public API or the app vendors a documented gridded-data source.

## Immediate Next UI Follow-Up

Before starting the persistent profile system, the current UI and new route-entry flow should be verified in real use and then refined on top of the new tabbed structure.

Likely next UI work after verification:

- tighten spacing, table width, and card density based on real usage
- improve the selected-flight-level workflow so mission focus, hazard detail, and comparison actions feel more connected
- add clearer visual treatment for caution/high-risk hazard states
- refine the new risk-preference, reserve-floor, fuel-stop, and alternate-airport controls after real use
- decide whether weather evidence should stay tabbed or move partly into expanders/panels
- decide whether the full routed fix string needs a secondary display location now that the hero title is intentionally limited to departure and destination
- refine waypoint entry and validation messaging if live FAA lookups expose ambiguous or procedure-style identifiers
- add a real `Settings -> Profile` surface when the persistence track begins, reusing the placeholder added in this session

## Settings/Profile Proposal

### Profile Purpose

Allow one or more aircraft profiles to drive mission calculations.

Examples:

- Book/default TBM 960 profile
- Tail-number-specific profile
- Instructor-maintained calibrated profile
- Experimental profile for testing new data before promoting it

### Data To Store

#### Cruise Performance Table

Store rows keyed by at least:

- flight level or altitude
- power or torque setting
- standard/ISA condition marker
- TAS
- fuel burn

Future expansion fields:

- temperature deviation from ISA
- gross weight band
- OAT
- notes
- source and confidence
- sample count

#### Climb Performance Table

Store rows keyed by altitude band and profile setting:

- target climb IAS
- climb rate
- climb fuel flow or fuel burn
- optional climb TAS

#### Descent Performance Table

Store rows keyed by altitude band and descent mode:

- target descent IAS
- descent rate
- descent fuel flow or fuel burn
- optional descent TAS

### Recommended App Behavior

- Let the user choose an active profile in Settings
- Interpolate between known altitude rows instead of using one fixed TAS/fuel value
- Support multiple cruise modes such as max cruise, normal cruise, and economy
- Keep a default built-in profile so the app works before any custom data is entered

## Better Structure Than Raw Notes

To make collected instructor data genuinely useful, store each performance entry with metadata:

- source: book, instructor, observed flight, ForeFlight comparison, other
- date captured
- conditions summary
- confidence level
- verified flag
- free-text notes

This avoids polluting the production model with unverified observations.

## Suggested Future Implementation Order

1. Add persistent profile storage plus CSV import/export.
2. Add editable profile tables in Settings for cruise, climb, and descent.
3. Add a calibration view comparing app output against ForeFlight and real-world observed flights.
4. Add selected-cruise summary metrics for direct comparison:
   - ETE
   - ETA
   - fuel burn
   - fuel at destination
5. Refine the new vertical mission profile view with terrain and richer hazard controls.

## Specific Calibration Targets Preserved From Current Comparison

- Improve fuel model so lower flight levels are not unrealistically close to high-flight-level burns
- Improve high-altitude wind handling so cases like `FL310` match ForeFlight more closely
- Improve altitude optimization so the shape of the ETE/fuel curve matches ForeFlight more closely
- Keep distance accuracy at current quality
- Validate that multi-leg route timing and wind projections still track ForeFlight well once real routed trips are entered instead of direct legs only

## Practical Suggestion

When performance data is first entered, prefer table entry and CSV import over free-form notes.

That makes the model usable for interpolation and testing immediately.

## Long-Term Nice-to-Have

Once the profile system exists, support:

- exporting and importing profiles
- multiple named profiles
- profile version history
- reverting to the built-in baseline profile

## July 19, 2026 — Performance integrity and composite climb

- Added complete-row validation across all mapped Daher PIM pages and fail-closed altitude-band checks before vertical interpolation.
- Added `scripts/build_pim_snapshot.py` plus a checked-in JSON snapshot keyed to the vendored PDF SHA-256, eliminating normal cold-start PDF parsing without disconnecting the data from its audited source.
- Added the pilot-selectable composite climb schedule: 124 KIAS below 10,000 ft MSL by default, then 170 KIAS/M0.40, with the transition altitude configurable.
- Defined displayed mission ETE as takeoff-to-landing airborne time, retained wind-adjusted groundspeed sampling across all phases, and changed displayed ETE/fuel burn to conservative upward rounding.
- Added regression coverage for every mapped PIM page, composite schedule splicing, and the composite schedule's mission-time effect; the suite increased from 79 to 82 tests.

## July 19, 2026 — Hazard and alternate correctness

- Replaced PIREP substring classification with structured icing/turbulence intensity and altitude fields, including explicit NEG/NIL filtering and ICT identifier immunity.
- Restricted CWA classification to phenomenon fields, bounded G-AIRMET records to their forecast snapshots, and treated AIRSIGMET numeric altitude fields as raw feet.
- Fixed clear-sky TAF alternate logic plus `vertVis`/`OVX` obscured-ceiling scoring and expanded explicit weather/severity tokens.
- Added end-to-end feed fixtures; the full suite reached 90 passing tests.

## July 19, 2026 — Long-route wind coverage

- Added automatic departure/midpoint/destination FB-region merging for routes over 500 NM, with station/altitude deduplication.
- Added a 300 NM station-coverage cap, invalid-direction rejection, and distance-scaled cruise integration bins.
- Added regression coverage for transcontinental region selection, coverage caps, and malformed direction groups; the suite reached 93 passing tests.

## July 19–20, 2026 — Claude work-plan completion release

- Completed the remaining performance, hazard, fuel/range, multi-leg, wind-source, UI-trust, and resilience items from the reviewed Claude Fable plan.
- Added complete date-aware FAA NASR fallback data and a weekly current/preview refresh workflow.
- Added Ruff correctness lint and all-active-module compilation to CI; Python 3.13.7 verification now passes 119 tests plus Streamlit AppTest with zero exceptions.
- Replaced silent unresolved-airport fallback behavior with a visible failure and added explicit warnings whenever forecast temperature or aircraft weight clamps to Daher table limits.
- Added Albers projection for regional/lower-48 maps, missing detour/zero-ring regression coverage, and bounded/minimum-cue rendering for thin or off-route profile hazards.
- Added tail-specific BOW, payload, and fuel inputs, computed takeoff/climb and representative mid-cruise weights, portable versioned JSON profiles, and actual-versus-book calibration-delta capture.
- Released and pushed `v2026.07.20.1` at commit `1e03d10`; GitHub CI passed.

## July 20, 2026 — Climb-weight interpretation and tooling

- Confirmed the app interpolates the Daher cumulative climb curve using the selected starting climb weight and then differences it into altitude bands for route/wind integration.
- Recorded that the engine does not independently re-interpolate every subsequent band after subtracting prior-band fuel. This should not change without authoritative confirmation that doing so would not double-count weight reduction already represented by the manufacturer’s cumulative table.
- Updated pip to `26.1.2` in the local project and Python 3.13 release-verification environments; no tracked project files changed from the pip update.

## July 20, 2026 — Claude follow-up review remediation

- Independently confirmed and closed the five new P1 findings plus the actionable P2/P3 punch list from `Follow_Up_Review_2026-07-20.md`.
- Added great-circle leg-track evolution, conservative malformed-ceiling handling, complete structured PIREP intensity/layer scoring, snapshot-path PIM validation, final-leg range fuel, and explicit unresolved-alternate warnings.
- Hardened NOAA/AWC units, validity, fallback, caching, and typed result handling; retained both current and preview FAA NASR cycles and made refresh-branch CI explicit.
- Consolidated duplicate performance interpolation and route/radial integration implementations, adopted shared exact-numeric multi-leg timing, and expanded the suite to 143 passing tests.
- Split deployment and development dependency locks and added a CI gate that freshly reparses all 28 mapped Daher PIM tables and requires an exact snapshot match.
- Hardened the release-version gate to compare the complete GitHub push range, including multi-commit and rewritten-history pushes, instead of assuming `HEAD^` identifies the prior deployment.
- Added a deployment hot-reload guard after Streamlit Community Cloud retained the pre-release `route_planning` module and rejected newly added imports despite the pushed files being consistent.
- Refreshed the landing and mission-matrix copy so completed profile persistence/import/export work is no longer described as future functionality.
- Fixed the FAA provenance caption for partial airport input so entering only a departure cannot dereference a missing route plan.
- Corrected the landing-minimum semantics: the operator value is now a destination-arrival floor compared against alternate-plus-reserve fuel and the separate pilot floor, rather than an additive buffer that double-counted protected fuel.
- Renamed destination fuel displays to `FOB at Landing` and explicitly stated that the value is gross fuel at touchdown before any alternate or reserve use.

## July 20, 2026 — Interactive route-profile hazard layers

- Replaced the four Streamlit profile checkboxes with clickable legend tiles inside the route-profile chart, so presentation-only filtering no longer reruns the app or returns the user to the Mission Plan tab.
- Added mouse and keyboard legend activation, dimmed hidden-layer tiles, and kept all filtering local to the embedded chart.
- Labeled every rendered hazard region with automatic text fitting and grouped same-type fills so overlapping Convective regions retain one consistent color.
- Expanded the full suite to 147 passing tests and released the change as `v2026.07.20.9`.
- Added a guarded `route_vertical_profile` module refresh after Community Cloud retained the pre-release renderer during deployment; the regression suite now simulates that stale-module state before binding the interactive helper.
- Replaced one-off hot-reload recovery as the primary defense with a release-boundary bootstrap that evicts every cached repo module before imports whenever a reused Streamlit process observes a new `RELEASE_VERSION`; CI covers both cross-release eviction and same-release cache retention.

## July 20, 2026 — Mission-summary, ETD, and hazard-label regression release

- Fixed the `FOB at Landing` mission-summary card regression by keeping gross-touchdown and reserve-margin wording concise in the card while moving the complete effective-requirement audit trail directly below the six-card row.
- Changed first-load ETD selection to the same rounded 15-minute lead used by the `Now +15 min` action, and added a five-minute grace window so the selected current minute does not immediately produce a stale-time warning.
- Restored hazard category names to each route-profile band's upper-left. Category and altitude labels now share collision bookkeeping, shrink only when necessary, and use separate vertical lanes even for fully overlapping thin bands.
- Added pure presentation helpers and coordinate-level SVG regression tests. Ruff, compilation, Streamlit AppTest, and the full 157-test suite passed locally.
- Released and pushed `v2026.07.20.12` at commit `6fc55d9`; GitHub CI run `29768433237` passed all version, dependency, compilation, Ruff, fresh PIM snapshot, and test gates in 41 seconds.
- Advanced the final close-out build to `v2026.07.20.13` because the main-branch deployment gate requires a version change on every push; `.13` contains the same app code as `.12` plus the finalized recaps.
- The private Streamlit URL and health path returned the expected unauthenticated `303` sign-in redirect. Final signed-in visual confirmation remains pending.
- Browser diagnostics found the Browser plugin enabled and browser feature flags available, but the current session exposed no browser backend and its configured native computer-use pipe was absent. For future visual automation, open the built-in Browser from a fresh ChatGPT desktop-app Codex chat; standalone CLI and IDE sessions do not provide it.
