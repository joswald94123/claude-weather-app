# Repo Startup Instructions

- This is **claude-weather-app**, the parallel fork of CODEX-Weather-Brief. Read
  `CLAUDE_ROADMAP.md` for the plan and current phase status; `APP_RECAP.md` and
  `prior/` are inherited upstream history only.
- **No mission arithmetic in the UI layer.** `streamlit_app.py` and
  `ui_presenters.py` must not compute or re-derive fuel, time, distance, wind,
  or risk quantities — they render values the core modules (`wxcore`,
  `route_planning`, `performance_profiles`, `tail_profiles`) already computed.
  If a rendering need requires a new number, add it to the core result object
  and test it there.
- The mission core lives in the `wxcore/` package (`models` -> `geo` -> `feeds`
  -> `wind` -> `mission`, imports flow strictly down that chain).
  `weather_core.py` is a re-export shim kept for import compatibility; put new
  core code in the right `wxcore` module, never back in the shim.
- Offline determinism for tests: recorded AWC payloads live in
  `tests/fixtures/` (see its README) and are served by
  `tests/feed_fixtures.py`; the AppTest guardrail runs the whole app against
  them and asserts rendered text equals the computed mission document.
- Every push to `main` must advance `RELEASE_VERSION` (CI enforces), keep
  `python -m pytest -q` green and `ruff check .` clean, and auto-deploys to
  Streamlit Community Cloud.
- The copyrighted Daher PIM PDF is NOT distributed here and must never be
  committed; performance data ships as the validated
  `assets/pim_tables_snapshot.json`.
