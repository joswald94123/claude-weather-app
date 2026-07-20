"""Render the route-context map with state outlines sized to the trip's geographic scope."""

from __future__ import annotations

import html
import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Callable
from zipfile import BadZipFile, ZipFile

from route_planning import RoutePlan, great_circle_distance_nm, route_sample_points

KML_NS = {"kml": "http://www.opengis.net/kml/2.2"}
STATE_BOUNDARY_ZIP = Path(__file__).resolve().parent / "assets" / "cb_2023_us_state_20m.zip"
STATE_BOUNDARY_KML = "cb_2023_us_state_20m.kml"
NON_CONTIGUOUS_CODES = {"AK", "HI", "PR", "GU", "AS", "MP", "VI"}
LOCAL_ROUTE_DISTANCE_NM = 900.0
NATIONAL_ROUTE_DISTANCE_NM = 1500.0
LOCAL_LONGITUDE_SPAN_DEG = 12.0
NATIONAL_LONGITUDE_SPAN_DEG = 25.0


# State boundaries are cached because the same asset drives every map render in a session.
@dataclass(frozen=True)
class StateBoundary:
    """One Census state boundary projected into the route-context SVG."""

    code: str
    name: str
    polygons: tuple[tuple[tuple[float, float], ...], ...]
    bbox: tuple[float, float, float, float]


def _simple_data(placemark: ET.Element, field_name: str) -> str:
    """Read one Census KML SimpleData value from a placemark."""

    return (
        placemark.findtext(
            f".//kml:SimpleData[@name='{field_name}']",
            default="",
            namespaces=KML_NS,
        )
        or ""
    ).strip()


def _parse_coordinates(raw_coordinates: str | None) -> tuple[tuple[float, float], ...]:
    """Parse KML coordinates into longitude/latitude tuples."""

    if not raw_coordinates:
        return ()

    points: list[tuple[float, float]] = []
    for token in raw_coordinates.replace("\n", " ").split():
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return tuple(points)


def _bbox_for_polygons(polygons: tuple[tuple[tuple[float, float], ...], ...]) -> tuple[float, float, float, float]:
    """Compute a longitude/latitude bounding box for one state's polygons."""

    longitudes = [point[0] for polygon in polygons for point in polygon]
    latitudes = [point[1] for polygon in polygons for point in polygon]
    return (
        min(longitudes),
        min(latitudes),
        max(longitudes),
        max(latitudes),
    )


@lru_cache(maxsize=1)
def load_state_boundaries() -> tuple[StateBoundary, ...]:
    """Load and cache state polygons from the vendored Census KML asset."""

    if not STATE_BOUNDARY_ZIP.exists():
        return ()

    try:
        with ZipFile(STATE_BOUNDARY_ZIP) as archive:
            root = ET.fromstring(archive.read(STATE_BOUNDARY_KML))
    except (BadZipFile, KeyError, OSError, ET.ParseError):
        # State outlines are useful context, not a prerequisite for flight
        # planning. Keep the route/range overlay available when the vendored
        # archive is incomplete or damaged.
        return ()

    states: list[StateBoundary] = []
    for placemark in root.findall(".//kml:Placemark", KML_NS):
        code = _simple_data(placemark, "STUSPS")
        name = _simple_data(placemark, "NAME")
        polygons: list[tuple[tuple[float, float], ...]] = []
        for polygon in placemark.findall(".//kml:Polygon", KML_NS):
            coordinates = polygon.findtext(
                "./kml:outerBoundaryIs/kml:LinearRing/kml:coordinates",
                default="",
                namespaces=KML_NS,
            )
            ring = _parse_coordinates(coordinates)
            if len(ring) >= 3:
                polygons.append(ring)
        if not code or not polygons:
            continue

        polygon_tuple = tuple(polygons)
        states.append(
            StateBoundary(
                code=code,
                name=name or code,
                polygons=polygon_tuple,
                bbox=_bbox_for_polygons(polygon_tuple),
            )
        )

    return tuple(sorted(states, key=lambda state: state.code))


def _sample_route_points(
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    *,
    samples: int = 96,
) -> tuple[tuple[float, float], ...]:
    """Sample a direct route leg for map scoping and drawing."""

    if samples <= 1:
        return (
            (departure_longitude, departure_latitude),
            (destination_longitude, destination_latitude),
        )

    return tuple(
        (
            departure_longitude + ((destination_longitude - departure_longitude) * fraction),
            departure_latitude + ((destination_latitude - departure_latitude) * fraction),
        )
        for fraction in (index / samples for index in range(samples + 1))
    )


def _route_points_for_display(
    *,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    route_plan: RoutePlan | None = None,
    samples: int = 96,
) -> tuple[tuple[float, float], ...]:
    """Return route sample points, using multi-leg geometry when present."""

    if route_plan is not None:
        # A polyline route needs denser sampling than a direct leg so map scope follows the flown path.
        return route_sample_points(route_plan, samples_per_leg=max(samples // max(len(route_plan.legs), 1), 8))
    return _sample_route_points(
        departure_latitude,
        departure_longitude,
        destination_latitude,
        destination_longitude,
        samples=samples,
    )


def _bbox_contains_point(
    bbox: tuple[float, float, float, float],
    point: tuple[float, float],
    *,
    padding_longitude: float = 0.0,
    padding_latitude: float = 0.0,
) -> bool:
    """Return whether a padded bounding box contains a longitude/latitude point."""

    longitude, latitude = point
    min_longitude, min_latitude, max_longitude, max_latitude = bbox
    return (
        (min_longitude - padding_longitude) <= longitude <= (max_longitude + padding_longitude)
        and (min_latitude - padding_latitude) <= latitude <= (max_latitude + padding_latitude)
    )


def _bbox_intersects(
    left: tuple[float, float, float, float],
    right: tuple[float, float, float, float],
) -> bool:
    """Return whether two longitude/latitude bounding boxes intersect."""

    left_min_lon, left_min_lat, left_max_lon, left_max_lat = left
    right_min_lon, right_min_lat, right_max_lon, right_max_lat = right
    return not (
        left_max_lon < right_min_lon
        or right_max_lon < left_min_lon
        or left_max_lat < right_min_lat
        or right_max_lat < left_min_lat
    )


def _union_bbox(
    bboxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, float, float]:
    """Return the smallest bounding box containing all provided boxes."""

    min_longitude = min(bbox[0] for bbox in bboxes)
    min_latitude = min(bbox[1] for bbox in bboxes)
    max_longitude = max(bbox[2] for bbox in bboxes)
    max_latitude = max(bbox[3] for bbox in bboxes)
    return min_longitude, min_latitude, max_longitude, max_latitude


def _bbox_from_points(points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
    """Return the smallest bounding box containing all route sample points."""

    longitudes = [point[0] for point in points]
    latitudes = [point[1] for point in points]
    return min(longitudes), min(latitudes), max(longitudes), max(latitudes)


def _expand_bbox(
    bbox: tuple[float, float, float, float],
    *,
    padding_longitude: float,
    padding_latitude: float,
) -> tuple[float, float, float, float]:
    """Pad a bounding box by longitude and latitude degrees."""

    min_longitude, min_latitude, max_longitude, max_latitude = bbox
    return (
        min_longitude - padding_longitude,
        min_latitude - padding_latitude,
        max_longitude + padding_longitude,
        max_latitude + padding_latitude,
    )


def _corridor_state_codes(
    route_points: tuple[tuple[float, float], ...],
    states: tuple[StateBoundary, ...],
) -> set[str]:
    """Choose state codes touched by the padded route corridor."""

    if not route_points or not states:
        return set()

    route_bbox = _bbox_from_points(route_points)
    latitude_span = max(route_bbox[3] - route_bbox[1], 0.0)
    longitude_span = max(route_bbox[2] - route_bbox[0], 0.0)
    padding_latitude = max(0.75, min(1.45, (latitude_span * 0.09) + 0.35))
    padding_longitude = max(0.95, min(1.65, (longitude_span * 0.08) + 0.45))

    corridor_codes: set[str] = set()
    for state in states:
        if any(
            _bbox_contains_point(
                state.bbox,
                point,
                padding_longitude=padding_longitude,
                padding_latitude=padding_latitude,
            )
            for point in route_points
        ):
            corridor_codes.add(state.code)

    if corridor_codes:
        return corridor_codes

    fallback_bbox = _expand_bbox(
        route_bbox,
        padding_longitude=padding_longitude,
        padding_latitude=padding_latitude,
    )
    return {state.code for state in states if _bbox_intersects(state.bbox, fallback_bbox)}


def _contiguous_states(states: tuple[StateBoundary, ...]) -> tuple[StateBoundary, ...]:
    """Filter out non-contiguous states for lower-48 map mode."""

    return tuple(state for state in states if state.code not in NON_CONTIGUOUS_CODES)


def _regional_states_for_route(
    route_points: tuple[tuple[float, float], ...],
    states: tuple[StateBoundary, ...],
) -> tuple[StateBoundary, ...]:
    """Expand the route bbox enough to keep nearby regional context without going national."""

    route_bbox = _bbox_from_points(route_points)
    latitude_span = max(route_bbox[3] - route_bbox[1], 0.0)
    longitude_span = max(route_bbox[2] - route_bbox[0], 0.0)
    regional_bbox = _expand_bbox(
        route_bbox,
        padding_longitude=max(4.0, min(8.0, (longitude_span * 0.28) + 2.0)),
        padding_latitude=max(3.0, min(6.5, (latitude_span * 0.30) + 1.8)),
    )
    return tuple(
        sorted(
            [state for state in states if _bbox_intersects(state.bbox, regional_bbox)],
            key=lambda state: state.code,
        )
    )


def _map_scope_for_route(
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    route_plan: RoutePlan | None = None,
) -> str:
    """Classify route extent as corridor, regional, or lower-48."""

    route_distance_nm = (
        route_plan.total_distance_nm
        if route_plan is not None
        else great_circle_distance_nm(
            departure_latitude,
            departure_longitude,
            destination_latitude,
            destination_longitude,
        )
    )
    route_points = _route_points_for_display(
        departure_latitude=departure_latitude,
        departure_longitude=departure_longitude,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        route_plan=route_plan,
        samples=48,
    )
    route_bbox = _bbox_from_points(route_points)
    longitude_span = abs(route_bbox[2] - route_bbox[0])

    if route_distance_nm >= NATIONAL_ROUTE_DISTANCE_NM or longitude_span >= NATIONAL_LONGITUDE_SPAN_DEG:
        return "lower48"
    if route_distance_nm >= LOCAL_ROUTE_DISTANCE_NM or longitude_span >= LOCAL_LONGITUDE_SPAN_DEG:
        return "regional"
    return "corridor"


def select_states_for_route(
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    route_plan: RoutePlan | None = None,
) -> tuple[StateBoundary, ...]:
    """Choose corridor, regional, or lower-48 context based on route span."""

    states = load_state_boundaries()
    if not states:
        return ()

    route_points = _route_points_for_display(
        departure_latitude=departure_latitude,
        departure_longitude=departure_longitude,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        route_plan=route_plan,
        samples=96,
    )
    map_scope = _map_scope_for_route(
        departure_latitude,
        departure_longitude,
        destination_latitude,
        destination_longitude,
        route_plan=route_plan,
    )
    contiguous_states = _contiguous_states(states)
    if map_scope == "lower48":
        return contiguous_states
    if map_scope == "regional":
        regional_states = _regional_states_for_route(route_points, contiguous_states)
        if regional_states:
            return regional_states

    route_bbox = _bbox_from_points(route_points)
    latitude_span = max(route_bbox[3] - route_bbox[1], 0.0)
    longitude_span = max(route_bbox[2] - route_bbox[0], 0.0)
    padding_latitude = max(1.2, min(2.4, (latitude_span * 0.10) + 0.8))
    padding_longitude = max(1.4, min(2.8, (longitude_span * 0.10) + 0.9))

    selected_states = []
    for state in contiguous_states:
        if any(
            _bbox_contains_point(
                state.bbox,
                point,
                padding_longitude=padding_longitude,
                padding_latitude=padding_latitude,
            )
            for point in route_points
        ):
            selected_states.append(state)

    if not selected_states:
        corridor_codes = _corridor_state_codes(route_points, contiguous_states)
        selected_states = [state for state in contiguous_states if state.code in corridor_codes]

    return tuple(sorted(selected_states, key=lambda state: state.code))


def _screen_projection(
    *,
    bbox: tuple[float, float, float, float],
    width: int,
    height: int,
    frame: float,
    projection: str = "local",
) -> Callable[[float, float], tuple[float, float]]:
    """Build a geographic-to-screen projection with one uniform SVG scale."""

    mean_latitude = (bbox[1] + bbox[3]) / 2.0
    longitude_scale = max(math.cos(math.radians(mean_latitude)), 0.45)

    def project(longitude: float, latitude: float) -> tuple[float, float]:
        if projection == "albers":
            # Spherical Albers equal-area parameters commonly used for the
            # contiguous United States. Regional/national maps need this
            # conformal-looking curvature more than local corridor maps do.
            phi = math.radians(latitude)
            lam = math.radians(longitude)
            phi_1 = math.radians(29.5)
            phi_2 = math.radians(45.5)
            phi_0 = math.radians(23.0)
            lam_0 = math.radians(-96.0)
            n = 0.5 * (math.sin(phi_1) + math.sin(phi_2))
            c = (math.cos(phi_1) ** 2) + (2.0 * n * math.sin(phi_1))
            rho = math.sqrt(max(c - (2.0 * n * math.sin(phi)), 0.0)) / n
            rho_0 = math.sqrt(max(c - (2.0 * n * math.sin(phi_0)), 0.0)) / n
            theta = n * (lam - lam_0)
            return rho * math.sin(theta), rho_0 - (rho * math.cos(theta))
        return longitude * longitude_scale, latitude

    # Curved projections can reach extrema between bbox corners, so sample
    # every edge before fitting the map into the SVG frame.
    edge_points: list[tuple[float, float]] = []
    for step in range(17):
        fraction = step / 16.0
        longitude = bbox[0] + ((bbox[2] - bbox[0]) * fraction)
        latitude = bbox[1] + ((bbox[3] - bbox[1]) * fraction)
        edge_points.extend(
            [
                project(longitude, bbox[1]),
                project(longitude, bbox[3]),
                project(bbox[0], latitude),
                project(bbox[2], latitude),
            ]
        )
    projected_min_x = min(point[0] for point in edge_points)
    projected_max_x = max(point[0] for point in edge_points)
    projected_min_y = min(point[1] for point in edge_points)
    projected_max_y = max(point[1] for point in edge_points)
    span_x = max(projected_max_x - projected_min_x, 1.0)
    span_y = max(projected_max_y - projected_min_y, 1.0)
    drawable_width = max(float(width) - (frame * 2.0), 10.0)
    drawable_height = max(float(height) - (frame * 2.0), 10.0)
    scale = min(drawable_width / span_x, drawable_height / span_y)
    origin_x = frame + ((drawable_width - (span_x * scale)) / 2.0)
    origin_y = frame + ((drawable_height - (span_y * scale)) / 2.0)

    def screen_point(longitude: float, latitude: float) -> tuple[float, float]:
        projected_x, projected_y = project(longitude, latitude)
        x = origin_x + ((projected_x - projected_min_x) * scale)
        y = float(height) - origin_y - ((projected_y - projected_min_y) * scale)
        return round(x, 2), round(y, 2)

    return screen_point


def build_range_inset_svg(
    *,
    anchor_label: str,
    anchor_latitude: float,
    anchor_longitude: float,
    range_rings: tuple[object, ...],
    title: str = "Fuel Range",
    width: int = 520,
    height: int = 360,
) -> str:
    """Render one local inset around a waypoint and its wind-shaped fuel range rings."""

    ring_points = [
        point
        for ring in range_rings
        for point in getattr(ring, "points", ())
    ]
    anchor_point = (anchor_longitude, anchor_latitude)
    if ring_points:
        base_bbox = _bbox_from_points((anchor_point, *tuple(ring_points)))
    else:
        base_bbox = _expand_bbox(
            _bbox_from_points((anchor_point,)),
            padding_longitude=1.0,
            padding_latitude=0.8,
        )

    latitude_span = max(base_bbox[3] - base_bbox[1], 0.0)
    longitude_span = max(base_bbox[2] - base_bbox[0], 0.0)
    padded_bbox = _expand_bbox(
        base_bbox,
        padding_longitude=max(0.6, min(2.0, (longitude_span * 0.12) + 0.35)),
        padding_latitude=max(0.45, min(1.6, (latitude_span * 0.12) + 0.28)),
    )

    states = tuple(
        sorted(
            [
                state
                for state in _contiguous_states(load_state_boundaries())
                if _bbox_intersects(state.bbox, padded_bbox)
            ],
            key=lambda state: state.code,
        )
    )
    screen_point = _screen_projection(bbox=padded_bbox, width=width, height=height, frame=22.0)

    state_paths: list[str] = []
    for state in states:
        for polygon in state.polygons:
            if len(polygon) < 3:
                continue
            commands = []
            for index, (longitude, latitude) in enumerate(polygon):
                x, y = screen_point(longitude, latitude)
                commands.append(f"{'M' if index == 0 else 'L'} {x} {y}")
            commands.append("Z")
            state_paths.append(
                f"<path d=\"{' '.join(commands)}\" fill=\"#e2eadf\" fill-opacity=\"0.74\" "
                "stroke=\"#8e8578\" stroke-width=\"1.0\" stroke-linejoin=\"round\"/>"
            )

    ring_markup = ""
    if range_rings:
        ring_parts: list[str] = []
        for ring in range_rings:
            points = tuple(getattr(ring, "points", ()))
            if len(points) < 3:
                continue
            polyline_points = " ".join(
                f"{x},{y}"
                for x, y in (
                    screen_point(float(longitude), float(latitude))
                    for longitude, latitude in points + (points[0],)
                )
            )
            label = str(getattr(ring, "label", "Range"))
            dash = str(getattr(ring, "line_style", "8 5"))
            average_range_nm = float(getattr(ring, "alt_average_range_nm", 0.0) or 0.0)
            title_text = f"{label} average range {average_range_nm:.0f} NM"
            ring_parts.append(
                f"<polyline points=\"{polyline_points}\" fill=\"rgba(123, 63, 152, 0.035)\" "
                "stroke=\"#7b3f98\" stroke-width=\"2.0\" "
                f"stroke-dasharray=\"{html.escape(dash)}\" stroke-linejoin=\"round\">"
                f"<title>{html.escape(title_text)}</title>"
                "</polyline>"
            )
            label_longitude, label_latitude = points[0]
            label_x, label_y = screen_point(float(label_longitude), float(label_latitude))
            ring_parts.append(
                f"<text x=\"{label_x}\" y=\"{label_y - 6}\" text-anchor=\"middle\" "
                "font-size=\"11\" font-weight=\"800\" fill=\"#7b3f98\">"
                f"{html.escape(label)}</text>"
            )
        ring_markup = "".join(ring_parts)

    anchor_x, anchor_y = screen_point(anchor_longitude, anchor_latitude)
    label_y = anchor_y - 13 if anchor_y > 48 else anchor_y + 23
    empty_markup = ""
    if not range_rings:
        empty_markup = (
            f"<text x=\"{width / 2:.1f}\" y=\"{height / 2:.1f}\" text-anchor=\"middle\" "
            "font-size=\"13\" font-weight=\"700\" fill=\"#6b6760\">No range available</text>"
        )

    return (
        f"<svg viewBox=\"0 0 {width} {height}\" width=\"100%\" height=\"100%\" "
        "preserveAspectRatio=\"xMidYMid meet\" xmlns=\"http://www.w3.org/2000/svg\">"
        f"<rect width=\"100%\" height=\"100%\" rx=\"18\" fill=\"#fbfaf6\"/>"
        f"<text x=\"18\" y=\"28\" font-size=\"14\" font-weight=\"800\" fill=\"#153646\">"
        f"{html.escape(title)}</text>"
        f"{''.join(state_paths)}"
        f"{ring_markup}"
        f"{empty_markup}"
        f"<circle cx=\"{anchor_x}\" cy=\"{anchor_y}\" r=\"7\" fill=\"#b75738\" "
        "stroke=\"#f7f3eb\" stroke-width=\"3\"/>"
        f"<text x=\"{anchor_x}\" y=\"{label_y}\" text-anchor=\"middle\" font-size=\"13\" "
        "font-weight=\"800\" fill=\"#b75738\">"
        f"{html.escape(anchor_label)}</text>"
        "</svg>"
    )


def build_route_context_svg(
    *,
    departure_label: str,
    departure_latitude: float,
    departure_longitude: float,
    destination_label: str,
    destination_latitude: float,
    destination_longitude: float,
    route_plan: RoutePlan | None = None,
    alternate_range_rings: tuple[object, ...] = (),
    width: int = 960,
    height: int = 500,
) -> str:
    """Render the cropped route map while preserving geographic aspect ratio."""

    states = select_states_for_route(
        departure_latitude,
        departure_longitude,
        destination_latitude,
        destination_longitude,
        route_plan=route_plan,
    )
    route_points = _route_points_for_display(
        departure_latitude=departure_latitude,
        departure_longitude=departure_longitude,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        route_plan=route_plan,
        samples=72,
    )
    ring_points = [
        point
        for ring in alternate_range_rings
        for point in getattr(ring, "points", ())
    ]
    corridor_codes = _corridor_state_codes(route_points, load_state_boundaries())

    state_boxes = [state.bbox for state in states]
    map_points = tuple(route_points) + tuple(ring_points)
    base_bbox = _union_bbox(state_boxes + [_bbox_from_points(map_points)]) if state_boxes else _bbox_from_points(map_points)

    latitude_span = max(base_bbox[3] - base_bbox[1], 0.0)
    longitude_span = max(base_bbox[2] - base_bbox[0], 0.0)
    padded_bbox = _expand_bbox(
        base_bbox,
        padding_longitude=max(1.4, min(3.2, (longitude_span * 0.10) + 0.7)),
        padding_latitude=max(1.0, min(2.8, (latitude_span * 0.12) + 0.6)),
    )

    map_scope = _map_scope_for_route(
        departure_latitude,
        departure_longitude,
        destination_latitude,
        destination_longitude,
        route_plan=route_plan,
    )
    screen_point = _screen_projection(
        bbox=padded_bbox,
        width=width,
        height=height,
        frame=24.0,
        projection="albers" if map_scope in {"regional", "lower48"} else "local",
    )

    state_paths: list[str] = []
    for state in states:
        fill = "#dcebe2" if state.code not in corridor_codes else "#e8d7bb"
        for polygon in state.polygons:
            if len(polygon) < 3:
                continue
            commands = []
            for index, (longitude, latitude) in enumerate(polygon):
                x, y = screen_point(longitude, latitude)
                commands.append(f"{'M' if index == 0 else 'L'} {x} {y}")
            commands.append("Z")
            state_paths.append(
                f"<path d=\"{' '.join(commands)}\" fill=\"{fill}\" fill-opacity=\"0.72\" "
                "stroke=\"#8e8578\" stroke-width=\"1.2\" stroke-linejoin=\"round\"/>"
            )

    route_polyline = " ".join(
        f"{x},{y}" for x, y in (screen_point(longitude, latitude) for longitude, latitude in route_points)
    )
    departure_x, departure_y = screen_point(departure_longitude, departure_latitude)
    destination_x, destination_y = screen_point(destination_longitude, destination_latitude)
    departure_label_y = departure_y - 12 if departure_y > 48 else departure_y + 22
    destination_label_y = destination_y - 12 if destination_y > 48 else destination_y + 22

    state_label_markup = ""
    if len(states) <= 12:
        labels: list[str] = []
        for state in states:
            min_longitude, min_latitude, max_longitude, max_latitude = state.bbox
            label_x, label_y = screen_point(
                (min_longitude + max_longitude) / 2.0,
                (min_latitude + max_latitude) / 2.0,
            )
            labels.append(
                f"<text x=\"{label_x}\" y=\"{label_y}\" text-anchor=\"middle\" "
                "font-size=\"13\" font-weight=\"700\" fill=\"#6b6760\" fill-opacity=\"0.80\">"
                f"{html.escape(state.code)}</text>"
            )
        state_label_markup = "".join(labels)

    waypoint_markup = ""
    if route_plan is not None and len(route_plan.waypoints) > 2:
        # Intermediate fixes get their own markers so the map and profile tell the same route story.
        waypoint_parts: list[str] = []
        for waypoint in route_plan.waypoints[1:-1]:
            waypoint_x, waypoint_y = screen_point(waypoint.longitude, waypoint.latitude)
            label_y = waypoint_y - 12 if waypoint_y > 48 else waypoint_y + 20
            is_fuel_stop = bool(getattr(waypoint, "is_fuel_stop", False))
            marker_fill = "#0f5f67" if is_fuel_stop else "#cf8c2b"
            marker_radius = 7.0 if is_fuel_stop else 5.5
            label_fill = "#0f5f67" if is_fuel_stop else "#7f5e26"
            waypoint_parts.append(
                "<g>"
                f"<title>{html.escape(waypoint.identifier)}"
                f"{' fuel stop' if is_fuel_stop else ' route waypoint'}</title>"
                f"<circle cx=\"{waypoint_x}\" cy=\"{waypoint_y}\" r=\"{marker_radius}\" fill=\"{marker_fill}\" "
                "stroke=\"#f7f3eb\" stroke-width=\"2\"/>"
                "</g>"
            )
            waypoint_parts.append(
                f"<text x=\"{waypoint_x}\" y=\"{label_y}\" text-anchor=\"middle\" font-size=\"12.5\" "
                f"font-weight=\"700\" fill=\"{label_fill}\">"
                f"{html.escape(waypoint.identifier)}</text>"
            )
        waypoint_markup = "".join(waypoint_parts)

    range_ring_markup = ""
    if alternate_range_rings:
        ring_parts: list[str] = []
        for ring in alternate_range_rings:
            points = getattr(ring, "points", ())
            if len(points) < 3:
                continue
            polyline_points = " ".join(
                f"{x},{y}"
                for x, y in (
                    screen_point(float(longitude), float(latitude))
                    for longitude, latitude in tuple(points) + (points[0],)
                )
            )
            label = str(getattr(ring, "label", "Range"))
            dash = str(getattr(ring, "line_style", "8 5"))
            ring_parts.append(
                f"<polyline points=\"{polyline_points}\" fill=\"rgba(15, 118, 110, 0.03)\" "
                "stroke=\"#7b3f98\" stroke-width=\"2.2\" "
                f"stroke-dasharray=\"{html.escape(dash)}\" stroke-linejoin=\"round\">"
                f"<title>Post-missed alternate range {html.escape(label)}</title>"
                "</polyline>"
            )
            label_longitude, label_latitude = points[0]
            label_x, label_y = screen_point(float(label_longitude), float(label_latitude))
            ring_parts.append(
                f"<text x=\"{label_x}\" y=\"{label_y - 6}\" text-anchor=\"middle\" "
                "font-size=\"12\" font-weight=\"800\" fill=\"#7b3f98\">"
                f"{html.escape(label)}</text>"
            )
        range_ring_markup = "".join(ring_parts)

    legend_items = [
        '<line x1="38" y1="462" x2="62" y2="462" stroke="#0f5f67" stroke-width="4" '
        'stroke-linecap="round"/><text x="69" y="466" font-size="11.5" font-weight="700" '
        'fill="#42545c">Route</text>',
        '<circle cx="125" cy="462" r="5.5" fill="#0f5f67" stroke="#f7f3eb" stroke-width="2"/>'
        '<text x="136" y="466" font-size="11.5" font-weight="700" fill="#42545c">Fuel stop</text>',
        '<circle cx="222" cy="462" r="5.5" fill="#b75738" stroke="#f7f3eb" stroke-width="2"/>'
        '<text x="233" y="466" font-size="11.5" font-weight="700" fill="#42545c">Destination</text>',
    ]
    legend_width = 306
    if alternate_range_rings:
        legend_items.append(
            '<line x1="320" y1="462" x2="344" y2="462" stroke="#7b3f98" stroke-width="2" '
            'stroke-dasharray="7 4"/><text x="351" y="466" font-size="11.5" font-weight="700" '
            'fill="#42545c">Range</text>'
        )
        legend_width = 398
    legend_y = height - 58
    # Keep the key anchored inside the frame even when callers request a non-default height.
    legend_markup = (
        f'<g transform="translate(0 {legend_y - 442})">'
        f'<rect x="24" y="442" width="{legend_width}" height="38" rx="12" '
        'fill="#fbfaf6" fill-opacity="0.94" stroke="#d8d2c8" stroke-width="1"/>'
        f'{"".join(legend_items)}</g>'
    )

    return (
        f"<svg viewBox=\"0 0 {width} {height}\" width=\"100%\" height=\"100%\" "
        "preserveAspectRatio=\"xMidYMid meet\" role=\"img\" "
        f"aria-label=\"Route from {html.escape(departure_label)} to {html.escape(destination_label)}\" "
        "xmlns=\"http://www.w3.org/2000/svg\">"
        f"<title>Route from {html.escape(departure_label)} to {html.escape(destination_label)}</title>"
        "<defs>"
        "<filter id=\"routeGlow\" x=\"-30%\" y=\"-30%\" width=\"160%\" height=\"160%\">"
        "<feGaussianBlur stdDeviation=\"4\" result=\"blur\"/>"
        "<feMerge><feMergeNode in=\"blur\"/><feMergeNode in=\"SourceGraphic\"/></feMerge>"
        "</filter>"
        "</defs>"
        "<rect width=\"100%\" height=\"100%\" rx=\"28\" fill=\"#f7f3eb\"/>"
        f"<rect x=\"10\" y=\"10\" width=\"{width - 20}\" height=\"{height - 20}\" rx=\"24\" "
        "fill=\"#fbfaf6\" stroke=\"#d8d2c8\" stroke-width=\"1.2\"/>"
        f"{''.join(state_paths)}"
        f"{state_label_markup}"
        f"{range_ring_markup}"
        f"<polyline points=\"{route_polyline}\" fill=\"none\" stroke=\"#cf8c2b\" stroke-opacity=\"0.35\" "
        "stroke-width=\"10\" stroke-linecap=\"round\" stroke-linejoin=\"round\" filter=\"url(#routeGlow)\"/>"
        f"<polyline points=\"{route_polyline}\" fill=\"none\" stroke=\"#0f5f67\" stroke-width=\"4.5\" "
        "stroke-linecap=\"round\" stroke-linejoin=\"round\"/>"
        f"{waypoint_markup}"
        f"<circle cx=\"{departure_x}\" cy=\"{departure_y}\" r=\"7\" fill=\"#0f5f67\" stroke=\"#f7f3eb\" stroke-width=\"3\"/>"
        f"<circle cx=\"{destination_x}\" cy=\"{destination_y}\" r=\"7\" fill=\"#b75738\" stroke=\"#f7f3eb\" stroke-width=\"3\"/>"
        f"<text x=\"{departure_x}\" y=\"{departure_label_y}\" text-anchor=\"middle\" font-size=\"15\" "
        "font-weight=\"800\" fill=\"#0f5f67\">"
        f"{html.escape(departure_label)}</text>"
        f"<text x=\"{destination_x}\" y=\"{destination_label_y}\" text-anchor=\"middle\" font-size=\"15\" "
        "font-weight=\"800\" fill=\"#b75738\">"
        f"{html.escape(destination_label)}</text>"
        f"{legend_markup}"
        "</svg>"
    )
