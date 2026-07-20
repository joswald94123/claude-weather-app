"""Regression coverage for the route side-profile SVG renderer."""

import datetime as dt
from dataclasses import replace
import xml.etree.ElementTree as ET

from route_planning import RouteWaypoint, build_route_plan
from route_vertical_profile import (
    build_interactive_route_vertical_profile_html,
    build_route_vertical_profile_svg,
)
from weather_core import (
    AirportData,
    HazardArea,
    RouteVerticalProfileHazardSpan,
    build_route_vertical_profile,
)


def test_route_vertical_profile_svg_shows_airports_selected_fl_and_hazard_band_labels():
    """Verify that route vertical profile svg shows airports selected fl and hazard band labels."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KPSP", 33.8297, -116.5070, "US/Pacific", "test", elevation_ft=477.0)
    route_hazard = HazardArea(
        hazard_type="icing",
        severity_score=2,
        base_ft=12000,
        top_ft=18000,
        polygons=[
            [
                (32.0, -124.5),
                (40.5, -124.5),
                (40.5, -115.0),
                (32.0, -115.0),
            ]
        ],
        source="Test icing band",
    )

    vertical_profile = build_route_vertical_profile(
        dep,
        arr,
        hazard_areas=[route_hazard],
        reference_time_utc=dt.datetime(2026, 3, 7, 18, 45, tzinfo=dt.timezone.utc),
        flight_level=310,
    )
    svg = build_route_vertical_profile_svg(
        vertical_profile,
        departure_label="KSTS",
        destination_label="KPSP",
        selected_flight_level_label="FL310",
    )

    assert svg.startswith("<svg")
    assert "KSTS" in svg
    assert "KPSP" in svg
    assert "FL310" in svg
    assert "18,000" in svg
    assert "12,000" in svg
    assert "Band labels show base/top feet MSL" in svg


# Filtering tests make sure the UI toggles can remove one hazard family without breaking the SVG.
def test_route_vertical_profile_svg_filters_hazard_types():
    """Verify that route vertical profile svg filters hazard types."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test", elevation_ft=129.0)
    arr = AirportData("KPSP", 33.8297, -116.5070, "US/Pacific", "test", elevation_ft=477.0)
    icing_hazard = HazardArea(
        hazard_type="icing",
        severity_score=2,
        base_ft=12000,
        top_ft=18000,
        polygons=[
            [
                (32.0, -124.5),
                (40.5, -124.5),
                (40.5, -115.0),
                (32.0, -115.0),
            ]
        ],
        source="Test icing band",
    )

    vertical_profile = build_route_vertical_profile(
        dep,
        arr,
        hazard_areas=[icing_hazard],
        reference_time_utc=dt.datetime(2026, 3, 7, 18, 45, tzinfo=dt.timezone.utc),
        flight_level=310,
    )
    svg = build_route_vertical_profile_svg(
        vertical_profile,
        departure_label="KSTS",
        destination_label="KPSP",
        selected_flight_level_label="FL310",
        visible_hazard_types={"turbulence"},
    )

    assert "ICING" not in svg
    assert "No visible route hazard bands currently intersect the route timeline." in svg


def test_route_vertical_profile_svg_labels_intermediate_waypoints():
    """Verify that route vertical profile svg labels intermediate waypoints."""

    dep = AirportData("KPHX", 33.4342, -112.0116, "US/Arizona", "test", elevation_ft=1135.0)
    arr = AirportData("KSFO", 37.6188, -122.3750, "US/Pacific", "test", elevation_ft=13.0)
    # A routed profile should expose the same intermediate fixes the sidebar and map already show.
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
    svg = build_route_vertical_profile_svg(
        vertical_profile,
        departure_label="KPHX",
        destination_label="KSFO",
        selected_flight_level_label="FL300",
    )

    assert "PMD" in svg


def test_route_vertical_profile_clamps_off_route_thin_hazard_and_marks_visual_minimum():
    """Verify tiny/off-route overlays stay inside the frame and disclose visual enlargement."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    arr = AirportData("KPSP", 33.8297, -116.5070, "US/Pacific", "test")
    profile = build_route_vertical_profile(
        dep,
        arr,
        hazard_areas=[],
        reference_time_utc=dt.datetime(2026, 3, 7, 18, 45, tzinfo=dt.timezone.utc),
        flight_level=310,
    )
    thin_span = RouteVerticalProfileHazardSpan(
        hazard_type="icing",
        severity_score=1,
        base_ft=12000,
        top_ft=12010,
        start_distance_nm=-5.0,
        end_distance_nm=0.1,
        source="Thin test band",
    )

    svg = build_route_vertical_profile_svg(
        replace(profile, hazard_spans=[thin_span]),
        departure_label="KSTS",
        destination_label="KPSP",
        selected_flight_level_label="FL310",
    )

    assert "enlarged to minimum visible size" in svg
    assert "Minimum visible size marker" in svg
    assert 'x="86.0"' in svg


def test_route_vertical_profile_labels_every_band_and_groups_same_type_fill():
    """Every region is named and same-type overlaps share one composited fill layer."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    arr = AirportData("KAUS", 30.1975, -97.6664, "US/Central", "test")
    profile = build_route_vertical_profile(
        dep,
        arr,
        hazard_areas=[],
        reference_time_utc=dt.datetime(2026, 7, 20, 17, 0, tzinfo=dt.timezone.utc),
        flight_level=310,
    )
    spans = [
        RouteVerticalProfileHazardSpan(
            hazard_type="convective",
            severity_score=1,
            base_ft=0,
            top_ft=39000,
            start_distance_nm=200.0,
            end_distance_nm=700.0,
            source="Test convective one",
        ),
        RouteVerticalProfileHazardSpan(
            hazard_type="convective",
            severity_score=3,
            base_ft=0,
            top_ft=34000,
            start_distance_nm=500.0,
            end_distance_nm=1000.0,
            source="Test convective two",
        ),
    ]

    svg = build_route_vertical_profile_svg(
        replace(profile, hazard_spans=spans),
        departure_label="KSTS",
        destination_label="KAUS",
        selected_flight_level_label="FL310",
    )

    assert svg.count(">CONVECTIVE</text>") == 2
    assert svg.count('class="hazard-fill-layer" opacity="0.42"') == 1
    assert svg.count('data-hazard-type="convective"') == 1
    assert 'data-hazard-toggle="convective"' in svg


def test_overlapping_hazard_bands_use_distinct_upper_left_label_lanes():
    """Keep hazard names readable when two advisory rectangles share their upper-left area."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    arr = AirportData("KAUS", 30.1975, -97.6664, "US/Central", "test")
    profile = build_route_vertical_profile(
        dep,
        arr,
        hazard_areas=[],
        reference_time_utc=dt.datetime(2026, 7, 20, 17, 0, tzinfo=dt.timezone.utc),
        flight_level=310,
    )
    shared_geometry = {
        "severity_score": 2,
        "base_ft": 12000,
        "top_ft": 39000,
        "start_distance_nm": 300.0,
        "end_distance_nm": 900.0,
    }
    spans = [
        RouteVerticalProfileHazardSpan(
            hazard_type="icing",
            source="Overlapping icing",
            **shared_geometry,
        ),
        RouteVerticalProfileHazardSpan(
            hazard_type="convective",
            source="Overlapping convection",
            **shared_geometry,
        ),
    ]

    svg = build_route_vertical_profile_svg(
        replace(profile, hazard_spans=spans),
        departure_label="KSTS",
        destination_label="KAUS",
        selected_flight_level_label="FL310",
    )
    root = ET.fromstring(svg)
    category_labels = [
        element
        for element in root.iter("{http://www.w3.org/2000/svg}text")
        if element.get("data-hazard-label") in {"ICING", "CONVECTIVE"}
    ]

    assert len(category_labels) == 2
    assert {element.get("text-anchor") for element in category_labels} == {"start"}
    assert len({float(element.get("y")) for element in category_labels}) == 2
    assert max(float(element.get("font-size")) for element in category_labels) <= 12.0
    label_boxes = []
    for element in category_labels:
        font_size = float(element.get("font-size"))
        label_x = float(element.get("x"))
        baseline_y = float(element.get("y"))
        rendered_width = float(element.get("textLength") or (len(element.text) * font_size * 0.58))
        label_boxes.append(
            (
                label_x,
                baseline_y - font_size - 2.0,
                label_x + rendered_width,
                baseline_y + 2.0,
            )
        )
    first_box, second_box = label_boxes
    assert (
        first_box[2] + 3.0 < second_box[0]
        or second_box[2] + 3.0 < first_box[0]
        or first_box[3] + 3.0 < second_box[1]
        or second_box[3] + 3.0 < first_box[1]
    )


def test_identical_thin_hazard_bands_keep_every_category_label_visible():
    """Use external vertical lanes when fully overlapping thin bands cannot fit two names."""

    dep = AirportData("KSTS", 38.5089, -122.8130, "US/Pacific", "test")
    arr = AirportData("KAUS", 30.1975, -97.6664, "US/Central", "test")
    profile = build_route_vertical_profile(
        dep,
        arr,
        hazard_areas=[],
        reference_time_utc=dt.datetime(2026, 7, 20, 17, 0, tzinfo=dt.timezone.utc),
        flight_level=310,
    )
    spans = [
        RouteVerticalProfileHazardSpan(
            hazard_type=hazard_type,
            severity_score=2,
            base_ft=12000,
            top_ft=12010,
            start_distance_nm=300.0,
            end_distance_nm=900.0,
            source=f"Thin {hazard_type}",
        )
        for hazard_type in ("icing", "convective")
    ]

    svg = build_route_vertical_profile_svg(
        replace(profile, hazard_spans=spans),
        departure_label="KSTS",
        destination_label="KAUS",
        selected_flight_level_label="FL310",
    )

    assert svg.count("data-hazard-label=") == 2
    assert ">ICING</text>" in svg
    assert ">CONVECTIVE</text>" in svg


def test_interactive_profile_legend_toggles_locally_without_streamlit_rerun():
    """The embedded legend uses browser-local click and keyboard handlers."""

    component_html = build_interactive_route_vertical_profile_html(
        '<svg><g data-hazard-type="icing"></g><g data-hazard-toggle="icing"></g></svg>'
    )

    assert "addEventListener('click'" in component_html
    assert "addEventListener('keydown'" in component_html
    assert "layer.style.display=visible?'':'none'" in component_html
    assert "classList.toggle('is-hidden',!visible)" in component_html
    assert "streamlit" not in component_html.lower()
