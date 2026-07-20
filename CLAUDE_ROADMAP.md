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

## Phase 1 — remaining P1 correctness (FIX chunks)
1. FIX-06 item 1: descent wind/temperature geographic mirroring (both integrators) + gradient regression test.
2. FIX-04: multi-leg hero/FOB/mission-risk truthfulness (worst per-leg basis; nonstop-matrix caption).
3. FIX-01 items 2–3: partial-failure cache TTL; release-keyed cache keys.
4. FIX-02 remainder: CWA band swap / UCWA-in-cwaText / qualifier severity; VV-cover units; vertVis in forecast-quality; window constants.
5. FIX-03: shared G-AIRMET horizon fallback for table + vertical profile.

## Phase 2 — policy, validation, hardening
- FIX-07: reserve-policy label (DECIDED: 60-gal floor protects the intended
  destination touchdown, diversion excluded — formula unchanged); 292-gal
  usable-capacity validation; wind-sign direction label; chained-leg
  uncovered-bin geography.
- FIX-08: RELEASE_VERSION guarded read; FAA failure-path tests; clean 3.13 lock
  regen; unknown-hazard-type rendering; workflow timeouts.
- FIX-05 remainder: spinner scope; widget/session-state patterns; risk-reason
  attribution and escalation wording; dead code.

## Phase 3 — structural convergence (FIX-09 → brief-as-document)
1. FIX-09: extract the fuel-stop orchestration engine into core
   (`build_multi_leg_plan` returning a frozen result); UI renders only;
   golden two-stop scenario tests; "no mission arithmetic in the UI layer" rule.
2. `FuelLedger` typed value object carrying the full fuel derivation
   (start → taxi → phases → FOB → requirement components → margin) consumed by
   every fuel surface.
3. `MissionBriefDocument`: one immutable computed document per run (all legs,
   FLs, hazards, provenance); enables a printable nav log.
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
