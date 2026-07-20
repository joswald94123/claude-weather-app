# AVWX Future Enhancement Assessment

## Decision

Use AVWX first as a provenance-visible secondary parser, station-discovery service, and optional
airport/runway metadata source. Do not silently merge or replace NOAA/AWC weather with AVWX data.
AVWX states that its METAR and TAF reports are sourced from NOAA ADDS plus localized sources, so
agreement is useful evidence of parsing and station-selection correctness but is not always an
independent confirmation of the underlying observation.

## Current Integration

The app calls `GET /station/{ident}` only when `AVWX_API_TOKEN` is configured. A successful response
can override airport coordinates, timezone, and elevation; otherwise the app falls back to its
built-in airport database. NOAA/AWC remains the weather source and FAA NASR remains the route-fix
source.

Authentication should continue to use the `Authorization` header. Never put the token in a URL,
log, repository file, cached response, or user-visible error. Root-level Streamlit secrets expose
`AVWX_API_TOKEN` to the running process.

## Capability Priorities

| Priority | AVWX capability | Proposed app use | Important limitation |
|---|---|---|---|
| P1 | `/metar/{location}` and `/taf/{location}` | Shadow-parse terminal reports and show field-level discrepancies against NOAA/AWC | Often shares NOAA-origin data; this validates parsing more than source independence |
| P1 | `/station/{ident}`, `/station/near/{coord}`, `/path/station` | Validate station selection, identify nearby reporting stations, and enrich airport/runway context | The enhanced aviowiki airport dataset may require a paid add-on |
| P2 | `/path/metar`, `/path/taf`, `/path/airsigmet` | Audit whether the existing route sampling missed relevant stations or advisories | Route results must retain AVWX timestamps, resolved coordinates, and distance settings |
| P2 | `/pirep/{location}` and `/airsigmet` | Cross-check structured parsing of icing, turbulence, altitude, and polygons | Likely overlaps existing upstream feeds and must not duplicate hazards |
| P2 | `/notam/{location}` and `/path/notam` | Add a separate operational-advisory view for runway, approach, service, and airspace notices | Must never be presented as a substitute for an official FAA briefing or authoritative NOTAM review |
| P3 | `/nbm/nbh`, `/nbm/nbs`, `/nbm/nbe` | Add hourly through extended terminal trend context beyond TAF coverage | NBM is model guidance, not an observation or TAF; keep it visually and logically separate |
| Backlog | `/summary/{location}` | Compact diagnostic comparison during development | Too lossy to replace the app's existing detailed risk logic |
| Avoid | GFS MOS endpoints | None for new work | AVWX documentation says NOAA is retiring these products and recommends NBM |

The multiple-report endpoint can request up to ten METAR or TAF stations per call and may reduce
quota use for missions with several stops and alternates. Exact endpoint access, daily limits, and
whether responses are live or samples depend on the token's plan and must be detected before design
assumptions are finalized.

## Safe Comparison Model

For each AVWX comparison, retain these fields independently from the NOAA record:

- provider, endpoint, request time, response timestamp, and cache timestamp;
- requested station and the actual station/coordinates AVWX selected;
- raw/sanitized bulletin and bulletin observation/issue time;
- parsed flight rules, ceiling, visibility, wind, gust, weather, and applicable TAF periods;
- whether AVWX returned live, cached, nearest-station, or sample/test data;
- plan/quota error state and response age.

Compare only like-for-like bulletins. If raw report identity or issue time differs, report a source-age
or bulletin-selection difference before comparing parsed fields. A disagreement should become a
visible `Source discrepancy` advisory; neither source should silently win. Existing mission-risk
scores should remain NOAA-based until discrepancy behavior has been calibrated with real cases.

## Proposed Delivery Stages

### A. Capability and entitlement probe

- Add a cached server-side probe for station, METAR, TAF, path, PIREP, AIR/SIGMET, NOTAM, and NBM.
- Record HTTP status, live-versus-sample metadata, rate-limit/quota evidence, and response schema.
- Show a private diagnostics table without exposing the token or full authorization header.
- Acceptance: unsupported or paid-only endpoints degrade cleanly and do not affect NOAA confidence.

### B. Terminal shadow validation

- Fetch AVWX METAR/TAF for departure, destination, fuel stops, and alternates after NOAA succeeds.
- Match reports by station and issue/observation time before comparing parsed fields.
- Add tests for identical reports, parser disagreement, different bulletin age, nearest-station
  substitution, AVWX outage, cached response, and sample/test response.
- Acceptance: the mission calculation is unchanged; discrepancies are explicit and attributable.

### C. Station and runway enrichment

- Add nearby-reporting-station suggestions when a selected airport does not report weather.
- Evaluate aviowiki runway/airport fields against FAA NASR before enabling them operationally.
- Acceptance: every displayed field includes source and freshness; FAA/AVWX disagreement is visible.

### D. Route coverage audit

- Compare AVWX path-selected stations and AIR/SIGMET intersections with the app's route sampling.
- Use the result to flag coverage gaps, not to duplicate hazards already identified from AWC.
- Acceptance: deterministic de-duplication by bulletin identity, validity window, and geometry.

### E. Optional NOTAM and NBM modules

- Put NOTAMs in a distinct operational-advisory section with effective/expiry filtering.
- Put NBM guidance in a distinct model-guidance section beyond the TAF horizon.
- Acceptance: prominent source/type labels and explicit non-substitution language for FAA briefing
  and official weather products.

## Operational Requirements

- Default AVWX failure mode: `error`, not stale cache or nearest station, during validation. If a later
  feature deliberately uses `cache` or `nearest`, surface that fact and the age/distance.
- Cache responses by endpoint, normalized parameters, and requested bulletin window; never by token.
- Bound concurrency and calls per rerun, prefer multi-station requests where supported, and expose
  quota exhaustion separately from weather confidence.
- Maintain independent feed-health records so an AVWX failure cannot downgrade otherwise healthy
  NOAA/AWC data.
- Do not add AVWX-derived values to fuel, time, legal-alternate, or hazard calculations until shadow
  comparisons demonstrate stable semantics and dedicated regressions cover the promotion.

## Sources Reviewed

- User-supplied API Blueprint: `C:\Users\JackOswald\OneDrive - ISOThrive Inc\ScanSnap\avwx.apib`
  (reviewed 2026-07-19).
- AVWX Apiary documentation: <https://avwx.docs.apiary.io/>
- AVWX account getting-started and plan behavior: <https://account.avwx.rest/>
- AVWX API source repository and upstream-data description:
  <https://github.com/avwx-rest/avwx-api>
