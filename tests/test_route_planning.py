"""Regression coverage for route parsing, geometry, and non-linear warnings."""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from weather_core import AirportData
from route_planning import (
    RouteWaypoint,
    build_route_plan,
    chain_multi_leg_timings,
    destination_arrival_fuel_gal,
    great_circle_distance_nm,
    route_point_at_distance_nm,
    route_progress_warning,
    resolve_fuel_stop_leg_policy,
    route_track_at_distance_nm,
    split_route_plan_at_fuel_stops,
    parse_airborne_ete,
)


def test_build_route_plan_accumulates_leg_distance_and_samples_polyline():
    """Verify that build route plan accumulates leg distance and samples polyline."""

    departure = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    destination = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test")
    oal = RouteWaypoint("OAL", 38.0000, -117.7690, "VOR/DME", "test")

    route_plan = build_route_plan(departure, destination, [oal])

    direct_distance_nm = great_circle_distance_nm(
        departure.latitude,
        departure.longitude,
        destination.latitude,
        destination.longitude,
    )
    # The midpoint of the flown route should fall near the inserted dogleg, not the direct midpoint.
    midpoint_latitude, midpoint_longitude = route_point_at_distance_nm(
        route_plan,
        route_plan.total_distance_nm / 2.0,
    )

    assert route_plan.route_label == "KSTS -> OAL -> KFFZ"
    assert route_plan.total_distance_nm > direct_distance_nm
    assert abs(midpoint_latitude - oal.latitude) < 2.0
    assert abs(midpoint_longitude - oal.longitude) < 2.0
    assert 80.0 <= route_track_at_distance_nm(route_plan, 10.0) <= 120.0


def test_route_point_uses_great_circle_interpolation_on_long_legs():
    """Verify that route point uses great circle interpolation on long legs."""

    departure = AirportData("KJFK", 40.6413, -73.7781, "US/Eastern", "test")
    destination = AirportData("EGLL", 51.4700, -0.4543, "Europe/London", "test")
    route_plan = build_route_plan(departure, destination)

    midpoint_latitude, midpoint_longitude = route_point_at_distance_nm(
        route_plan,
        route_plan.total_distance_nm / 2.0,
    )

    assert midpoint_latitude > 50.0
    assert -45.0 < midpoint_longitude < -30.0


def test_long_great_circle_track_changes_along_the_leg():
    """Verify wind decomposition follows course convergence rather than a frozen initial track."""

    departure = AirportData("KSEA", 47.4502, -122.3088, "US/Pacific", "test")
    destination = AirportData("EGLL", 51.4700, -0.4543, "Europe/London", "test")
    route_plan = build_route_plan(departure, destination)

    start_track = route_track_at_distance_nm(route_plan, route_plan.total_distance_nm * 0.1)
    end_track = route_track_at_distance_nm(route_plan, route_plan.total_distance_nm * 0.9)

    assert abs(start_track - end_track) > 20.0


def test_route_progress_warning_flags_backtracking_order():
    """Verify that route progress warning flags backtracking order."""

    departure = AirportData("AAAA", 0.0, 0.0, "UTC", "test")
    destination = AirportData("BBBB", 0.0, 10.0, "UTC", "test")
    route_plan = build_route_plan(
        departure,
        destination,
        [
            RouteWaypoint("FIRST", 0.0, 7.5, "Fix", "test"),
            RouteWaypoint("SECOND", 0.0, 3.5, "Fix", "test"),
        ],
    )

    warning = route_progress_warning(route_plan)

    assert warning is not None
    assert "FIRST -> SECOND" in warning


def test_route_progress_warning_flags_large_detour_without_backtracking():
    """Verify that a forward-progressing but excessive dogleg receives the detour warning."""

    departure = AirportData("AAAA", 0.0, 0.0, "UTC", "test")
    destination = AirportData("BBBB", 0.0, 10.0, "UTC", "test")
    route_plan = build_route_plan(
        departure,
        destination,
        [
            RouteWaypoint("NORTH", 12.0, 3.0, "Fix", "test"),
            RouteWaypoint("RETURN", 12.0, 7.0, "Fix", "test"),
        ],
    )

    assert route_progress_warning(route_plan) == (
        "Waypoint order creates a large detour relative to the direct route. Verify the sequence."
    )


def test_split_route_plan_at_fuel_stops_preserves_flown_subroutes():
    """Verify that split route plan at fuel stops preserves flown subroutes."""

    departure = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    destination = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test")
    fuel_stop = RouteWaypoint("KBFL", 35.4336, -119.0568, "Airport", "test", is_fuel_stop=True)
    shaping_fix = RouteWaypoint("PMD", 34.6294, -118.0631, "VOR/DME", "test")
    route_plan = build_route_plan(departure, destination, [fuel_stop, shaping_fix])

    segments = split_route_plan_at_fuel_stops(route_plan)

    assert len(segments) == 2
    assert segments[0].route_plan.route_text == "KSTS KBFL"
    assert segments[1].route_plan.route_text == "KBFL PMD KFFZ"
    assert segments[0].route_plan.total_distance_nm + segments[1].route_plan.total_distance_nm == pytest.approx(
        route_plan.total_distance_nm
    )


def test_chain_multi_leg_timings_accumulates_ground_time_across_timezones():
    """Verify two stops accumulate numeric ETE and ground time on one absolute timeline."""

    departure = dt.datetime(2026, 7, 19, 8, 0, tzinfo=ZoneInfo("America/Los_Angeles"))
    timings = chain_multi_leg_timings(
        departure,
        ("1h 30m", "2h 15m", "1h 05m"),
        ground_minutes=45,
    )

    assert len(timings) == 3
    assert timings[1].departure == timings[0].arrival + dt.timedelta(minutes=45)
    assert timings[2].departure == timings[1].arrival + dt.timedelta(minutes=45)
    assert timings[-1].arrival.astimezone(ZoneInfo("America/New_York")) == dt.datetime(
        2026, 7, 19, 17, 20, tzinfo=ZoneInfo("America/New_York")
    )


def test_chain_multi_leg_timings_accepts_numeric_airborne_hours():
    """Verify calculation timing does not need to round-trip through display text."""

    departure = dt.datetime(2026, 7, 20, 20, 0, tzinfo=ZoneInfo("UTC"))
    timing = chain_multi_leg_timings(departure, [1.5])[0]

    assert timing.arrival == departure + dt.timedelta(hours=1, minutes=30)


def test_parse_airborne_ete_rejects_malformed_or_overflow_values():
    """Verify an invalid ETE cannot silently become a zero-duration leg."""

    with pytest.raises(ValueError, match="Invalid airborne ETE"):
        parse_airborne_ete("pending")
    with pytest.raises(ValueError, match="minutes"):
        parse_airborne_ete("1h 75m")


def test_two_stop_fuel_policies_apply_uplift_and_explicit_alternate_fallbacks():
    """Verify two-stop policy resolution never hides omitted alternate fuel."""

    first = resolve_fuel_stop_leg_policy(
        destination_identifier="KBFL",
        is_final_leg=False,
        landing_fuel_gal=70,
        default_start_fuel_gal=292,
        uplifts={"KBFL": 80},
        alternates={"KBFL": "KSMX"},
        mission_alternate_code="KSDL",
    )
    second = resolve_fuel_stop_leg_policy(
        destination_identifier="KLAS",
        is_final_leg=False,
        landing_fuel_gal=65,
        default_start_fuel_gal=292,
        uplifts={},
        alternates={},
        mission_alternate_code="KSDL",
    )
    final = resolve_fuel_stop_leg_policy(
        destination_identifier="KFFZ",
        is_final_leg=True,
        landing_fuel_gal=55,
        default_start_fuel_gal=292,
        uplifts={},
        alternates={},
        mission_alternate_code="KSDL",
    )

    assert first.next_start_fuel_gal == 150
    assert first.alternate_code == "KSMX"
    assert not first.alternate_fuel_excluded
    assert second.next_start_fuel_gal == 292
    assert second.alternate_fuel_excluded
    assert final.next_start_fuel_gal == 55
    assert final.alternate_code == "KSDL"


def test_destination_range_fuel_uses_final_chained_leg_arrival():
    """Verify refueled missions do not draw destination rings from nonstop through-fuel."""

    assert destination_arrival_fuel_gal(42, [80, 67, 55]) == 55
    assert destination_arrival_fuel_gal(42, []) == 42
