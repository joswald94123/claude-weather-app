"""Regression coverage for state-selection heuristics and route-map rendering."""

import re
from zipfile import ZipFile

from route_planning import RouteWaypoint, build_route_plan
from route_context_map import (
    _screen_projection,
    build_range_inset_svg,
    build_route_context_svg,
    load_state_boundaries,
    select_states_for_route,
)
from weather_core import AirportData, AlternateRangeRing


def test_load_state_boundaries_includes_contiguous_states():
    """Verify that load state boundaries includes contiguous states."""

    states = load_state_boundaries()
    codes = {state.code for state in states}

    assert "CA" in codes
    assert "AZ" in codes
    assert "NV" in codes


def test_load_state_boundaries_degrades_when_kml_member_is_missing(tmp_path, monkeypatch):
    """A broken optional basemap must not prevent the flight plan from rendering."""

    archive_path = tmp_path / "states.zip"
    with ZipFile(archive_path, "w") as archive:
        archive.writestr("unexpected.kml", "<kml />")

    monkeypatch.setattr("route_context_map.STATE_BOUNDARY_ZIP", archive_path)
    load_state_boundaries.cache_clear()
    try:
        assert load_state_boundaries() == ()
    finally:
        load_state_boundaries.cache_clear()


def test_screen_projection_uses_one_scale_for_both_axes():
    """A geographic square at the mean latitude should remain square onscreen."""

    project = _screen_projection(
        bbox=(-101.0, 39.0, -99.0, 41.0),
        width=800,
        height=600,
        frame=20.0,
    )
    longitude_scale = 0.7660444431  # cos(40 degrees)
    x0, y0 = project(-100.0, 40.0)
    x1, _ = project(-100.0 + (1.0 / longitude_scale), 40.0)
    _, y1 = project(-100.0, 41.0)

    assert abs(abs(x1 - x0) - abs(y1 - y0)) <= 0.02


def test_albers_projection_returns_finite_points_inside_the_frame():
    """Verify the regional/national Albers branch remains directly exercised."""

    project = _screen_projection(
        bbox=(-125.0, 25.0, -66.0, 49.0),
        width=900.0,
        height=560.0,
        frame=22.0,
        projection="albers",
    )

    for longitude, latitude in ((-122.3, 47.6), (-96.8, 32.8), (-73.8, 40.6)):
        x_value, y_value = project(longitude, latitude)
        assert 22.0 <= x_value <= 878.0
        assert 22.0 <= y_value <= 538.0


# Route-length tests pin the corridor/regional/lower-48 map-scope thresholds.
def test_select_states_for_route_keeps_context_local_to_route_corridor():
    """Verify that select states for route keeps context local to route corridor."""

    states = select_states_for_route(
        38.5089,
        -122.8130,
        33.4608,
        -111.7280,
    )
    codes = {state.code for state in states}

    assert {"CA", "NV", "AZ"}.issubset(codes)
    assert "KS" not in codes
    assert "TX" not in codes


def test_select_states_for_route_uses_regional_mode_for_medium_length_trip():
    """Verify that select states for route uses regional mode for medium length trip."""

    states = select_states_for_route(
        38.5089,
        -122.8130,
        39.8617,
        -104.6731,
    )
    codes = {state.code for state in states}

    assert {"CA", "NV", "UT", "CO"}.issubset(codes)
    assert "WI" not in codes


def test_select_states_for_route_uses_lower_48_mode_for_long_trip():
    """Verify that select states for route uses lower 48 mode for long trip."""

    states = select_states_for_route(
        38.5089,
        -122.8130,
        41.9786,
        -87.9048,
    )
    codes = {state.code for state in states}

    assert "CA" in codes
    assert "FL" in codes
    assert "ME" in codes
    assert "AK" not in codes
    assert "HI" not in codes
    assert len(codes) >= 48


def test_build_route_context_svg_marks_endpoints():
    """Verify that build route context svg marks endpoints."""

    svg = build_route_context_svg(
        departure_label="KSTS",
        departure_latitude=38.5089,
        departure_longitude=-122.8130,
        destination_label="KFFZ",
        destination_latitude=33.4608,
        destination_longitude=-111.7280,
    )

    assert svg.startswith("<svg")
    assert "KSTS" in svg
    assert "KFFZ" in svg
    assert "<polyline" in svg
    assert 'role="img"' in svg
    assert "Route from KSTS to KFFZ" in svg
    assert "Fuel stop" in svg
    assert "Destination" in svg


def test_build_route_context_svg_preserves_uniform_scale():
    """Verify that build route context svg preserves uniform scale."""

    svg = build_route_context_svg(
        departure_label="KSTS",
        departure_latitude=38.5089,
        departure_longitude=-122.8130,
        destination_label="KPSP",
        destination_latitude=33.8297,
        destination_longitude=-116.5070,
    )

    coordinates = [float(value) for value in re.findall(r"\b\d+\.\d+\b", svg)]

    assert coordinates
    assert min(coordinates) >= 0.0
    assert max(coordinates) <= 960.0


def test_build_route_context_svg_labels_intermediate_waypoints():
    """Verify that build route context svg labels intermediate waypoints."""

    departure = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    destination = AirportData("KFFZ", 33.4608, -111.7280, "US/Arizona", "test")
    route_plan = build_route_plan(
        departure,
        destination,
        [RouteWaypoint("OAL", 38.0000, -117.7690, "VOR/DME", "test")],
    )

    svg = build_route_context_svg(
        departure_label=departure.icao,
        departure_latitude=departure.latitude,
        departure_longitude=departure.longitude,
        destination_label=destination.icao,
        destination_latitude=destination.latitude,
        destination_longitude=destination.longitude,
        route_plan=route_plan,
    )

    assert "OAL" in svg


def test_build_route_context_svg_draws_alternate_range_rings():
    """Verify that build route context svg draws alternate range rings."""

    ring = AlternateRangeRing(
        altitude_agl_ft=5000,
        altitude_msl_ft=6400,
        alt_cruise_fuel_gal=40.0,
        alt_climb_fuel_gal=5.0,
        alt_descent_fuel_gal=3.0,
        alt_missed_approach_fuel_gal=5.0,
        alt_climb_distance_nm=18.0,
        alt_cruise_distance_nm=120.0,
        alt_descent_distance_nm=32.0,
        alt_min_range_nm=160.0,
        alt_max_range_nm=180.0,
        alt_average_range_nm=170.0,
        points=(
            (-112.0, 33.9),
            (-111.0, 34.0),
            (-111.1, 33.0),
        ),
        line_style="8 5",
        label="5k AGL",
    )

    svg = build_route_context_svg(
        departure_label="KSTS",
        departure_latitude=38.5089,
        departure_longitude=-122.8130,
        destination_label="KFFZ",
        destination_latitude=33.4608,
        destination_longitude=-111.7280,
        alternate_range_rings=(ring,),
    )

    assert "Post-missed alternate range" in svg
    assert "5k AGL" in svg
    assert ">Range</text>" in svg


def test_build_range_inset_svg_draws_waypoint_specific_rings():
    """Verify that build range inset svg draws waypoint-specific rings."""

    ring = AlternateRangeRing(
        altitude_agl_ft=5000,
        altitude_msl_ft=6400,
        alt_cruise_fuel_gal=40.0,
        alt_climb_fuel_gal=5.0,
        alt_descent_fuel_gal=3.0,
        alt_missed_approach_fuel_gal=5.0,
        alt_climb_distance_nm=18.0,
        alt_cruise_distance_nm=120.0,
        alt_descent_distance_nm=32.0,
        alt_min_range_nm=160.0,
        alt_max_range_nm=180.0,
        alt_average_range_nm=170.0,
        points=(
            (-112.0, 33.9),
            (-111.0, 34.0),
            (-111.1, 33.0),
        ),
        line_style="8 5",
        label="5k AGL",
    )

    svg = build_range_inset_svg(
        anchor_label="KFFZ",
        anchor_latitude=33.4608,
        anchor_longitude=-111.7280,
        range_rings=(ring,),
        title="Destination KFFZ",
    )

    assert "Destination KFFZ" in svg
    assert "KFFZ" in svg
    assert "5k AGL" in svg
    assert "average range 170 NM" in svg


def test_build_range_inset_svg_states_when_no_ring_is_available():
    """Verify that a zero-ring inset explains the absence instead of looking broken."""

    svg = build_range_inset_svg(
        anchor_label="KFFZ",
        anchor_latitude=33.4608,
        anchor_longitude=-111.7280,
        range_rings=(),
    )

    assert "No range available" in svg
    assert "KFFZ" in svg
