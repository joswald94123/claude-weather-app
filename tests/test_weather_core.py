"""Regression coverage for mission math, NOAA parsing, and hazard timing behavior."""

import datetime as dt
import threading

import pytest
import weather_core

from performance_profiles import (
    DEFAULT_CRUISE_MODE_ID,
    DEFAULT_PERFORMANCE_PROFILE_ID,
    VerticalPerformanceRow,
    get_performance_profile,
    sample_cruise_performance,
)
from route_planning import RouteWaypoint, build_route_plan, split_route_plan_at_fuel_stops
from weather_core import (
    _along_track_ground_speed,
    _estimate_wind_components,
    _derive_noaa_confidence,
    _ias_to_tas,
    _ias_to_tas_with_temperature,
    _lookup_airport_from_avwx,
    _parse_hazard_areas,
    _taf_period_from_dict,
    _terminal_risk_from_metar_row,
    AirportData,
    AirportWeather,
    FeedStatus,
    HazardArea,
    MissionRiskThresholds,
    NoaaWeather,
    SegmentHazard,
    WindTempPoint,
    build_alternate_range_rings,
    build_mission_risk_summary,
    build_route_vertical_profile,
    build_route_wind_model,
    build_mission_brief,
    cruise_flight_levels_for_direction,
    evaluate_legal_alternate_requirement,
    evaluate_terminal_forecast_quality,
    evaluate_route_hazards,
    fetch_noaa_weather,
    get_airport_data,
    great_circle_distance_nm,
    hazard_label,
    infer_windtemp_region,
    parse_windtemp_text,
    parse_windtemp_product_times,
    select_windtemp_forecast_cycle,
    summarize_segment_hazard,
)


def test_vertical_visibility_scores_as_a_low_ceiling():
    """Verify an obscured-sky vertical visibility is not treated as unlimited ceiling."""

    risk = _terminal_risk_from_metar_row({"vertVis": 400, "cover": "OVX"})

    assert risk is not None
    assert risk.score == 3
    assert "Ceiling 400 ft" in risk.reasons


def test_empty_hazard_feeds_do_not_reduce_otherwise_high_data_confidence():
    """Verify a clear-weather empty advisory feed is not mistaken for missing data."""

    fetched = dt.datetime(2026, 7, 19, tzinfo=dt.timezone.utc)
    statuses = {
        name: FeedStatus(name, name, status, fetched, row_count=1 if status == "ok" else 0)
        for name, status in {
            "metar": "ok",
            "taf": "ok",
            "windtemp": "ok",
            "gairmet": "empty",
            "airsigmet": "empty",
            "tcf": "empty",
            "cwa": "empty",
        }.items()
    }

    assert _derive_noaa_confidence(statuses) == "High"


def test_structured_negative_pirep_and_ict_identifier_do_not_create_false_hazards():
    """Verify smooth rides and the ICT identifier do not trigger turbulence or icing."""

    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={},
        pirep_rows=[
            {
                "lat": 37.65,
                "lon": -97.43,
                "fltLvl": 180,
                "tbInt1": "NEG",
                "rawOb": "UA /OV ICT /FL180 /TB NEG",
            }
        ],
    )

    assert areas == []


def test_clear_sky_taf_has_unlimited_ceiling_for_one_two_three():
    """Verify a decoded FEW-only period does not become an unknown or failing ceiling."""

    eta = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)
    period = _taf_period_from_dict(
        {
            "timeFrom": (eta - dt.timedelta(hours=2)).timestamp(),
            "timeTo": (eta + dt.timedelta(hours=2)).timestamp(),
            "visib": "6+",
            "wspd": 5,
            "clouds": [{"cover": "FEW", "base": 10000}],
        }
    )
    assert period.ceiling_is_unlimited is True
    airport = AirportWeather(
        icao="KSTS",
        metar_raw=None,
        metar_time_utc=None,
        flight_category=None,
        metar_summary=None,
        taf_raw="P6SM FEW100",
        taf_issue_time_utc=None,
        taf_summary=None,
        taf_periods=(period,),
    )
    weather = NoaaWeather(
        airports={"KSTS": airport},
        windtemps=[],
        windtemp_region="sfo",
        windtemp_level="low",
        windtemp_fcst="06",
        hazard_areas=[],
    )

    assessment = evaluate_legal_alternate_requirement(
        weather=weather,
        destination_icao="KSTS",
        eta_utc=eta,
        has_destination_approach=True,
    )

    assert assessment.is_required is False
    assert assessment.worst_ceiling_ft is None


@pytest.mark.parametrize("cover", ["BKN", "OVC", "VV", "OVX"])
def test_undecodable_ceiling_layer_requires_an_alternate(cover):
    """Verify a ceiling-significant layer without a height stays conservatively unknown."""

    eta = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)
    period = _taf_period_from_dict(
        {
            "timeFrom": (eta - dt.timedelta(hours=2)).timestamp(),
            "timeTo": (eta + dt.timedelta(hours=2)).timestamp(),
            "visib": "6+",
            "clouds": [{"cover": cover, "base": None}],
        }
    )
    airport = AirportWeather(
        icao="KSTS",
        metar_raw=None,
        metar_time_utc=None,
        flight_category=None,
        metar_summary=None,
        taf_raw=f"P6SM {cover}///",
        taf_issue_time_utc=None,
        taf_summary=None,
        taf_periods=(period,),
    )
    weather = NoaaWeather(
        airports={"KSTS": airport},
        windtemps=[],
        windtemp_region="sfo",
        windtemp_level="low",
        windtemp_fcst="06",
        hazard_areas=[],
    )

    assessment = evaluate_legal_alternate_requirement(
        weather=weather,
        destination_icao="KSTS",
        eta_utc=eta,
        has_destination_approach=True,
    )

    assert period.ceiling_is_unlimited is False
    assert assessment.is_required is True
    assert assessment.status == "Required"


@pytest.mark.parametrize(
    ("intensity", "expected_score"),
    [
        ("LGT", 1),
        ("MOD", 2),
        ("SEV", 3),
        ("SVR", 3),
        ("EXTM", 3),
        ("MOD-EXTM", 3),
        ("SEV-EXTM", 3),
        ("HVY", 3),
    ],
)
def test_pirep_structured_intensity_vocabulary_scores_conservatively(intensity, expected_score):
    """Verify every supported AWC intensity token maps to its intended hazard severity."""

    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={},
        pirep_rows=[
            {
                "lat": 38.5,
                "lon": -122.8,
                "obsTime": dt.datetime(2026, 3, 6, tzinfo=dt.timezone.utc).timestamp(),
                "altitude": 150,
                "tbInt1": intensity,
                "rawOb": f"UA /OV KSTS /TB {intensity}",
            }
        ],
    )

    assert len(areas) == 1
    assert areas[0].severity_score == expected_score


def test_structured_pirep_uses_intensity_and_reported_altitude_band():
    """Verify structured PIREP fields drive type, severity, base, and top."""

    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={},
        pirep_rows=[
            {
                "lat": 38.5,
                "lon": -122.5,
                "icgInt1": "MOD",
                "icgBas1": 120,
                "icgTop1": 180,
                "rawOb": "UA /OV KSTS /IC MOD 120-180",
            }
        ],
    )

    assert len(areas) == 1
    assert areas[0].hazard_type == "icing"
    assert areas[0].severity_score == 2
    assert areas[0].base_ft == 12000
    assert areas[0].top_ft == 18000


def test_second_structured_pirep_layer_survives_negative_first_layer():
    """Verify a negative first layer cannot suppress a reported hazardous second layer."""

    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={},
        pirep_rows=[
            {
                "lat": 38.5,
                "lon": -122.8,
                "fltLvl": 180,
                "icgInt1": "NEG",
                "icgInt2": "MOD",
                "icgBas2": 140,
                "icgTop2": 180,
                "rawOb": "UA /OV KSTS /IC NEG /IC MOD 140-180 /RM LLWS",
            }
        ],
    )

    assert [(area.hazard_type, area.severity_score) for area in areas] == [
        ("icing", 2),
        ("llws", 2),
    ]
    assert areas[0].base_ft == 14000
    assert areas[0].top_ft == 18000


def test_gairmet_forecast_snapshot_has_a_bounded_validity_window():
    """Verify a three-hour forecast snapshot is not smeared across the full product lifetime."""

    issue_time = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)
    areas = _parse_hazard_areas(
        gairmet_rows=[
            {
                "hazard": "TURB-HI",
                "tag": "TANGO",
                "issueTime": issue_time.timestamp(),
                "expireTime": (issue_time + dt.timedelta(hours=12)).timestamp(),
                "forecastHour": 6,
                "coords": [
                    {"lat": 38.0, "lon": -123.0},
                    {"lat": 39.0, "lon": -123.0},
                    {"lat": 39.0, "lon": -122.0},
                ],
            }
        ],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={},
        pirep_rows=[],
    )

    assert len(areas) == 1
    assert areas[0].valid_from_utc == issue_time + dt.timedelta(hours=4, minutes=30)
    assert areas[0].valid_to_utc == issue_time + dt.timedelta(hours=7, minutes=30)


def test_cwa_classification_ignores_unrelated_property_substrings():
    """Verify words such as SERVICE cannot turn a turbulence CWA into icing."""

    polygon = {
        "type": "Polygon",
        "coordinates": [[[-123.0, 38.0], [-122.0, 38.0], [-122.0, 39.0], [-123.0, 38.0]]],
    }
    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={
            "features": [
                {
                    "properties": {"phenom": "TURB", "notice": "SERVICE MESSAGE"},
                    "geometry": polygon,
                }
            ]
        },
        pirep_rows=[],
    )

    assert len(areas) == 1
    assert areas[0].hazard_type == "turbulence"


def test_cwa_numeric_altitudes_are_raw_feet_and_empty_phenomena_are_skipped():
    """Verify CWA units stay product-specific and unknown products do not invent turbulence."""

    polygon = {
        "type": "Polygon",
        "coordinates": [[[-123.0, 38.0], [-122.0, 38.0], [-122.0, 39.0], [-123.0, 38.0]]],
    }
    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={
            "features": [
                {
                    "properties": {"phenom": "TURB", "base": 500, "top": 12000, "seriesId": "UCWA"},
                    "geometry": polygon,
                },
                {"properties": {}, "geometry": polygon},
            ]
        },
        pirep_rows=[],
    )

    assert len(areas) == 1
    assert areas[0].base_ft == 500
    assert areas[0].top_ft == 12000
    assert areas[0].severity_score == 3


def test_temperature_interpolation_uses_the_wind_station_distance_cap():
    """Verify remote FD temperatures cannot influence performance outside NOAA coverage."""

    nearby = weather_core._estimate_temperature_c(
        station_temperature_profiles=[(38.5, -122.8, {30000: -40.0})],
        sample_lat=38.5,
        sample_lon=-122.8,
        altitude_ft=30000,
    )
    remote = weather_core._estimate_temperature_c(
        station_temperature_profiles=[(38.5, -122.8, {30000: -40.0})],
        sample_lat=38.5,
        sample_lon=-112.0,
        altitude_ft=30000,
    )

    assert nearby == -40.0
    assert remote is None


def test_latest_row_coercion_ignores_malformed_scores():
    """Verify one malformed NOAA score cannot abort terminal-feed normalization."""

    latest = weather_core._coerce_latest_rows(
        [{"icaoId": "KSTS", "obsTime": "not-an-int"}],
        score_field="obsTime",
    )

    assert latest["KSTS"]["obsTime"] == "not-an-int"


def test_light_and_variable_windtemp_group_contributes_calm():
    """Verify NOAA 9900 decodes as a usable zero wind vector."""

    assert weather_core._decode_windtemp_group("9900", altitude_ft=9000) == (0, 0, None)


def test_airsigmet_numeric_altitudes_are_raw_feet_not_hundreds():
    """Verify a 500-foot AIRSIGMET base remains 500 feet rather than 50,000."""

    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[
            {
                "hazard": "IFR",
                "altitudeLow1": 500,
                "altitudeHi1": 8000,
                "coords": [
                    {"lat": 38.0, "lon": -123.0},
                    {"lat": 39.0, "lon": -123.0},
                    {"lat": 39.0, "lon": -122.0},
                ],
            }
        ],
        tcf_payload={},
        cwa_payload={},
        pirep_rows=[],
    )

    assert len(areas) == 1
    assert areas[0].base_ft == 500
    assert areas[0].top_ft == 8000


def test_airsigmet_surface_base_of_zero_is_preserved():
    """Verify altitudeLow1=0 (SFC) is kept instead of falling through to altitudeLow2."""

    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[
            {
                "hazard": "TURB",
                "altitudeLow1": 0,
                "altitudeLow2": 24000,
                "altitudeHi1": 24000,
                "coords": [
                    {"lat": 38.0, "lon": -123.0},
                    {"lat": 39.0, "lon": -123.0},
                    {"lat": 39.0, "lon": -122.0},
                ],
            }
        ],
        tcf_payload={},
        cwa_payload={},
        pirep_rows=[],
    )

    assert len(areas) == 1
    assert areas[0].base_ft == 0
    assert areas[0].top_ft == 24000


def _ete_to_minutes(ete_text: str) -> int:
    """Convert mission ETE display text into minutes for assertions."""

    hours_text, minutes_text = ete_text.split("h ")
    return (int(hours_text) * 60) + int(minutes_text.replace("m", ""))


# Lightweight fakes keep NOAA/network tests deterministic and fast.
class _BrokenSession:
    """Test helper for BrokenSession behavior."""

    def get(self, *_args, **_kwargs):
        raise RuntimeError("network down")


class _FakeResponse:
    """Test helper for FakeResponse behavior."""

    def __init__(self, *, json_payload=None, text_payload: str = "", status_code: int = 200):
        self._json_payload = json_payload
        self.text = text_payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        if self._json_payload is None:
            raise ValueError("missing json payload")
        return self._json_payload


class _RoutingSession:
    """Test helper for RoutingSession behavior."""

    def __init__(self, responses: dict[str, _FakeResponse]):
        self._responses = responses
        self.calls: list[tuple[str, dict[str, object] | None, int | None]] = []

    def get(self, url: str, *, params=None, timeout=None, headers=None):
        self.calls.append((url, params, timeout))
        if url.endswith("/cwa") and url not in self._responses:
            return _FakeResponse(json_payload={"features": []}, status_code=204)
        if url.endswith("/pirep") and url not in self._responses:
            return _FakeResponse(json_payload=[], status_code=204)
        if url not in self._responses:
            raise RuntimeError(f"unmapped url: {url}")
        return self._responses[url]


def test_live_noaa_fetches_use_parallel_independent_sessions(monkeypatch):
    """Live requests should overlap without sharing one mutable requests session."""

    state = {"active": 0, "max_active": 0, "created": 0, "closed": 0}
    lock = threading.Lock()
    overlap_observed = threading.Event()

    class ParallelSession:
        def __init__(self):
            with lock:
                state["created"] += 1

        def get(self, url: str, *, params=None, timeout=None):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                if state["active"] >= 2:
                    overlap_observed.set()
            try:
                assert overlap_observed.wait(timeout=1.0)
                if url.endswith("/windtemp"):
                    return _FakeResponse(text_payload="")
                if url.endswith("/tcf") or url.endswith("/cwa"):
                    return _FakeResponse(json_payload={"features": []})
                return _FakeResponse(json_payload=[])
            finally:
                with lock:
                    state["active"] -= 1

        def close(self):
            with lock:
                state["closed"] += 1

    monkeypatch.setattr(weather_core.requests, "Session", ParallelSession)

    weather = fetch_noaa_weather([])

    assert weather.data_confidence == "Medium"
    assert state["max_active"] >= 2
    assert state["created"] == state["closed"]


def test_lookup_uses_builtin_data_without_token():
    """Verify that lookup uses builtin data without token."""

    airport = get_airport_data("ksts")
    assert airport.icao == "KSTS"
    assert airport.source in {"airportsdata", "avwx"}
    assert airport.latitude != 39.8283
    assert airport.longitude != -98.5795


# Basic airport lookup and mission-brief tests verify the app can plan a route at all.
def test_lookup_refuses_unknown_airport():
    """Unknown identifiers must not silently plan from center-of-country coordinates."""

    with pytest.raises(ValueError, match="Airport ZZZZ could not be resolved"):
        get_airport_data("zzzz", session=_BrokenSession())  # type: ignore[arg-type]


def test_great_circle_distance_is_reasonable_for_sfo_to_lax():
    """Verify that great circle distance is reasonable for sfo to lax."""

    # SFO 37.6213,-122.3790 to LAX 33.9416,-118.4085 is ~293 NM
    distance_nm = great_circle_distance_nm(37.6213, -122.3790, 33.9416, -118.4085)
    assert 280 <= distance_nm <= 310


def test_build_mission_brief_generates_all_flight_levels():
    """Verify that build mission brief generates all flight levels."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test")
    eastbound_levels = cruise_flight_levels_for_direction(is_westbound=False)

    brief = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        flight_levels=eastbound_levels,
    )

    assert brief.route_label == "KSTS -> KFFZ"
    assert brief.direction_label.startswith("Eastbound")
    assert len(brief.points) == len(eastbound_levels)
    assert brief.points[0].flight_level == "FL190"
    assert brief.points[-1].flight_level == "FL310"


def test_directional_cruise_levels_match_semicircular_rules():
    """Verify that directional cruise levels match semicircular rules."""

    assert cruise_flight_levels_for_direction(is_westbound=False) == [190, 210, 230, 250, 270, 290, 310]
    assert cruise_flight_levels_for_direction(is_westbound=True) == [200, 220, 240, 260, 280, 300]


def test_return_leg_flips_route_label():
    """Verify that return leg flips route label."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test")

    brief = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=True,
        start_fuel_gal=292,
    )

    assert brief.route_label == "KFFZ -> KSTS"
    assert "Westbound" in brief.direction_label


def test_mission_brief_uses_12_hour_time_format():
    """Verify that mission brief uses 12 hour time format."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test")

    brief = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(13, 5),
        is_return_leg=False,
        start_fuel_gal=292,
    )

    assert brief.departure_zone_time.endswith(("AM", "PM"))
    assert brief.points[0].eta_arrival_zone.endswith(("AM", "PM"))
    assert brief.points[0].eta_departure_zone.endswith(("AM", "PM"))


def test_build_mission_brief_uses_multi_leg_route_distance_and_label():
    """Verify that build mission brief uses multi leg route distance and label."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test")
    # A dogleg route should carry its longer distance and expanded route label into the brief output.
    route_plan = build_route_plan(
        dep,
        arr,
        [RouteWaypoint("OAL", 38.0000, -117.7690, "VOR/DME", "test")],
    )

    brief = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        route_plan=route_plan,
        flight_levels=[310],
    )

    assert brief.route_label == "KSTS -> OAL -> KFFZ"
    assert brief.distance_nm > int(
        great_circle_distance_nm(dep.latitude, dep.longitude, arr.latitude, arr.longitude)
    )


def test_performance_inputs_change_flight_time_and_fuel():
    """Verify that performance inputs change flight time and fuel."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)

    brief_fast = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        climb_rate_fpm=1500,
        descent_rate_fpm=1500,
        cruise_tas_kts=340,
        climb_ias_kts=190,
        descent_ias_kts=190,
    )
    brief_slow = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        climb_rate_fpm=700,
        descent_rate_fpm=700,
        cruise_tas_kts=260,
        climb_ias_kts=130,
        descent_ias_kts=130,
    )

    fast_fl300 = next(point for point in brief_fast.points if point.flight_level == "FL300")
    slow_fl300 = next(point for point in brief_slow.points if point.flight_level == "FL300")

    assert slow_fl300.fuel_burn > fast_fl300.fuel_burn
    assert slow_fl300.fuel_at_dest < fast_fl300.fuel_at_dest


def test_sample_cruise_performance_interpolates_between_defined_rows():
    """Verify that sample cruise performance interpolates between defined rows."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    sample = sample_cruise_performance(
        profile,
        flight_level=190,
        cruise_mode_id="normal",
    )

    assert sample.mode_id == "normal"
    assert sample.mode_label == "Recommended Cruise"
    assert sample.tas_kts == pytest.approx(274.0)
    assert sample.fuel_gph == pytest.approx(59.35)


def test_default_cruise_mode_is_max_cruise():
    """Verify that default cruise mode is max cruise."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)
    default_sample = sample_cruise_performance(profile, flight_level=300)
    max_sample = sample_cruise_performance(profile, flight_level=300, cruise_mode_id="max")

    assert DEFAULT_CRUISE_MODE_ID == "max"
    assert profile.default_cruise_mode_id == "max"
    assert default_sample.mode_id == "max"
    assert default_sample.mode_label == "Max Cruise"
    assert default_sample.tas_kts == pytest.approx(max_sample.tas_kts)
    assert default_sample.fuel_gph == pytest.approx(max_sample.fuel_gph)


def test_ias_to_tas_increases_with_altitude_under_standard_atmosphere():
    """Verify that ias to tas increases with altitude under standard atmosphere."""

    tas_10k = _ias_to_tas(124, 10000)
    tas_20k = _ias_to_tas(124, 20000)

    assert tas_10k > 124
    assert tas_20k > tas_10k


def test_ias_to_tas_with_temperature_uses_forecast_oat():
    """Verify that ias to tas with temperature uses forecast oat."""

    cold_tas = _ias_to_tas_with_temperature(220, 20000, outside_air_temp_c=-30)
    warm_tas = _ias_to_tas_with_temperature(220, 20000, outside_air_temp_c=-10)

    assert warm_tas > cold_tas


def test_crosswind_reduces_along_track_groundspeed():
    """Verify that crosswind reduces along track groundspeed."""

    no_crosswind = _along_track_ground_speed(
        true_airspeed_kts=300,
        tailwind_kts=0,
        crosswind_kts=0,
    )
    heavy_crosswind = _along_track_ground_speed(
        true_airspeed_kts=300,
        tailwind_kts=0,
        crosswind_kts=80,
    )

    assert heavy_crosswind < no_crosswind


def test_profile_modes_change_mission_time_and_fuel():
    """Verify that profile modes change mission time and fuel."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    brief_max = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        flight_levels=[300],
    )
    brief_economy = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="economy",
        flight_levels=[300],
    )

    max_point = brief_max.points[0]
    economy_point = brief_economy.points[0]

    assert _ete_to_minutes(max_point.ete) < _ete_to_minutes(economy_point.ete)
    assert max_point.fuel_burn > economy_point.fuel_burn


# These tests ensure the structured performance profile overrides old manual fallbacks.
def test_profile_inputs_override_manual_performance_values_when_present():
    """Verify that profile inputs override manual performance values when present."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    brief_fast_manual = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        climb_rate_fpm=4000,
        descent_rate_fpm=4000,
        cruise_tas_kts=380,
        climb_ias_kts=250,
        descent_ias_kts=250,
        performance_profile=profile,
        cruise_mode_id="normal",
        flight_levels=[300],
    )
    brief_slow_manual = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        climb_rate_fpm=600,
        descent_rate_fpm=600,
        cruise_tas_kts=220,
        climb_ias_kts=100,
        descent_ias_kts=100,
        performance_profile=profile,
        cruise_mode_id="normal",
        flight_levels=[300],
    )

    assert brief_fast_manual.points[0].ete == brief_slow_manual.points[0].ete
    assert brief_fast_manual.points[0].fuel_burn == brief_slow_manual.points[0].fuel_burn


def test_route_wind_model_from_noaa_points_is_used_in_calculations():
    """Verify that route wind model from noaa points is used in calculations."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)

    tailwind_points = [
        WindTempPoint("SFO", 30000, 290, 90, -45, "dummy"),
        WindTempPoint("LAX", 30000, 290, 90, -45, "dummy"),
        WindTempPoint("LAS", 30000, 290, 90, -45, "dummy"),
        WindTempPoint("PHX", 30000, 290, 90, -45, "dummy"),
    ]
    headwind_points = [
        WindTempPoint("SFO", 30000, 110, 90, -45, "dummy"),
        WindTempPoint("LAX", 30000, 110, 90, -45, "dummy"),
        WindTempPoint("LAS", 30000, 110, 90, -45, "dummy"),
        WindTempPoint("PHX", 30000, 110, 90, -45, "dummy"),
    ]

    tail_model = build_route_wind_model(dep, arr, tailwind_points)
    head_model = build_route_wind_model(dep, arr, headwind_points)

    assert tail_model is not None
    assert head_model is not None
    assert tail_model.source == "NOAA FD windtemp interpolation"
    assert tail_model.station_count == 4
    assert tail_model.usable_sample_count == 8
    assert len(tail_model.station_temperature_profiles) == 4
    assert 300 in tail_model.segment_tailwind_by_fl
    assert len(tail_model.segment_tailwind_by_fl[300]) == 12

    brief_tail = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        wind_model=tail_model,
    )
    brief_head = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        wind_model=head_model,
    )

    tail_fl300 = next(point for point in brief_tail.points if point.flight_level == "FL300")
    head_fl300 = next(point for point in brief_head.points if point.flight_level == "FL300")

    tail_wind = int(tail_fl300.wind_knots.replace("k", ""))
    head_wind = int(head_fl300.wind_knots.replace("k", ""))
    assert tail_wind > head_wind


def test_forecast_temperature_adjusts_profile_performance():
    """Verify that forecast temperature adjusts profile performance."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    isa_points = [
        WindTempPoint("SFO", 24000, 270, 0, -33, "dummy"),
        WindTempPoint("SFO", 34000, 270, 0, -53, "dummy"),
        WindTempPoint("LAX", 24000, 270, 0, -33, "dummy"),
        WindTempPoint("LAX", 34000, 270, 0, -53, "dummy"),
        WindTempPoint("LAS", 24000, 270, 0, -33, "dummy"),
        WindTempPoint("LAS", 34000, 270, 0, -53, "dummy"),
        WindTempPoint("PHX", 24000, 270, 0, -33, "dummy"),
        WindTempPoint("PHX", 34000, 270, 0, -53, "dummy"),
    ]
    warm_points = [
        WindTempPoint("SFO", 24000, 270, 0, -13, "dummy"),
        WindTempPoint("SFO", 34000, 270, 0, -33, "dummy"),
        WindTempPoint("LAX", 24000, 270, 0, -13, "dummy"),
        WindTempPoint("LAX", 34000, 270, 0, -33, "dummy"),
        WindTempPoint("LAS", 24000, 270, 0, -13, "dummy"),
        WindTempPoint("LAS", 34000, 270, 0, -33, "dummy"),
        WindTempPoint("PHX", 24000, 270, 0, -13, "dummy"),
        WindTempPoint("PHX", 34000, 270, 0, -33, "dummy"),
    ]

    isa_model = build_route_wind_model(dep, arr, isa_points)
    warm_model = build_route_wind_model(dep, arr, warm_points)

    brief_isa = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        wind_model=isa_model,
        flight_levels=[300],
    )
    brief_warm = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        wind_model=warm_model,
        flight_levels=[300],
    )

    isa_point = brief_isa.points[0]
    warm_point = brief_warm.points[0]

    assert _ete_to_minutes(warm_point.ete) > _ete_to_minutes(isa_point.ete)
    assert warm_point.fuel_burn < isa_point.fuel_burn


def test_forecast_temperature_changes_descent_profile_distance_from_220_kias_default():
    """Verify that forecast temperature changes descent profile distance from 220 kias default."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    cold_points = [
        WindTempPoint("SFO", 12000, 270, 0, -20, "dummy"),
        WindTempPoint("SFO", 20000, 270, 0, -30, "dummy"),
        WindTempPoint("SFO", 30000, 270, 0, -45, "dummy"),
        WindTempPoint("LAX", 12000, 270, 0, -20, "dummy"),
        WindTempPoint("LAX", 20000, 270, 0, -30, "dummy"),
        WindTempPoint("LAX", 30000, 270, 0, -45, "dummy"),
        WindTempPoint("LAS", 12000, 270, 0, -20, "dummy"),
        WindTempPoint("LAS", 20000, 270, 0, -30, "dummy"),
        WindTempPoint("LAS", 30000, 270, 0, -45, "dummy"),
        WindTempPoint("PHX", 12000, 270, 0, -20, "dummy"),
        WindTempPoint("PHX", 20000, 270, 0, -30, "dummy"),
        WindTempPoint("PHX", 30000, 270, 0, -45, "dummy"),
    ]
    warm_points = [
        WindTempPoint("SFO", 12000, 270, 0, 0, "dummy"),
        WindTempPoint("SFO", 20000, 270, 0, -10, "dummy"),
        WindTempPoint("SFO", 30000, 270, 0, -25, "dummy"),
        WindTempPoint("LAX", 12000, 270, 0, 0, "dummy"),
        WindTempPoint("LAX", 20000, 270, 0, -10, "dummy"),
        WindTempPoint("LAX", 30000, 270, 0, -25, "dummy"),
        WindTempPoint("LAS", 12000, 270, 0, 0, "dummy"),
        WindTempPoint("LAS", 20000, 270, 0, -10, "dummy"),
        WindTempPoint("LAS", 30000, 270, 0, -25, "dummy"),
        WindTempPoint("PHX", 12000, 270, 0, 0, "dummy"),
        WindTempPoint("PHX", 20000, 270, 0, -10, "dummy"),
        WindTempPoint("PHX", 30000, 270, 0, -25, "dummy"),
    ]

    cold_model = build_route_wind_model(dep, arr, cold_points)
    warm_model = build_route_wind_model(dep, arr, warm_points)

    brief_cold = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        wind_model=cold_model,
        flight_levels=[310],
    )
    brief_warm = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        wind_model=warm_model,
        flight_levels=[310],
    )

    assert _ete_to_minutes(brief_warm.points[0].ete) >= _ete_to_minutes(brief_cold.points[0].ete)


def test_profile_schedule_variants_change_mission_results():
    """Verify that profile schedule variants change mission results."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    brief_124 = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        climb_schedule_id="124_kias",
        flight_levels=[300],
    )
    brief_170 = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        climb_schedule_id="170_kias_m0_40",
        flight_levels=[300],
    )
    brief_descent_2500 = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        descent_profile_rate_fpm=2500,
        flight_levels=[300],
    )

    point_124 = brief_124.points[0]
    point_170 = brief_170.points[0]
    point_descent_2500 = brief_descent_2500.points[0]

    assert _ete_to_minutes(point_170.ete) < _ete_to_minutes(point_124.ete)
    assert point_170.fuel_burn <= point_124.fuel_burn
    assert _ete_to_minutes(point_descent_2500.ete) <= _ete_to_minutes(point_124.ete)


def test_composite_climb_time_falls_between_full_124_and_170_schedules():
    """Verify that a 10,000-foot schedule transition affects takeoff-to-landing airborne ETE."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)
    common = dict(
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        flight_levels=[300],
    )

    all_124 = build_mission_brief(dep, arr, climb_schedule_id="124_kias", **common)
    composite = build_mission_brief(
        dep,
        arr,
        climb_schedule_id="124_kias",
        upper_climb_schedule_id="170_kias_m0_40",
        climb_transition_altitude_ft=10000,
        **common,
    )
    all_170 = build_mission_brief(dep, arr, climb_schedule_id="170_kias_m0_40", **common)

    ete_124 = _ete_to_minutes(all_124.points[0].ete)
    ete_composite = _ete_to_minutes(composite.points[0].ete)
    ete_170 = _ete_to_minutes(all_170.points[0].ete)
    assert ete_170 <= ete_composite <= ete_124


def test_startup_taxi_fuel_override_changes_trip_burn_and_destination_fuel():
    """Verify that startup taxi fuel override changes trip burn and destination fuel."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    baseline = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        flight_levels=[300],
    )
    high_taxi = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        fixed_fuel_gal_override=12.0,
        flight_levels=[300],
    )

    baseline_point = baseline.points[0]
    high_taxi_point = high_taxi.points[0]
    assert high_taxi_point.fuel_burn > baseline_point.fuel_burn
    assert high_taxi_point.fuel_at_dest < baseline_point.fuel_at_dest


def test_reserve_and_alternate_fuel_create_destination_margin():
    """Verify that reserve and alternate fuel create destination margin."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    brief = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=180,
        performance_profile=profile,
        cruise_mode_id="max",
        flight_levels=[300],
        alternate_distance_nm=90,
        reserve_minutes=45,
        landing_minimum_gal=60,
    )

    point = brief.points[0]
    assert point.alternate_fuel_gal > 0
    assert point.reserve_fuel_gal > 0
    assert point.calculated_required_landing_fuel_gal == point.alternate_fuel_gal + point.reserve_fuel_gal
    assert point.required_landing_fuel_gal == max(60, point.calculated_required_landing_fuel_gal)
    assert point.reserve_margin_gal == point.fuel_at_dest - point.required_landing_fuel_gal
    assert point.fuel_status in {"Meets reserve", "Meets landing minimum", "Below reserve"}


def test_pilot_reserve_floor_overrides_risk_requirement_without_hiding_calculated_reserve():
    """Verify that pilot reserve floor overrides risk requirement without hiding calculated reserve."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    baseline = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        flight_levels=[300],
        reserve_minutes=45,
        landing_minimum_gal=60,
    ).points[0]
    with_floor = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        flight_levels=[300],
        reserve_minutes=45,
        landing_minimum_gal=60,
        reserve_floor_gal=baseline.required_landing_fuel_gal + 25,
    ).points[0]

    assert baseline.required_landing_fuel_gal == max(60, baseline.calculated_required_landing_fuel_gal)
    assert with_floor.calculated_required_landing_fuel_gal == baseline.calculated_required_landing_fuel_gal
    assert with_floor.required_landing_fuel_gal == baseline.required_landing_fuel_gal + 25
    assert with_floor.reserve_margin_gal == baseline.reserve_margin_gal - 25
    assert with_floor.fuel_status == "Meets pilot floor"


def test_gross_weight_selection_changes_pim_performance_samples():
    """Verify that gross weight selection changes pim performance samples."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    light = sample_cruise_performance(profile, flight_level=300, cruise_mode_id="max", weight_lb=5500)
    heavy = sample_cruise_performance(profile, flight_level=300, cruise_mode_id="max", weight_lb=7300)

    assert light.tas_kts != heavy.tas_kts

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    light_brief = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        cruise_weight_lb=5500,
        climb_weight_lb=5794,
        flight_levels=[300],
    )
    heavy_brief = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        performance_profile=profile,
        cruise_mode_id="max",
        cruise_weight_lb=7300,
        climb_weight_lb=7615,
        flight_levels=[300],
    )

    assert light_brief.points[0].fuel_burn != heavy_brief.points[0].fuel_burn


def test_build_route_vertical_profile_exposes_hazard_altitude_spans():
    """Verify that build route vertical profile exposes hazard altitude spans."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KPSP", 33.8297, -116.5070, "US/Pacific", "test", elevation_ft=477.0)
    route_hazard = HazardArea(
        hazard_type="turbulence",
        severity_score=2,
        base_ft=24000,
        top_ft=36000,
        polygons=[
            [
                (32.0, -124.5),
                (40.5, -124.5),
                (40.5, -115.0),
                (32.0, -115.0),
            ]
        ],
        source="Test turbulence band",
    )

    vertical_profile = build_route_vertical_profile(
        dep,
        arr,
        hazard_areas=[route_hazard],
        reference_time_utc=dt.datetime(2026, 3, 7, 18, 45, tzinfo=dt.timezone.utc),
        flight_level=310,
    )

    assert vertical_profile.flight_level == 310
    assert vertical_profile.cruise_altitude_ft == 31000
    assert vertical_profile.path_points[0].altitude_ft == pytest.approx(129, abs=150)
    assert vertical_profile.path_points[-1].altitude_ft == pytest.approx(477, abs=150)
    assert vertical_profile.hazard_spans
    assert vertical_profile.hazard_spans[0].hazard_type == "turbulence"
    assert vertical_profile.hazard_spans[0].base_ft == 24000
    assert vertical_profile.hazard_spans[0].top_ft == 36000


def test_build_route_vertical_profile_exposes_intermediate_waypoint_markers():
    """Verify that build route vertical profile exposes intermediate waypoint markers."""

    dep = AirportData("KPHX", 33.4342, -112.0116, "US/Arizona", "test", elevation_ft=1135.0)
    arr = AirportData("KSFO", 37.6188, -122.3750, "US/Pacific", "test", elevation_ft=13.0)
    # The vertical-profile model should emit cumulative-distance markers for intermediate fixes.
    route_plan = build_route_plan(
        dep,
        arr,
        [RouteWaypoint("PMD", 34.6294, -118.0844, "VORTAC", "test")],
    )

    vertical_profile = build_route_vertical_profile(
        dep,
        arr,
        hazard_areas=[],
        reference_time_utc=dt.datetime(2026, 3, 7, 18, 45, tzinfo=dt.timezone.utc),
        flight_level=300,
        route_plan=route_plan,
    )

    assert vertical_profile.waypoint_markers
    assert vertical_profile.waypoint_markers[0].identifier == "PMD"
    assert 0.0 < vertical_profile.waypoint_markers[0].distance_nm < vertical_profile.mission_distance_nm


def test_parse_windtemp_text_decodes_columns_and_speed_encoding():
    """Verify that parse windtemp text decodes columns and speed encoding."""

    raw = """
(Extracted from TEST)
FT  3000    6000   9000  30000
SFO 3520 0131+05 3631+00 731960
"""
    points = parse_windtemp_text(raw)
    point_map = {(p.station, p.altitude_ft): p for p in points}

    low = point_map[("SFO", 3000)]
    assert low.direction_deg == 350
    assert low.speed_kt == 20
    assert low.temperature_c is None

    mid = point_map[("SFO", 6000)]
    assert mid.direction_deg == 10
    assert mid.speed_kt == 31
    assert mid.temperature_c == 5

    high = point_map[("SFO", 30000)]
    assert high.direction_deg == 230
    assert high.speed_kt == 119
    assert high.temperature_c == -60


def test_invalid_windtemp_direction_is_rejected():
    """Verify a decoded direction outside 010-360 cannot enter route interpolation."""

    points = parse_windtemp_text("FT  3000\nSFO 9040\n")

    assert len(points) == 1
    assert points[0].direction_deg is None
    assert points[0].speed_kt is None


def test_long_route_requests_departure_midpoint_and_destination_wind_regions():
    """Verify a transcontinental route no longer relies on one midpoint FB region."""

    departure = AirportData("KLAX", 33.9425, -118.4081, "US/Pacific", "test")
    destination = AirportData("KJFK", 40.6413, -73.7781, "US/Eastern", "test")

    regions = infer_windtemp_region(departure, destination).split(",")

    assert regions[0] == "sfo"
    assert regions[-1] == "bos"
    assert len(regions) >= 3


def test_wind_interpolation_refuses_stations_beyond_coverage_cap():
    """Verify a remote station cannot silently supply authoritative route winds."""

    components = _estimate_wind_components(
        station_profiles=[(10.0, 10.0, {30000: (20.0, 0.0)})],
        sample_lat=0.0,
        sample_lon=0.0,
        track_deg=90.0,
        altitude_ft=30000,
    )

    assert components is None


def test_parse_windtemp_product_times_handles_midnight_window():
    """Verify FB header provenance resolves issue time and an overnight FOR-USE window."""

    issue, valid_from, valid_to = parse_windtemp_product_times(
        "DATA BASED ON 191200Z\nVALID 191800Z FOR USE 1800-0300Z\nFT 3000\nSFO 3520\n",
        fetched_at_utc=dt.datetime(2026, 7, 19, 13, 0, tzinfo=dt.timezone.utc),
    )

    assert issue == dt.datetime(2026, 7, 19, 12, 0, tzinfo=dt.timezone.utc)
    assert valid_from == dt.datetime(2026, 7, 19, 18, 0, tzinfo=dt.timezone.utc)
    assert valid_to == dt.datetime(2026, 7, 20, 3, 0, tzinfo=dt.timezone.utc)


# NOAA feed tests focus on normalization and failure handling rather than remote availability.
def test_fetch_noaa_weather_merges_metar_taf_and_windtemp():
    """Verify that fetch noaa weather merges metar taf and windtemp."""

    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "obsTime": 1772747220,
                        "rawOb": "METAR KSTS 052147Z 30008KT 10SM CLR 18/04 A3007 RMK AO2",
                        "fltCat": "VFR",
                        "reportTime": "2026-03-05T21:47:00.000Z",
                        "wdir": 300,
                        "wspd": 8,
                        "visib": "10+",
                        "temp": 18.0,
                        "dewp": 4.0,
                    },
                    {
                        "icaoId": "KSTS",
                        "obsTime": 1772754780,
                        "rawOb": (
                            "METAR KSTS 052353Z 02015G27KT 10SM CLR 25/M13 A2988 RMK "
                            "AO2 PK WND 02031/2323 SLP105 T02501133 10256 20222 55003 $"
                        ),
                        "fltCat": "MVFR",
                        "reportTime": "2026-03-06T00:00:00.000Z",
                        "wdir": 20,
                        "wspd": 15,
                        "wgst": 27,
                        "visib": "10+",
                        "temp": 25.0,
                        "dewp": -13.3,
                        "altim": 1012.0,
                        "cover": "CLR",
                        "clouds": [],
                    },
                    {
                        "icaoId": "KFFZ",
                        "obsTime": 150,
                        "rawOb": "FFZ METAR",
                        "fltCat": "VFR",
                        "reportTime": "2026-03-05T22:00:00.000Z",
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "validTimeFrom": 1772733600,
                        "validTimeTo": 1772820000,
                        "rawTAF": "STS TAF",
                        "issueTime": "2026-03-05T17:24:00.000Z",
                        "fcsts": [
                            {
                                "timeFrom": 1772733600,
                                "timeTo": 1772769600,
                                "fcstChange": None,
                                "wdir": 340,
                                "wspd": 12,
                                "wgst": 21,
                                "visib": "6+",
                                "wxString": None,
                                "clouds": [{"cover": "SKC", "base": None}],
                            }
                        ],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(
                text_payload="FT  3000\nSFO 3520\n"
            ),
        }
    )

    weather = fetch_noaa_weather(
        ["ksts", "kffz"],
        windtemp_region="sfo",
        windtemp_level="low",
        windtemp_fcst="06",
        session=session,  # type: ignore[arg-type]
    )

    assert weather.airports["KSTS"].metar_raw is not None
    assert weather.airports["KSTS"].metar_raw.startswith("METAR KSTS 052353Z")
    assert weather.airports["KSTS"].flight_category == "MVFR"
    assert weather.airports["KSTS"].taf_raw == "STS TAF"
    assert weather.airports["KSTS"].metar_summary is not None
    assert "Wind" in str(weather.airports["KSTS"].metar_summary)
    assert "Visibility" in str(weather.airports["KSTS"].metar_summary)
    assert "(2353Z)" in str(weather.airports["KSTS"].metar_summary)
    assert "Automated station with precipitation discriminator (AO2)" in str(
        weather.airports["KSTS"].metar_summary
    )
    assert "Peak wind 020 deg at 31 kt at 23:23Z" in str(weather.airports["KSTS"].metar_summary)
    assert "Sea-level pressure 1010.5 hPa" in str(weather.airports["KSTS"].metar_summary)
    assert "Exact temp/dewpoint 25.0C/-13.3C" in str(weather.airports["KSTS"].metar_summary)
    assert weather.airports["KSTS"].taf_summary is not None
    assert "Forecast periods:" in str(weather.airports["KSTS"].taf_summary)
    assert "Visibility" in str(weather.airports["KSTS"].taf_summary)
    assert "(1724Z)" in str(weather.airports["KSTS"].taf_summary)
    assert weather.airports["KSTS"].metar_risk is not None
    assert weather.airports["KSTS"].metar_risk.label == "Moderate"
    assert "Surface wind/gust 27 kt" in weather.airports["KSTS"].metar_risk.reasons
    assert weather.airports["KSTS"].taf_risk is not None
    assert weather.airports["KSTS"].taf_risk.label == "Low"
    assert weather.airports["KFFZ"].metar_raw == "FFZ METAR"
    assert weather.airports["KFFZ"].taf_raw is None
    assert len(weather.windtemps) == 1
    assert weather.windtemps[0].station == "SFO"
    assert weather.windtemp_region == "sfo"
    assert weather.feed_statuses["metar"].status == "ok"
    assert weather.feed_statuses["taf"].status == "ok"
    assert weather.feed_statuses["windtemp"].status == "ok"
    assert weather.feed_statuses["windtemp"].row_count == 1
    assert any(call[0].endswith("/metar") for call in session.calls)
    assert any(call[0].endswith("/taf") for call in session.calls)
    assert any(call[0].endswith("/windtemp") for call in session.calls)
    pirep_call = next(call for call in session.calls if call[0].endswith("/pirep"))
    assert pirep_call[1] is not None
    assert "bbox" in pirep_call[1]
    assert "id" not in pirep_call[1]


def test_fetch_noaa_weather_tolerates_network_failure():
    """Verify that fetch noaa weather tolerates network failure."""

    weather = fetch_noaa_weather(["KSTS", "KFFZ"], session=_BrokenSession())  # type: ignore[arg-type]

    assert set(weather.airports.keys()) == {"KSTS", "KFFZ"}
    assert weather.airports["KSTS"].metar_raw is None
    assert weather.airports["KSTS"].taf_raw is None
    assert weather.windtemps == []
    assert weather.hazard_areas == []
    assert weather.data_confidence == "Unknown"
    assert all(status.status == "failed" for status in weather.feed_statuses.values())


def test_feed_failures_lower_confidence_without_creating_known_mission_risk():
    """Verify that feed failures lower confidence without creating known mission risk."""

    weather = fetch_noaa_weather(["KSTS"], session=_BrokenSession())  # type: ignore[arg-type]
    safe_point = build_mission_brief(
        AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0),
        AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0),
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        flight_levels=[300],
    ).points[0]

    summary = build_mission_risk_summary(
        weather=weather,
        segment_hazards=[],
        mission_point=safe_point,
        thresholds=MissionRiskThresholds(fuel_caution_margin_gal=0),
    )

    assert summary.score == 0
    assert summary.label == "Clear"
    assert summary.confidence == "Unknown"
    assert any("Failed feeds" in reason for reason in summary.confidence_reasons)


def test_fetch_noaa_weather_tracks_empty_feed_as_available_not_failed():
    """Verify that fetch noaa weather tracks empty feed as available not failed."""

    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(json_payload=[], status_code=204),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(json_payload=[], status_code=204),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload="", status_code=204),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(json_payload=[], status_code=204),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[], status_code=204),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": []}, status_code=204),
        }
    )

    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]

    assert weather.windtemps == []
    assert weather.hazard_areas == []
    data_api_statuses = [
        status for key, status in weather.feed_statuses.items() if key != "gfa_fip_gtg"
    ]
    assert all(status.status == "empty" for status in data_api_statuses)
    assert all(status.is_available for status in data_api_statuses)
    assert weather.data_confidence == "Medium"


def test_empty_pirep_feed_does_not_lower_data_confidence_by_itself():
    """Verify that empty pirep feed does not lower data confidence by itself."""

    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(json_payload=[{}]),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(json_payload=[{}]),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload="FT  3000\nSFO 3520\n"),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(json_payload=[{}]),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[{}]),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": [{}]}),
            "https://aviationweather.gov/api/data/cwa": _FakeResponse(json_payload={"features": [{}]}),
            "https://aviationweather.gov/api/data/pirep": _FakeResponse(json_payload=[], status_code=204),
        }
    )

    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]

    assert weather.feed_statuses["pirep"].status == "empty"
    assert weather.data_confidence == "High"


def test_select_windtemp_forecast_cycle_tracks_requested_etd_window():
    """Verify that select windtemp forecast cycle tracks requested etd window."""

    now = dt.datetime(2026, 4, 24, 12, 0, tzinfo=dt.timezone.utc)

    assert select_windtemp_forecast_cycle(now + dt.timedelta(hours=4), now_utc=now) == "06"
    assert select_windtemp_forecast_cycle(now + dt.timedelta(hours=12), now_utc=now) == "12"
    assert select_windtemp_forecast_cycle(now + dt.timedelta(hours=22), now_utc=now) == "24"


def test_terminal_risk_scores_low_ceiling_visibility_and_taf_llws():
    """Verify that terminal risk scores low ceiling visibility and taf llws."""

    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "obsTime": 1772754780,
                        "rawOb": "METAR KSTS 052353Z 01020G38KT 1/2SM FZRA OVC003 00/M01 A2992",
                        "fltCat": "LIFR",
                        "reportTime": "2026-03-06T00:00:00.000Z",
                        "wdir": 10,
                        "wspd": 20,
                        "wgst": 38,
                        "visib": "1/2",
                        "wxString": "FZRA",
                        "clouds": [{"cover": "OVC", "base": 300}],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "validTimeFrom": 1772733600,
                        "validTimeTo": 1772820000,
                        "rawTAF": "STS TAF",
                        "issueTime": "2026-03-05T17:24:00.000Z",
                        "fcsts": [
                            {
                                "timeFrom": 1772733600,
                                "timeTo": 1772769600,
                                "wdir": 340,
                                "wspd": 12,
                                "wgst": 18,
                                "visib": "6+",
                                "wxString": None,
                                "clouds": [{"cover": "BKN", "base": 2500}],
                                "wshearHgt": 2000,
                                "wshearDir": 20,
                                "wshearSpd": 45,
                            }
                        ],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload=""),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": []}),
        }
    )

    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]
    airport = weather.airports["KSTS"]

    assert airport.metar_risk is not None
    assert airport.metar_risk.label == "High"
    assert "LIFR flight category" in airport.metar_risk.reasons
    assert "Ceiling 300 ft" in airport.metar_risk.reasons
    assert "Weather FZRA" in airport.metar_risk.reasons
    assert airport.taf_risk is not None
    assert airport.taf_risk.label == "High"
    assert "LLWS 020/45 kt at 2,000 ft" in airport.taf_risk.reasons


def test_legal_alternate_requirement_uses_destination_taf_window_and_approach_flag():
    """Verify that legal alternate requirement uses destination taf window and approach flag."""

    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "validTimeFrom": 1772733600,
                        "validTimeTo": 1772820000,
                        "rawTAF": "STS TAF",
                        "issueTime": "2026-03-05T17:24:00.000Z",
                        "fcsts": [
                            {
                                "timeFrom": 1772748000,
                                "timeTo": 1772762400,
                                "visib": "2",
                                "clouds": [{"cover": "BKN", "base": 1800}],
                            }
                        ],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload=""),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": []}),
        }
    )
    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]

    assessment = evaluate_legal_alternate_requirement(
        weather=weather,
        destination_icao="KSTS",
        eta_utc=dt.datetime.fromtimestamp(1772755200, tz=dt.timezone.utc),
        has_destination_approach=True,
    )
    no_approach = evaluate_legal_alternate_requirement(
        weather=weather,
        destination_icao="KSTS",
        eta_utc=dt.datetime.fromtimestamp(1772755200, tz=dt.timezone.utc),
        has_destination_approach=False,
    )

    assert assessment.is_required is True
    assert assessment.worst_ceiling_ft == 1800
    assert assessment.worst_visibility_sm == 2.0
    assert any("below 2,000" in reason for reason in assessment.reasons)
    assert no_approach.is_required is True
    assert no_approach.has_destination_approach is False


def test_legal_alternate_not_required_when_taf_meets_one_two_three():
    """Verify that legal alternate is not required when taf meets one two three."""

    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "validTimeFrom": 1772733600,
                        "validTimeTo": 1772820000,
                        "rawTAF": "STS TAF",
                        "issueTime": "2026-03-05T17:24:00.000Z",
                        "fcsts": [
                            {
                                "timeFrom": 1772748000,
                                "timeTo": 1772762400,
                                "visib": "6+",
                                "clouds": [{"cover": "BKN", "base": 2500}],
                            }
                        ],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload=""),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": []}),
        }
    )
    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]

    assessment = evaluate_legal_alternate_requirement(
        weather=weather,
        destination_icao="KSTS",
        eta_utc=dt.datetime.fromtimestamp(1772755200, tz=dt.timezone.utc),
        has_destination_approach=True,
    )

    assert assessment.is_required is False
    assert assessment.worst_ceiling_ft == 2500
    assert assessment.worst_visibility_sm == 6.0


def test_forecast_quality_flags_observation_worse_than_applicable_taf():
    """Verify that forecast quality flags observation worse than applicable taf."""

    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "obsTime": 1772754780,
                        "rawOb": "METAR KSTS 052353Z 01020G38KT 1/2SM FZRA OVC003 00/M01 A2992",
                        "fltCat": "LIFR",
                        "reportTime": "2026-03-06T00:00:00.000Z",
                        "wspd": 20,
                        "wgst": 38,
                        "visib": "1/2",
                        "wxString": "FZRA",
                        "clouds": [{"cover": "OVC", "base": 300}],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "validTimeFrom": 1772733600,
                        "validTimeTo": 1772820000,
                        "rawTAF": "STS TAF",
                        "issueTime": "2026-03-05T17:24:00.000Z",
                        "fcsts": [
                            {
                                "timeFrom": 1772733600,
                                "timeTo": 1772769600,
                                "wspd": 10,
                                "visib": "6+",
                                "wxString": None,
                                "clouds": [{"cover": "BKN", "base": 2500}],
                            }
                        ],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload=""),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": []}),
        }
    )
    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]

    checks = evaluate_terminal_forecast_quality(
        weather=weather,
        phase_airports={"Arrival": "KSTS"},
    )

    assert len(checks) == 1
    assert checks[0].phase == "Arrival"
    assert checks[0].score >= 2
    assert any("Observed ceiling" in reason for reason in checks[0].reasons)


def test_build_alternate_range_rings_subtracts_missed_climb_and_descent_fuel_only():
    """Verify that alternate range rings subtract missed climb and descent fuel only."""

    destination = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    rings = build_alternate_range_rings(
        destination=destination,
        fuel_at_destination_gal=80,
        performance_profile=get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID),
        cruise_mode_id=DEFAULT_CRUISE_MODE_ID,
        alt_missed_approach_fuel_gal=5,
    )

    assert rings
    assert rings[-1].altitude_agl_ft <= 20000
    assert all(len(ring.points) == 36 for ring in rings)
    assert all(ring.alt_cruise_fuel_gal > 0 for ring in rings)
    assert all(ring.alt_descent_fuel_gal > 0 for ring in rings)
    assert all((ring.alt_average_range_nm + 1e-6) >= ring.alt_min_range_nm for ring in rings)
    assert all((ring.alt_average_range_nm - 1e-6) <= ring.alt_max_range_nm for ring in rings)
    assert rings[0].alt_cruise_fuel_gal > rings[-1].alt_cruise_fuel_gal


def test_avwx_airport_lookup_prefers_explicit_feet_elevation():
    """Verify AVWX elevation_ft is not silently replaced with sea level."""

    session = _RoutingSession(
        {
            "https://avwx.rest/api/station/KASE": _FakeResponse(
                json_payload={
                    "latitude": 39.2232,
                    "longitude": -106.8688,
                    "timezone": "America/Denver",
                    "elevation_ft": 7820,
                    "elevation": 2384,
                }
            )
        }
    )

    airport = _lookup_airport_from_avwx(
        "KASE", session=session, api_token="test", timeout_seconds=5
    )

    assert airport is not None
    assert airport.elevation_ft == 7820


def test_composite_mission_risk_combines_terminal_route_feed_and_fuel_status():
    """Verify that composite mission risk combines terminal route feed and fuel status."""

    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "obsTime": 1772754780,
                        "rawOb": "METAR KSTS 052353Z 01020G38KT 1/2SM FZRA OVC003 00/M01 A2992",
                        "fltCat": "LIFR",
                        "reportTime": "2026-03-06T00:00:00.000Z",
                        "wdir": 10,
                        "wspd": 20,
                        "wgst": 38,
                        "visib": "1/2",
                        "wxString": "FZRA",
                        "clouds": [{"cover": "OVC", "base": 300}],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload=""),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": []}),
        }
    )
    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]
    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    point = build_mission_brief(
        dep,
        arr,
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=150,
        flight_levels=[300],
        alternate_distance_nm=100,
        reserve_minutes=45,
        landing_minimum_gal=60,
    ).points[0]
    route_hazard = SegmentHazard(1, 40.0, 38.0, -122.0, 0, 2, 0, 2, "TEST")

    summary = build_mission_risk_summary(
        weather=weather,
        segment_hazards=[route_hazard],
        mission_point=point,
    )

    assert summary.score == 3
    assert summary.label == "High"
    assert any("Terminal METAR" in reason for reason in summary.reasons)
    assert any("Fuel reserve" in reason for reason in summary.reasons)


def test_fetch_noaa_weather_parses_expanded_gairmet_hazard_categories():
    """Verify that fetch noaa weather parses expanded gairmet hazard categories."""

    reference_time = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)
    polygon = [
        {"lat": -1.0, "lon": -1.0},
        {"lat": 1.0, "lon": -1.0},
        {"lat": 1.0, "lon": 1.0},
        {"lat": -1.0, "lon": 1.0},
    ]
    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload=""),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(
                json_payload=[
                    {
                        "hazard": "IFR",
                        "severity": "MOD",
                        "coords": polygon,
                        "tag": "SIERRA",
                        "issueTime": int(reference_time.timestamp()),
                        "expireTime": int((reference_time + dt.timedelta(hours=6)).timestamp()),
                    },
                    {
                        "hazard": "MT_OBSC",
                        "severity": "MOD",
                        "coords": polygon,
                        "tag": "SIERRA",
                    },
                    {
                        "hazard": "SFC_WND",
                        "severity": "MOD",
                        "coords": polygon,
                        "tag": "TANGO",
                    },
                    {
                        "hazard": "LLWS",
                        "severity": "MOD",
                        "coords": polygon,
                        "tag": "TANGO",
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": []}),
        }
    )

    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]

    hazard_types = {area.hazard_type for area in weather.hazard_areas}
    assert {"ifr", "mountain_obscuration", "surface_wind", "llws"} <= hazard_types
    ifr_area = next(area for area in weather.hazard_areas if area.hazard_type == "ifr")
    assert ifr_area.base_ft == 0
    assert ifr_area.top_ft == 12000


def test_fetch_noaa_weather_parses_cwa_and_pirep_hazard_inputs():
    """Verify that fetch noaa weather parses cwa and pirep hazard inputs."""

    cwa_polygon = {
        "type": "Polygon",
        "coordinates": [
            [
                [-123.0, 38.0],
                [-122.0, 38.0],
                [-122.0, 39.0],
                [-123.0, 39.0],
                [-123.0, 38.0],
            ]
        ],
    }
    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload=""),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": []}),
            "https://aviationweather.gov/api/data/cwa": _FakeResponse(
                json_payload={
                    "features": [
                        {
                            "properties": {
                                "cwsu": "ZOA",
                                "phenom": "TURB",
                                "issueTime": "2026-03-06T00:00:00.000Z",
                                "expireTime": "2026-03-06T02:00:00.000Z",
                                "top": "FL330",
                                "base": "FL180",
                            },
                            "geometry": cwa_polygon,
                        }
                    ]
                }
            ),
            "https://aviationweather.gov/api/data/pirep": _FakeResponse(
                json_payload=[
                    {
                        "lat": 38.5,
                        "lon": -122.5,
                        "fltLvl": "300",
                        "reportTime": "2026-03-06T00:30:00.000Z",
                        "rawOb": "UA /OV KSTS /TM 0030 /FL300 /TB MOD",
                    }
                ]
            ),
        }
    )

    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]

    sources = " ".join(area.source for area in weather.hazard_areas)
    assert "CWA ZOA TURB" in sources
    assert "PIREP/AIREP" in sources
    assert sum(1 for area in weather.hazard_areas if area.hazard_type == "turbulence") == 2


def test_evaluate_route_hazards_scores_expanded_hazard_fields():
    """Verify that evaluate route hazards scores expanded hazard fields."""

    dep = AirportData("AAAA", 0.0, 0.0, "UTC", "test", elevation_ft=0.0)
    arr = AirportData("BBBB", 0.0, 2.0, "UTC", "test", elevation_ft=0.0)
    reference_time = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)
    route_polygon = [
        (-1.0, -1.0),
        (1.0, -1.0),
        (1.0, 3.0),
        (-1.0, 3.0),
    ]
    expanded_hazard = HazardArea(
        hazard_type="ifr",
        severity_score=2,
        base_ft=0,
        top_ft=45000,
        polygons=[route_polygon],
        source="TEST IFR",
        valid_from_utc=reference_time - dt.timedelta(hours=1),
        valid_to_utc=reference_time + dt.timedelta(hours=3),
    )

    rows = evaluate_route_hazards(
        dep,
        arr,
        hazard_areas=[expanded_hazard],
        reference_time_utc=reference_time,
        flight_levels=[190],
    )[190]

    assert any(row.ifr_score == 2 for row in rows)
    assert summarize_segment_hazard(rows, score_field="ifr_score").startswith("Moderate")
    assert any(row.overall_score == 2 for row in rows)


def test_evaluate_route_hazards_scores_segments_by_altitude_and_time():
    """Verify that evaluate route hazards scores segments by altitude and time."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test")
    reference_time = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)

    route_polygon = [
        (39.5, -124.5),
        (32.5, -124.5),
        (32.5, -110.5),
        (39.5, -110.5),
        (39.5, -124.5),
    ]

    hazard_areas = [
        HazardArea(
            hazard_type="icing",
            severity_score=2,
            base_ft=29000,
            top_ft=32000,
            polygons=[route_polygon],
            source="TEST ICE",
            valid_from_utc=reference_time - dt.timedelta(hours=1),
            valid_to_utc=reference_time + dt.timedelta(hours=3),
        ),
        HazardArea(
            hazard_type="turbulence",
            severity_score=1,
            base_ft=0,
            top_ft=45000,
            polygons=[route_polygon],
            source="TEST TURB",
            valid_from_utc=reference_time - dt.timedelta(hours=1),
            valid_to_utc=reference_time + dt.timedelta(hours=3),
        ),
        HazardArea(
            hazard_type="convective",
            severity_score=3,
            base_ft=0,
            top_ft=45000,
            polygons=[route_polygon],
            source="TEST CONV",
            valid_from_utc=reference_time - dt.timedelta(hours=1),
            valid_to_utc=reference_time + dt.timedelta(hours=3),
        ),
    ]

    hazards_fl300 = evaluate_route_hazards(
        dep,
        arr,
        hazard_areas=hazard_areas,
        reference_time_utc=reference_time,
        flight_levels=[300],
    )[300]
    assert len(hazards_fl300) >= 12
    assert all(row.segment_distance_nm > 0 for row in hazards_fl300)
    assert [row.segment_index for row in hazards_fl300 if row.icing_score >= 2]
    assert all(row.turbulence_score >= 1 for row in hazards_fl300)
    assert all(row.convective_score >= 3 for row in hazards_fl300)
    assert summarize_segment_hazard(hazards_fl300, score_field="overall_score").startswith("High")
    assert hazard_label(2) == "Moderate"

    hazards_fl260 = evaluate_route_hazards(
        dep,
        arr,
        hazard_areas=hazard_areas,
        reference_time_utc=reference_time,
        flight_levels=[260],
    )[260]
    assert all(row.icing_score == 0 for row in hazards_fl260)


def test_evaluate_route_hazards_uses_segment_eta_not_departure_time_only():
    """Verify that evaluate route hazards uses segment eta not departure time only."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    departure_time = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)

    route_polygon = [
        (39.5, -124.5),
        (32.5, -124.5),
        (32.5, -110.5),
        (39.5, -110.5),
        (39.5, -124.5),
    ]

    time_window_hazard = HazardArea(
        hazard_type="turbulence",
        severity_score=2,
        base_ft=0,
        top_ft=45000,
        polygons=[route_polygon],
        source="TEST WINDOW",
        valid_from_utc=departure_time + dt.timedelta(minutes=45),
        valid_to_utc=departure_time + dt.timedelta(minutes=75),
    )

    rows = evaluate_route_hazards(
        dep,
        arr,
        hazard_areas=[time_window_hazard],
        reference_time_utc=departure_time,
        flight_levels=[310],
    )[310]

    impacted_segments = [row.segment_index for row in rows if row.overall_score > 0]
    assert len(impacted_segments) >= 3
    assert min(impacted_segments) > 1


def test_evaluate_route_hazards_captures_climb_and_descent_altitude_bands():
    """Verify that evaluate route hazards captures climb and descent altitude bands."""

    dep = AirportData("AAAA", 0.0, 0.0, "UTC", "test", elevation_ft=0.0)
    arr = AirportData("BBBB", 0.0, 27.0, "UTC", "test", elevation_ft=0.0)
    reference_time = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)

    route_polygon = [
        (-1.0, -1.0),
        (1.0, -1.0),
        (1.0, 28.0),
        (-1.0, 28.0),
        (-1.0, -1.0),
    ]

    climb_descent_hazard = HazardArea(
        hazard_type="icing",
        severity_score=2,
        base_ft=10000,
        top_ft=20000,
        polygons=[route_polygon],
        source="TEST CLIMB DESCENT",
        valid_from_utc=reference_time - dt.timedelta(hours=1),
        valid_to_utc=reference_time + dt.timedelta(hours=10),
    )

    rows = evaluate_route_hazards(
        dep,
        arr,
        hazard_areas=[climb_descent_hazard],
        reference_time_utc=reference_time,
        flight_levels=[310],
    )[310]

    impacted_segments = [row.segment_index for row in rows if row.icing_score > 0]
    assert impacted_segments[0] == 1
    assert impacted_segments[-1] == rows[-1].segment_index


def test_evaluate_route_hazards_uses_multi_leg_route_geometry():
    """Verify that evaluate route hazards uses multi leg route geometry."""

    dep = AirportData("AAAA", 0.0, 0.0, "UTC", "test", elevation_ft=0.0)
    arr = AirportData("BBBB", 0.0, 10.0, "UTC", "test", elevation_ft=0.0)
    # The hazard polygon only intersects the routed dogleg, not the direct track, so the route plan must matter.
    route_plan = build_route_plan(
        dep,
        arr,
        [RouteWaypoint("NORTH", 5.0, 5.0, "Fix", "test")],
    )
    reference_time = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)
    northern_hazard = HazardArea(
        hazard_type="turbulence",
        severity_score=2,
        base_ft=0,
        top_ft=45000,
        polygons=[
            [
                (4.0, 3.5),
                (6.5, 3.5),
                (6.5, 6.5),
                (4.0, 6.5),
            ]
        ],
        source="TEST DOGLEG",
        valid_from_utc=reference_time - dt.timedelta(hours=1),
        valid_to_utc=reference_time + dt.timedelta(hours=3),
    )

    direct_rows = evaluate_route_hazards(
        dep,
        arr,
        hazard_areas=[northern_hazard],
        reference_time_utc=reference_time,
        flight_levels=[310],
    )[310]
    routed_rows = evaluate_route_hazards(
        dep,
        arr,
        hazard_areas=[northern_hazard],
        reference_time_utc=reference_time,
        flight_levels=[310],
        route_plan=route_plan,
    )[310]

    assert all(row.overall_score == 0 for row in direct_rows)
    assert any(row.overall_score > 0 for row in routed_rows)


def test_evaluate_route_hazards_samples_across_segment_not_only_midpoint():
    """Verify that evaluate route hazards samples across segment not only midpoint."""

    dep = AirportData("AAAA", 0.0, 0.0, "UTC", "test", elevation_ft=0.0)
    arr = AirportData("BBBB", 0.0, 12.0, "UTC", "test", elevation_ft=0.0)
    reference_time = dt.datetime(2026, 3, 6, 0, 0, tzinfo=dt.timezone.utc)
    narrow_crossing = HazardArea(
        hazard_type="convective",
        severity_score=3,
        base_ft=0,
        top_ft=45000,
        polygons=[
            [
                (-0.2, 0.12),
                (0.2, 0.12),
                (0.2, 0.22),
                (-0.2, 0.22),
            ]
        ],
        source="TEST NARROW CROSSING",
        valid_from_utc=reference_time - dt.timedelta(hours=1),
        valid_to_utc=reference_time + dt.timedelta(hours=3),
    )

    rows = evaluate_route_hazards(
        dep,
        arr,
        hazard_areas=[narrow_crossing],
        reference_time_utc=reference_time,
        flight_levels=[310],
    )[310]

    impacted_segments = [row.segment_index for row in rows if row.convective_score > 0]
    assert impacted_segments == [1]


def test_descent_bands_sample_touchdown_winds_at_destination_not_tod(monkeypatch):
    """Verify descent wind sampling pairs low bands with the destination end of the route."""

    samples: list[tuple[float, float]] = []

    def record_wind_sample(*, wind_model, sample_latitude, sample_longitude, altitude_ft, track_deg):
        samples.append((float(altitude_ft), float(sample_latitude)))
        return (0.0, 0.0)

    monkeypatch.setattr(weather_core, "_sample_wind_components_from_model", record_wind_sample)
    monkeypatch.setattr(weather_core, "_sample_temperature_from_model", lambda **_kwargs: None)

    def descent_band(low_ft: int, high_ft: int) -> VerticalPerformanceRow:
        return VerticalPerformanceRow(
            start_altitude_ft=low_ft,
            end_altitude_ft=high_ft,
            ias_kts=230,
            rate_fpm=1500,
            fuel_gph=40.0,
        )

    rows = (
        descent_band(0, 8000),
        descent_band(8000, 16000),
        descent_band(16000, 24000),
        descent_band(24000, 31000),
    )
    model = weather_core.RouteWindModel(
        segment_tailwind_by_fl={},
        climb_tailwind_by_fl={},
        descent_tailwind_by_fl={},
        station_profiles=((40.0, -100.0, {30000: (0.0, 0.0)}),),
    )

    weather_core._integrate_vertical_phase(
        lower_altitude_ft=0.0,
        upper_altitude_ft=31000.0,
        rows=rows,
        fallback_ias_kts=230,
        fallback_rate_fpm=1500,
        fallback_fuel_gph=57.0,
        default_tailwind_kts=0.0,
        default_crosswind_kts=0.0,
        departure_latitude=30.0,
        departure_longitude=-100.0,
        destination_latitude=40.0,
        destination_longitude=-100.0,
        mission_distance_nm=600.0,
        track_deg=0.0,
        wind_model=model,
        integrate_from_destination=True,
    )

    assert samples
    lowest_band = min(samples, key=lambda item: item[0])
    highest_band = max(samples, key=lambda item: item[0])
    # Northbound route: the touchdown band must sample at the destination (higher
    # latitude) and the top-of-descent band farther back along the route.
    assert lowest_band[1] > highest_band[1]
    assert lowest_band[1] == pytest.approx(40.0, abs=0.2)


def test_mission_risk_uses_worst_leg_margin_override_for_refueled_missions():
    """Verify a healthy nonstop margin cannot mask a short leg on a refueled mission."""

    weather = fetch_noaa_weather(["KSTS"], session=_BrokenSession())  # type: ignore[arg-type]
    safe_point = build_mission_brief(
        AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0),
        AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0),
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        is_return_leg=False,
        start_fuel_gal=292,
        flight_levels=[300],
    ).points[0]
    assert safe_point.reserve_margin_gal > 0

    summary = build_mission_risk_summary(
        weather=weather,
        segment_hazards=[],
        mission_point=safe_point,
        thresholds=MissionRiskThresholds(),
        reserve_margin_override_gal=-12,
        reserve_margin_context="Leg 2",
    )

    assert summary.score == 3
    assert any("shortfall 12 gal (Leg 2)" in reason for reason in summary.reasons)


def test_cwa_transposed_band_swaps_and_sev_qualifier_escalates():
    """Verify a base/top transposition is corrected and a SEV qualifier scores High."""

    polygon = {
        "type": "Polygon",
        "coordinates": [[[-123.0, 38.0], [-122.0, 38.0], [-122.0, 39.0], [-123.0, 38.0]]],
    }
    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={
            "features": [
                {
                    "properties": {"phenom": "TURB", "qualifier": "SEV", "base": 33000, "top": 8000},
                    "geometry": polygon,
                }
            ]
        },
        pirep_rows=[],
    )

    assert len(areas) == 1
    assert areas[0].base_ft == 8000
    assert areas[0].top_ft == 33000
    assert areas[0].severity_score == 3


def test_cwa_urgent_marker_in_product_text_escalates_severity():
    """Verify a numeric seriesId cannot hide the UCWA urgency carried in cwaText."""

    polygon = {
        "type": "Polygon",
        "coordinates": [[[-123.0, 38.0], [-122.0, 38.0], [-122.0, 39.0], [-123.0, 38.0]]],
    }
    areas = _parse_hazard_areas(
        gairmet_rows=[],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={
            "features": [
                {
                    "properties": {"phenom": "ICE", "seriesId": 104, "cwaText": "UCWA ZOA1 041815"},
                    "geometry": polygon,
                }
            ]
        },
        pirep_rows=[],
    )

    assert len(areas) == 1
    assert areas[0].hazard_type == "icing"
    assert areas[0].severity_score == 3


def test_vv_cover_fallback_decodes_hundreds_of_feet():
    """Verify a raw VV cover group reads as hundreds of feet, not feet."""

    assert weather_core._lowest_ceiling_ft(cover="VV003", clouds=None) == 300


def test_forecast_quality_sees_obscured_sky_ceiling():
    """Verify an OVX/vertVis METAR ceiling participates in the METAR-vs-TAF check."""

    session = _RoutingSession(
        {
            "https://aviationweather.gov/api/data/metar": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "obsTime": 1772754780,
                        "rawOb": "METAR KSTS 052353Z 00000KT 6SM BR VV002 08/06 A2992",
                        "reportTime": "2026-03-06T00:00:00.000Z",
                        "visib": "6+",
                        "cover": "OVX",
                        "vertVis": 200,
                        "clouds": [{"cover": "OVX"}],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/taf": _FakeResponse(
                json_payload=[
                    {
                        "icaoId": "KSTS",
                        "validTimeFrom": 1772733600,
                        "validTimeTo": 1772820000,
                        "rawTAF": "STS TAF",
                        "issueTime": "2026-03-05T17:24:00.000Z",
                        "fcsts": [
                            {
                                "timeFrom": 1772733600,
                                "timeTo": 1772769600,
                                "visib": "6+",
                                "wxString": None,
                                "clouds": [{"cover": "BKN", "base": 2500}],
                            }
                        ],
                    },
                ]
            ),
            "https://aviationweather.gov/api/data/windtemp": _FakeResponse(text_payload=""),
            "https://aviationweather.gov/api/data/gairmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/airsigmet": _FakeResponse(json_payload=[]),
            "https://aviationweather.gov/api/data/tcf": _FakeResponse(json_payload={"features": []}),
        }
    )
    weather = fetch_noaa_weather(["KSTS"], session=session)  # type: ignore[arg-type]

    checks = evaluate_terminal_forecast_quality(
        weather=weather,
        phase_airports={"Arrival": "KSTS"},
    )

    assert len(checks) == 1
    assert checks[0].score >= 2
    assert any("Observed ceiling" in reason for reason in checks[0].reasons)


def test_hazard_applies_at_shares_the_gairmet_horizon_fallback():
    """Verify the shared validity decision labels and caps the beyond-horizon fallback."""

    last_snapshot_end = dt.datetime(2026, 7, 20, 18, 0, tzinfo=dt.timezone.utc)
    footprint = [[(32.0, -124.5), (40.5, -124.5), (40.5, -115.0), (32.0, -115.0)]]
    area = HazardArea(
        hazard_type="icing",
        severity_score=2,
        base_ft=8000,
        top_ft=20000,
        polygons=footprint,
        source="G-AIRMET SIERRA 1",
        valid_from_utc=last_snapshot_end - dt.timedelta(hours=3),
        valid_to_utc=last_snapshot_end,
    )
    stale = HazardArea(
        hazard_type="icing",
        severity_score=2,
        base_ft=8000,
        top_ft=20000,
        polygons=footprint,
        source="G-AIRMET SIERRA 1",
        valid_from_utc=last_snapshot_end - dt.timedelta(hours=6),
        valid_to_utc=last_snapshot_end - dt.timedelta(hours=3),
    )
    latest = {"G-AIRMET SIERRA 1": last_snapshot_end}

    inside_window = weather_core.hazard_applies_at(area, last_snapshot_end - dt.timedelta(hours=1), latest)
    beyond_horizon = weather_core.hazard_applies_at(area, last_snapshot_end + dt.timedelta(hours=3), latest)
    beyond_cap = weather_core.hazard_applies_at(area, last_snapshot_end + dt.timedelta(hours=7), latest)
    stale_snapshot = weather_core.hazard_applies_at(stale, last_snapshot_end + dt.timedelta(hours=1), latest)

    assert inside_window == (True, False)
    assert beyond_horizon == (True, True)
    assert beyond_cap == (False, False)
    assert stale_snapshot == (False, False)


def test_vertical_profile_paints_gairmet_beyond_horizon_like_the_table():
    """Verify the side profile shows the labeled beyond-horizon G-AIRMET the table shows."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KPSP", 33.8297, -116.5070, "US/Pacific", "test", elevation_ft=477.0)
    reference_time = dt.datetime(2026, 3, 7, 18, 45, tzinfo=dt.timezone.utc)
    beyond_horizon_area = HazardArea(
        hazard_type="turbulence",
        severity_score=2,
        base_ft=24000,
        top_ft=36000,
        polygons=[
            [
                (32.0, -124.5),
                (40.5, -124.5),
                (40.5, -115.0),
                (32.0, -115.0),
            ]
        ],
        source="G-AIRMET TANGO 5",
        valid_from_utc=reference_time - dt.timedelta(hours=5),
        valid_to_utc=reference_time - dt.timedelta(hours=2),
    )

    vertical_profile = build_route_vertical_profile(
        dep,
        arr,
        hazard_areas=[beyond_horizon_area],
        reference_time_utc=reference_time,
        flight_level=310,
    )

    assert vertical_profile.hazard_spans
    assert "(latest snapshot beyond forecast horizon)" in vertical_profile.hazard_spans[0].source


def test_build_multi_leg_plan_chains_fuel_timing_and_alternates():
    """Golden two-leg mission: fuel handoff, ground-time chaining, and per-leg policy."""

    weather = fetch_noaa_weather(["KSTS"], session=_BrokenSession())  # type: ignore[arg-type]
    departure = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    destination = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    stop = RouteWaypoint(
        identifier="KBFL",
        latitude=35.4336,
        longitude=-119.0568,
        waypoint_type="Airport",
        source="test",
        is_fuel_stop=True,
    )
    segments = split_route_plan_at_fuel_stops(build_route_plan(departure, destination, [stop]))
    assert len(segments) == 2

    plan = weather_core.build_multi_leg_plan(
        fuel_stop_segments=segments,
        departure_dt=dt.datetime(2026, 7, 21, 10, 0, tzinfo=dt.timezone.utc),
        start_fuel_gal=292.0,
        ground_minutes=45.0,
        uplifts={"KBFL": 80.0},
        alternates={},
        mission_alternate_code=None,
        mission_alternate_distance_nm=0.0,
        mission_alternate_route_label="",
        approach_confirmed_icaos={"KBFL"},
        destination_has_approach=True,
        departure_fallback_timezone="US/Pacific",
        destination_fallback_timezone="US/Arizona",
        weather=weather,
        usable_fuel_capacity_gal=292.0,
        focus_flight_level=300,
        mission_brief_kwargs={},
    )

    assert len(plan.legs) == 2
    first, second = plan.legs
    assert not first.is_final_leg
    assert second.is_final_leg
    assert (second.departure_utc - first.arrival_utc) == dt.timedelta(minutes=45)
    # Landing fuel plus the 80-gal uplift exceeds the tank, so the handoff trims
    # to usable capacity and says so.
    assert first.point.fuel_at_dest + 80.0 > 292.0
    assert second.start_fuel_gal == pytest.approx(292.0)
    assert first.uplift_gal == 80.0
    assert any("trimmed" in warning for warning in first.warnings)
    assert plan.final_arrival_utc == second.arrival_utc
    assert plan.leg_reserve_margins_gal[0][0] == "Leg 1"
    assert plan.leg_arrival_fuels_gal[-1] == float(second.point.fuel_at_dest)
    assert first.has_approach_confirmed is True
    assert first.alternate_route_label == "Not specified — alternate fuel excluded"


def test_build_mission_brief_derives_direction_and_matches_the_swap_convention():
    """Verify derive_direction owns the westbound convention the UI used to pre-swap."""

    ksts = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    kffz = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test", elevation_ft=1394.0)
    kwargs = dict(
        departure_date=dt.date(2026, 3, 5),
        departure_time_local=dt.time(10, 0),
        start_fuel_gal=292,
        flight_levels=[300],
    )

    derived = build_mission_brief(kffz, ksts, derive_direction=True, **kwargs)
    legacy = build_mission_brief(ksts, kffz, is_return_leg=True, **kwargs)

    assert weather_core.is_westbound_route(kffz, ksts)
    assert "Westbound" in derived.direction_label
    assert derived.points[0].ete == legacy.points[0].ete
    assert derived.points[0].fuel_burn == legacy.points[0].fuel_burn


def test_windtemp_cycle_correction_requires_product_issue_time():
    """Verify no correction is offered without the product's own DATA-BASED-ON time."""

    weather = fetch_noaa_weather(["KSTS"], session=_BrokenSession())  # type: ignore[arg-type]

    correction = weather_core.windtemp_cycle_correction(
        weather, dt.datetime(2026, 7, 21, 10, 0, tzinfo=dt.timezone.utc)
    )

    assert correction is None


def test_gairmet_iso_valid_time_builds_the_snapshot_window():
    """Verify the live-data ISO validTime shape drives the snapshot validity window."""

    areas = _parse_hazard_areas(
        gairmet_rows=[
            {
                "hazard": "TURB-HI",
                "validTime": "2026-07-20T18:00:00Z",
                "forecastHour": 3,
                "issueTime": "2026-07-20T14:45:00Z",
                "base": "240",
                "top": "360",
                "coords": [
                    {"lat": 38.0, "lon": -123.0},
                    {"lat": 39.0, "lon": -123.0},
                    {"lat": 39.0, "lon": -122.0},
                ],
            }
        ],
        airsigmet_rows=[],
        tcf_payload={},
        cwa_payload={},
        pirep_rows=[],
    )

    snapshot = dt.datetime(2026, 7, 20, 18, 0, tzinfo=dt.timezone.utc)
    assert len(areas) == 1
    assert areas[0].valid_from_utc == snapshot - weather_core.GAIRMET_SNAPSHOT_HALF_WINDOW
    assert areas[0].valid_to_utc == snapshot + weather_core.GAIRMET_SNAPSHOT_HALF_WINDOW


@pytest.mark.parametrize(
    ("numeric_severity", "expected_score"),
    [(5, 3), (4, 3), (3, 2), (1, 1)],
)
def test_numeric_severity_coding_matches_observed_awc_values(numeric_severity, expected_score):
    """Verify the documented numeric severity mapping, with >=4 conservatively High."""

    assert weather_core._risk_score_from_severity(numeric_severity) == expected_score
