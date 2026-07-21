"""Parse recorded live AWC payloads end to end and pin the wire-format contract.

Every prior hazard-parsing defect (NEG PIREPs scored as hazards, EXTM mapped
Low, numeric CWA seriesId, feet-vs-hundreds altitudes) was a wrong assumption
about the live feed's shape. These tests run each captured payload through the
real fetch pipeline so a parser change that disagrees with the feed — or an
upstream schema change after a fixture recapture — fails loudly here.
"""

from __future__ import annotations

import pytest

from feed_fixtures import FIXTURE_AIRPORTS, FixtureFeedSession, load_fixture_json
from route_vertical_profile import KNOWN_HAZARD_TYPES
from weather_core import fetch_noaa_weather


@pytest.fixture(scope="module")
def fixture_weather():
    """Fetch once per module; the session is deterministic so sharing is safe."""

    return fetch_noaa_weather(
        list(FIXTURE_AIRPORTS),
        windtemp_region="sfo,slc",
        session=FixtureFeedSession(),
    )


def test_every_fixture_airport_parses_a_current_metar(fixture_weather):
    """Recorded METARs must yield raw text, a flight category, and a summary."""

    for icao in FIXTURE_AIRPORTS:
        airport = fixture_weather.airports[icao]
        assert airport.metar_raw, icao
        assert airport.flight_category in {"VFR", "MVFR", "IFR", "LIFR"}, icao
        assert airport.metar_summary, icao
        assert airport.metar_observed_at_utc is not None, icao
    assert any(fixture_weather.airports[icao].taf_raw for icao in FIXTURE_AIRPORTS)


def test_windtemp_products_decode_into_plausible_station_points(fixture_weather):
    """Both FD regions must decode into physically plausible wind/temp points."""

    points = fixture_weather.windtemps
    assert len(points) > 100
    stations = {point.station for point in points}
    assert "SFO" in stations
    for point in points:
        assert 1000 <= point.altitude_ft <= 45000, point
        if point.direction_deg is not None:
            assert 0 <= point.direction_deg <= 360, point
        if point.speed_kt is not None:
            assert 0 <= point.speed_kt <= 300, point
        if point.temperature_c is not None:
            assert -80 <= point.temperature_c <= 50, point
        assert point.raw_code.strip(), point


def test_windtemp_fetch_issues_one_request_per_region():
    """A joined region list must fan out into one recorded request per region."""

    session = FixtureFeedSession()
    fetch_noaa_weather(list(FIXTURE_AIRPORTS), windtemp_region="sfo,slc", session=session)
    windtemp_regions = [
        str(params["region"])
        for url, params in session.calls
        if url.endswith("/windtemp") and params
    ]
    assert sorted(windtemp_regions) == ["sfo", "slc"]


def test_recorded_hazard_feeds_map_into_known_renderable_types(fixture_weather):
    """Every hazard parsed from the live capture must carry renderable fields."""

    hazards = fixture_weather.hazard_areas
    assert hazards
    for hazard in hazards:
        assert hazard.hazard_type in KNOWN_HAZARD_TYPES, hazard.source
        assert 1 <= hazard.severity_score <= 5, hazard.source
        assert hazard.top_ft > hazard.base_ft >= 0, hazard.source
        assert hazard.polygons and all(len(polygon) >= 3 for polygon in hazard.polygons), hazard.source
        assert hazard.source.strip(), hazard
    parsed_types = {hazard.hazard_type for hazard in hazards}
    assert {"convective", "turbulence", "icing"} <= parsed_types


def test_feed_statuses_report_the_capture_honestly(fixture_weather):
    """No fixture-backed feed may report failed; only the unavailable GFA does."""

    statuses = fixture_weather.feed_statuses
    for name, status in statuses.items():
        if name == "gfa_fip_gtg":
            assert status.status == "failed"
            continue
        assert status.status in {"ok", "empty"}, (name, status.error_message)
    assert statuses["metar"].row_count == len(FIXTURE_AIRPORTS)
    assert statuses["windtemp"].row_count == len(fixture_weather.windtemps)
    assert fixture_weather.data_confidence == "High"


def test_empty_cwa_feature_collection_is_a_valid_feed_state(fixture_weather):
    """A quiet CWA day parses as empty — never as a failure or a phantom hazard."""

    payload = load_fixture_json("cwa.json")
    assert payload.get("features") == []
    assert fixture_weather.feed_statuses["cwa"].status == "empty"
    assert not any("CWA" in hazard.source for hazard in fixture_weather.hazard_areas)
