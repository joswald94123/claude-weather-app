# Claude Weather App

Streamlit app for TBM 960 mission planning with live NOAA aviation weather, route wind interpolation, and segment-based hazard scoring.

> Parallel implementation forked from CODEX-Weather-Brief at `28b2dc9`
> (v2026.07.20.13) on 2026-07-20. The upstream project continues independently;
> this repo evolves per `CLAUDE_ROADMAP.md`. Deployed as **claude-weather-app**
> on Streamlit Community Cloud.
>
> **Disclaimer:** personal flight-planning aid for one owner's TBM 960 — not an
> official Daher product and not for operational or navigational use. The
> copyrighted Daher PIM PDF is **not** distributed in this repo; performance
> values load from `assets/pim_tables_snapshot.json`, which records the SHA-256
> of the owner's source manual for provenance.

## What The App Uses As Inputs

User-entered inputs:

- departure ICAO
- destination ICAO
- cruise mode
- climb schedule
- descent rate/profile selection
- cruise flight level selection
- ETD date and time in the departure airport timezone
- fuel load
- landing minimum fuel

Derived inputs:

- route direction (`eastbound` or `westbound`) from the airport coordinates
- route-aware flight level set
  - eastbound: `FL190`, `FL210`, `FL230`, `FL250`, `FL270`, `FL290`, `FL310`
  - westbound: `FL200`, `FL220`, `FL240`, `FL260`, `FL280`, `FL300`
- NOAA FD windtemp region inferred from the route midpoint
- route-context state outlines from the local U.S. Census 2023 20m state boundary KML asset

Local reference docs:

- FAA PHAK chapters 8/11 and the FAA Aircraft Dynamics Model (public-domain FAA
  handbooks; not vendored here — see faa.gov links in the upstream project)

## Airport Data Source

Airport coordinates, timezone, and elevation come from the local `airportsdata` dataset by default, so the app does not need an online airport lookup in normal operation.

If `AVWX_API_TOKEN` is provided, AVWX is used first as an optional override source for airport metadata.

Those airport values drive:

- route distance
- route direction
- departure and arrival timezone handling
- climb and descent altitude calculations
- route segment sample locations
- wind interpolation sample geometry

## What The App Pulls From Online Sources

The app currently fetches these live NOAA/AviationWeather feeds:

1. `metar`
   - Query: departure and destination ICAOs
   - Used for raw METAR display and translated local/Zulu summaries

2. `taf`
   - Query: departure and destination ICAOs
   - Used for raw TAF display and translated local/Zulu summaries

3. `windtemp`
   - Query: inferred region, `level=low`, `fcst=06`
   - Used to build route wind estimates by flight level and route segment
   - Used to derive forecast temperature deviations from ISA for official Daher cruise/climb table interpolation

4. `gairmet`
   - Query: full current JSON feed
   - Used for icing and turbulence hazard polygons and altitude bands

5. `airsigmet`
   - Query: full current JSON feed
   - Used for convective, turbulence, and icing hazard polygons and altitude bands

6. `tcf`
   - Query: full current GeoJSON feed
   - Used for convective polygon coverage and tops

Important detail:

- METAR and TAF are queried directly for the selected departure/destination airports.
- Windtemp is not queried by airport pair directly; it is queried by NOAA region inferred from the route midpoint.
- Hazard feeds are fetched globally, then filtered in-app against the route, altitude band, and ETD/segment reference times.

## How ETD Is Used

ETD affects several parts of the app:

- the displayed departure clock time
- ETA values in departure and arrival local time
- timezone abbreviations such as `PST` vs `PDT`
- hazard validity checks
- cache invalidation for the NOAA weather bundle

Hazard timing is ETA-aware by segment:

- the app computes midpoint reference times along the route for each flight level
- each hazard area is checked against the segment midpoint time, not just the departure time

## How Each Displayed Mission Value Is Calculated

### Distance

- Formula: spherical law of cosines
- Earth radius: `3440.06 NM`
- Source: departure/destination coordinates
- Display: truncated integer nautical miles

### Route Direction

- Computed from wrapped longitude delta
- Negative wrapped delta = westbound
- Non-negative wrapped delta = eastbound

### Flight Levels Shown

- The table does not always show every level in `FLIGHT_LEVELS`
- It shows only the route-appropriate eastbound or westbound subset

### Wind

Primary path:

- NOAA FD windtemp text is parsed into station/altitude wind points
- Station coordinates are resolved from the airport dataset
- Winds are converted from direction/speed into `u/v` vector components
- For a requested altitude:
  - wind is interpolated vertically between surrounding windtemp altitudes
- For each route segment midpoint:
  - the app finds nearby stations with usable altitude data
  - it takes up to the 4 nearest stations
  - it inverse-distance-weights them with `1 / (distance_nm + 20)^2`
  - it projects the weighted wind vector onto the route track
- During climb and descent, the app samples wind along the route using the evolving 3D position of the aircraft, not just one phase-average wind

Fallback path:

- If usable windtemp coverage is not sufficient, the app falls back to the internal heuristic model

Displayed mission wind:

- `MissionPoint.wind_knots` is the time-weighted average wind for that flight level
- Positive = tailwind
- Negative = headwind

### ETE

For each flight level the app builds a climb/cruise/descent profile.

- The app uses the built-in official Daher TBM 960 PIM profile as the current performance baseline
- Those hard-coded baseline tables are intentionally sourced from official Daher manuals and are expected to remain the durable default profile
- the repo vendors the Daher PIM PDF and parses the official source tables locally
- cruise TAS and cruise fuel flow are interpolated by flight level from the selected cruise mode and forecast ISA deviation when NOAA temperatures are available
- climb performance is selected from the published Daher climb schedule family and interpolated across the official ISA-deviation tables when forecast temperatures are available
- cruise and climb calculations can use the selected published Daher source-weight columns instead of being locked to the default baseline weights
- descent performance is selected from the published Daher descent profile/rate columns
- climb and descent IAS values are converted to TAS with a standard-atmosphere density model
- climb and descent are integrated band-by-band using the aircraft's evolving route position and altitude
- groundspeed uses the route-track wind vector, so headwind/tailwind and crosswind are resolved from the actual interpolated wind direction at each sampled point
- remaining mission distance is assigned to cruise
- cruise distance is split across the configured segment count
- each cruise segment time uses crosswind-corrected along-track groundspeed at that segment's actual route location
- total ETE = climb hours + cruise segment hours + descent hours

If climb plus descent distance exceeds total mission distance:

- the app scales climb and descent time/distance down proportionally
- cruise distance becomes zero

### ETA Arrival And ETA Departure

- `ETA = ETD + total ETE`
- one ETA is displayed in the arrival airport timezone
- one ETA is displayed in the departure airport timezone

### Fuel Burn

- climb fuel is summed from the profile climb table by altitude band
- cruise fuel is `segment_hours * interpolated cruise fuel flow`
- descent fuel is summed from the profile descent table by altitude band
- a small fixed trip add-on from the profile is added once per mission

### Fuel At Destination

- `fuel_at_dest = start_fuel_gal - fuel_burn` is gross FOB at touchdown; alternate and reserve fuel are not subtracted from it
- post-destination fuel planning also estimates:
  - alternate fuel from the entered alternate distance
  - final reserve fuel from the entered reserve minutes
  - final landing minimum as an operator destination-arrival floor
- `effective_requirement = max(alternate_fuel + reserve_fuel, final_landing_minimum, pilot_reserve_floor)`
- `reserve_margin_gal = fuel_at_dest - effective_requirement`

### Hazard Columns

Hazard scoring uses live NOAA polygon products:

- G-AIRMET
- AIRSIGMET
- TCF
- CWA
- PIREP/AIREP point reports

Normalization:

- hazard records are converted into common `HazardArea` entries
- each entry stores:
  - hazard type
  - severity score
  - altitude band
  - polygon geometry
  - source label
  - valid-from and valid-to times when available

Per-flight-level route scoring:

- the route starts with `12` base horizontal segments, then adds route waypoints and climb/descent transition breakpoints
- each segment has:
  - interval geometry along the route
  - an interval midpoint reference time
  - a traversed altitude band derived from climb/cruise/descent geometry
- a hazard intersects a segment only if:
  - the hazard is valid at that segment reference time
  - the segment altitude band overlaps the hazard altitude band
  - the route interval enters or crosses one of the hazard polygons

Severity:

- `0 = None`
- `1 = Low`
- `2 = Moderate`
- `3 = High`

Displayed summary values:

- each category column shows the highest severity seen on that route at that flight level
- the summary also includes how many adaptive route bins were impacted
- the segment detail table shows per-segment category scores and source labels

### Mission Posture

- the composite posture keeps severity and data confidence separate
- severity combines terminal METAR/TAF risk, route hazards, fuel reserve margin, and failed feed health
- confidence is still derived from NOAA feed health
- GFA/FIP/GTG gridded layers were evaluated, but AWC's public Data API product list does not currently expose a direct ingestion endpoint for those layers

## Wind Source Behavior

The app makes wind provenance explicit:

- if NOAA wind interpolation succeeds, the UI shows station/sample counts
- if it does not, the UI warns that the mission winds are using the heuristic fallback

## Run Locally

```bash
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

`requirements.in` contains maintainable production ranges. `requirements.lock` pins the exact deployed environment, and `requirements.txt` points Community Cloud at that lock. CI and local verification add `requirements-dev.lock`, sourced from `requirements-dev.in`, so pytest and Ruff do not ship in the production environment. Regenerate both locks deliberately after dependency review rather than during deployment.

## Validate Locally

```bash
python -m py_compile streamlit_app.py weather_core.py performance_profiles.py
python -m pytest -q
```

## Project Files

- `streamlit_app.py`: Streamlit UI and orchestration
- `weather_core.py`: airport lookup, NOAA ingestion, wind interpolation, mission calculations, and hazard logic
- `performance_profiles.py`: official-performance profile builder and interpolation helpers driven by the extracted Daher PIM tables
- `daher_pim_tables.py`: parser/extraction layer for the vendored Daher PIM source tables
- `route_context_map.py`: static route-context map builder using the local U.S. Census state boundary asset
- `assets/pim_tables_snapshot.json`: validated Daher PIM performance tables (the copyrighted source PDF is not distributed; the snapshot stores its SHA-256 for provenance)
- `assets/cb_2023_us_state_20m.zip`: official U.S. Census 2023 20m KML state boundaries used for the route-context map
- `tests/test_weather_core.py`: unit tests
- `tests/test_daher_pim_tables.py`: Daher table extraction tests
- `tests/test_route_context_map.py`: route map tests
- `APP_RECAP.md`: current state, gaps, and next-step planning

## Current Limitations

- The built-in aircraft profiles are intentionally hard-coded from official Daher manuals; custom profile persistence/import for optional alternate profiles is not implemented yet
- The default built-in profile now interpolates official ISA-deviation and selected source-weight tables, but custom tail-specific weight/profile persistence is not implemented yet
- The UI does not yet expose direct torque/power targeting beyond the published Daher cruise-mode families
- Windtemp is currently requested with `level=low` and an ETD-based `fcst` cycle
- GFA/FIP/GTG gridded layers are not ingested because no public AWC Data API endpoint is listed for them in the current docs

## Deploy To Streamlit Community Cloud

The visible release uses calendar versioning: `vYYYY.MM.DD.N`, where `N` is the deployment sequence for that date. The short Git commit remains visible after the release number for exact build traceability.

Before every push to `main` that will deploy the app:

```bash
python scripts/bump_release_version.py
git add RELEASE_VERSION
```

The script increments the same-day sequence or starts at `.1` on a new date. CI rejects a `main` push that does not advance `RELEASE_VERSION`, ensuring every Streamlit auto-deployment receives a new visible version.

Initial Streamlit Community Cloud setup:

1. Choose this GitHub repo and branch `main`.
2. Set **Main file path** to `streamlit_app.py`.
3. Deploy. Subsequent accepted pushes to `main` deploy automatically with their new release version.

No API key is required for airport lookup by default.
If you want AVWX as an override source, set `AVWX_API_TOKEN` in Streamlit app secrets.

FAA route identifiers normally resolve from the active online NASR cycle. If the FAA service is
unavailable, the app falls back to a complete two-cycle bundle containing the effective and preview
airport/navaid/fix datasets. It selects the offline cycle by the planned flight date and labels the
waypoint source with that snapshot's effective date. Refresh both cycles from official FAA data with
`python scripts/build_faa_nasr_fallback.py`.

The `Refresh FAA NASR fallback` GitHub workflow checks the FAA index every Thursday. It does nothing
when the effective/preview dates are unchanged; when either advances, it rebuilds the bundle, runs
the complete test suite, bumps the release, and opens or updates a reviewable refresh pull request.
