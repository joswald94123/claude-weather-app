# Recorded AWC feed fixtures

Unedited responses captured from `https://aviationweather.gov/api/data`
on 2026-07-21Z for the KSTS / KBFL / KFFZ station set. They are served to
tests by `tests/feed_fixtures.py` so the full weather pipeline — and the
whole app under Streamlit AppTest — runs offline and deterministically
against the feed's real wire format.

| File | Endpoint and query |
| --- | --- |
| `metar.json` | `metar?ids=KSTS,KBFL,KFFZ&format=json` |
| `taf.json` | `taf?ids=KSTS,KBFL,KFFZ&format=json` |
| `windtemp_sfo.txt` | `windtemp?region=sfo&level=low&fcst=06` |
| `windtemp_slc.txt` | `windtemp?region=slc&level=low&fcst=06` |
| `gairmet.json` | `gairmet?format=json` |
| `airsigmet.json` | `airsigmet?format=json` |
| `tcf.json` | `tcf?format=geojson` |
| `cwa.json` | `cwa?format=geojson` (no CWAs were active — the empty FeatureCollection is itself a valid feed state) |
| `pirep.json` | `pirep?format=json&bbox=30,-125,42,-110` |

To recapture, fetch each URL above and overwrite the file verbatim (no
editing — the point is the feed's reality, not ours). Keep the station
set aligned with `FIXTURE_AIRPORTS` in `tests/feed_fixtures.py`, then
run the suite: the contract tests only assert invariants that any honest
capture satisfies.
