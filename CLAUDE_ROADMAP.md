# Claude Weather App — Improvement Roadmap

This repo is a parallel implementation forked from CODEX-Weather-Brief at
`28b2dc9` (v2026.07.20.13). The source project continues independently; this
repo starts from the same baseline and converges on the architecture described
in the July 2026 review series (`Deep_Sweep_2026-07-20.md` and the
`CODEX_FIX-01..09` chunks in the review folder).

## Phase 0 — shipped with the fork
- FIX-01 item 1: Refresh Weather button crash (`.clear()` on the uncached wrapper).
- FIX-02 item 1: AIRSIGMET surface-base (`altitudeLow1=0`) falsy-zero fix + regression test.
- FIX-05 items 1–2: `tone-moderate` hero/pill CSS coverage; Turbulence column coloring.

## Phase 1 — remaining P1 correctness (COMPLETE 2026-07-20, v2026.07.20.21)
1. DONE (v.17) FIX-06 item 1: descent wind mirroring fixed in the route integrator + failing-first gradient regression test.
2. DONE (v.18) FIX-04: multi-leg hero/FOB/mission-risk truthfulness via resolve_mission_headline + risk override.
3. DONE (v.19) FIX-01 items 2–3: degraded-bundle 2-minute session stash; release-keyed cache keys.
4. DONE (v.20) FIX-02 remainder: CWA band swap / UCWA-in-cwaText / SEV qualifier; VV-cover units; vertVis in forecast-quality; window constants.
5. DONE (v.21) FIX-03: shared hazard_applies_at() horizon fallback for table + profile, capped at 6 h.

## Phase 2 — policy, validation, hardening
- DONE (v.22) FIX-07: reserve-basis caption + decision comment at the formula
  (floor protects intended destination touchdown, diversion excluded);
  292-gal usable-capacity cap on fuel load and uplifts with a visible trim
  warning; hero wind parenthetical derived from computed winds; per-leg wind
  models for chained legs; falsy-track cleanup.
- DONE (v.24) FIX-08: guarded RELEASE_VERSION read with env-override precedence
  matching app_version; FAA missing-anchor failure tests; unknown hazard types
  render generically in the profile (and an explicit empty visible-set now
  means hide-all); workflow timeout-minutes; snapshot env-flag comment;
  launcher requires pwsh and targets this repo.
- DONE (v.27–v.30) Review-remainder closeout: FIX-06 items 2–7 (fail-loud
  sentinels, clipped composite bands, MIN_PLANNING_RATE_FPM, page-map assert,
  integrator dedup, label trio, snapshot --check under pytest); numeric AWC
  severity verified live and >=4 documented High; ISO validTime parser test;
  UI source-contract tests; NASR refresh outage tolerance; clean 3.13
  pip-compile lock regeneration (backport pins removed). Open deferral: the
  AppTest value-equality guardrail, blocked on feed fixtures (Phase 3 item 4).
- DONE (v.23) FIX-05 remainder: spinners over per-leg briefs, destination
  rings, and the vertical profile; widget default+session-state warnings
  removed (cruise, ETD, hazard-detail); ignored per-leg alternate entries
  captioned; terminal-risk reasons name the airport; widespread-exposure
  escalation labeled; TAF-risk card scoped "worst of full TAF"; matrix wind
  sign legend; dead helpers deleted.

## Phase 3 — structural convergence (FIX-09 → brief-as-document)
1. FIX-09 — DONE (v.25–v.26): `weather_core.build_multi_leg_plan` computes every
   chained leg into frozen MissionLegPlan/MultiLegPlan with a golden two-leg
   test; the westbound convention is core-owned (`derive_direction=True`, no UI
   pre-swap; `is_westbound_route` exported); the windtemp refetch decision is
   core-owned (`windtemp_cycle_correction`); residual UI arithmetic moved to
   helpers (`gallons_from_pounds`, `preferred_baseline_flight_level`); the
   no-UI-arithmetic rule is written into AGENTS.md. Deferred: an AppTest
   asserting rendered values equal core-document fields — needs the feed-
   fixture infrastructure (item 4) to drive a full mission headlessly.
2. DONE (v.31) `FuelLedger`: frozen value object carrying the full derivation
   (start → taxi/climb/cruise/descent → ceiled burn → FOB → alternate/reserve/
   minimum/floor → effective requirement → margin → status), built once in
   `build_fuel_ledger`; every MissionPoint fuel field is a projection of it,
   invariant-tested, asserted per leg in the multi-leg golden test, and the
   fuel-audit caption now shows the burn composition.
3. DONE (v.32) `MissionBriefDocument`: one immutable document per run — wind
   model, brief, hazards-by-FL, focus resolution, fuel-stop legs, chained ETA,
   headline, legal-alternate, forecast quality, and risk computed in a single
   core pass (`build_mission_brief_document`); the UI compute pipeline is one
   call, the signature-introspection compat shims are deleted, and nonstop +
   two-stop assembly goldens pin consistency. (Printable nav log: future,
   trivial now that the document exists.) Fuel-stop ground time defaults 30 min.
4. Feed adapters with recorded live-response fixtures and contract tests.
5. `Sourced[T]` provenance wrapper (value, source, issue time, validity,
   fallback flag) rendered generically.
6. Package split: `weather_core.py` → feeds / wind / performance / mission;
   `streamlit_app.py` → `main()` + per-tab renderers.

## Working rules
- The upstream CODEX repo is read-only reference; never push changes there.
- Keep the 157+ test suite green and ruff clean at every commit; bump
  `RELEASE_VERSION` on every main push (CI enforces).
- Public repo: the copyrighted Daher PIM PDF is NOT distributed here and must
  never be committed. Performance data ships as the validated JSON snapshot;
  rebuilding the snapshot requires the owner's local PDF copy. If this app
  later replaces the upstream deployment, the repo returns to private.
