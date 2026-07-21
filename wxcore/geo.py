"""Polygon geometry, route sampling, and hazard time-validity shared by feeds and mission."""

from __future__ import annotations

import datetime as dt
import math


from route_planning import (
    RoutePlan,
    great_circle_distance_nm as _route_great_circle_distance_nm,
    initial_track_deg as _route_initial_track_deg,
    route_point_at_distance_nm as _route_plan_point_at_distance_nm,
    route_track_at_distance_nm as _route_plan_track_at_distance_nm,
)

from .models import (
    GAIRMET_HORIZON_FALLBACK_LIMIT,
    TCF_VALIDITY_HALF_WINDOW,
    RISK_LABEL_BY_SCORE,
    HazardArea,
    SegmentHazard,
    _safe_float,
)


def great_circle_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute distance in nautical miles using the shared route geometry helper."""

    return _route_great_circle_distance_nm(lat1, lon1, lat2, lon2)


def _initial_track_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute initial true course using the shared route geometry helper."""

    return _route_initial_track_deg(lat1, lon1, lat2, lon2)


def _route_point_at_distance_nm(
    *,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    mission_distance_nm: float,
    distance_from_departure_nm: float,
    route_plan: RoutePlan | None = None,
) -> tuple[float, float]:
    """Return the route point at a distance, using a planned route when available."""

    if route_plan is not None:
        return _route_plan_point_at_distance_nm(route_plan, distance_from_departure_nm)

    if mission_distance_nm <= 0.0:
        return departure_latitude, departure_longitude

    fraction = min(max(distance_from_departure_nm / mission_distance_nm, 0.0), 1.0)
    latitude = departure_latitude + ((destination_latitude - departure_latitude) * fraction)
    longitude = departure_longitude + ((destination_longitude - departure_longitude) * fraction)
    return latitude, longitude


def _route_track_at_distance_nm(
    *,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    distance_from_departure_nm: float,
    route_plan: RoutePlan | None = None,
) -> float:
    """Return route track at a distance, using waypoint-leg geometry when available."""

    if route_plan is not None:
        return _route_plan_track_at_distance_nm(route_plan, distance_from_departure_nm)
    return _initial_track_deg(
        departure_latitude,
        departure_longitude,
        destination_latitude,
        destination_longitude,
    )


def _polygon_from_latlon_dicts(coords: object) -> list[tuple[float, float]]:
    """Convert API lat/lon dictionaries into an internal polygon point list."""

    polygon: list[tuple[float, float]] = []
    if not isinstance(coords, list):
        return polygon
    for point in coords:
        if not isinstance(point, dict):
            continue
        lat = _safe_float(point.get("lat"))
        lon = _safe_float(point.get("lon"))
        if lat is None or lon is None:
            continue
        polygon.append((lat, lon))
    return polygon


def _polygons_from_geojson_geometry(geometry: object) -> list[list[tuple[float, float]]]:
    """Convert GeoJSON Polygon or MultiPolygon geometry into internal polygons."""

    if not isinstance(geometry, dict):
        return []
    geo_type = str(geometry.get("type") or "")
    coordinates = geometry.get("coordinates")
    polygons: list[list[tuple[float, float]]] = []

    if geo_type == "Polygon" and isinstance(coordinates, list):
        rings = coordinates
    elif geo_type == "MultiPolygon" and isinstance(coordinates, list):
        rings = [ring for poly in coordinates if isinstance(poly, list) for ring in poly]
    else:
        rings = []

    for ring in rings:
        if not isinstance(ring, list):
            continue
        polygon: list[tuple[float, float]] = []
        for point in ring:
            if not isinstance(point, list) or len(point) < 2:
                continue
            lon = _safe_float(point[0])
            lat = _safe_float(point[1])
            if lat is None or lon is None:
                continue
            polygon.append((lat, lon))
        if len(polygon) >= 3:
            polygons.append(polygon)
    return polygons


def _circle_polygon_nm(lat: float, lon: float, radius_nm: float = 20.0, points: int = 18) -> list[tuple[float, float]]:
    """Approximate a localized point report as a small aviation-scale hazard area."""

    polygon: list[tuple[float, float]] = []
    lat_rad = math.radians(lat)
    nm_per_lon_degree = max(60.0 * math.cos(lat_rad), 1.0)
    for idx in range(points):
        bearing = (2.0 * math.pi * idx) / points
        d_lat = math.cos(bearing) * radius_nm / 60.0
        d_lon = math.sin(bearing) * radius_nm / nm_per_lon_degree
        polygon.append((lat + d_lat, lon + d_lon))
    return polygon


def _point_in_polygon(lat: float, lon: float, polygon: list[tuple[float, float]]) -> bool:
    """Return whether a latitude/longitude point falls inside one polygon."""

    if len(polygon) < 3:
        return False
    x = lon
    y = lat
    inside = False
    for idx in range(len(polygon)):
        y1, x1 = polygon[idx]
        y2, x2 = polygon[(idx + 1) % len(polygon)]
        intersects = ((y1 > y) != (y2 > y)) and (
            x < ((x2 - x1) * (y - y1) / ((y2 - y1) + 1e-12) + x1)
        )
        if intersects:
            inside = not inside
    return inside


def _point_in_any_polygon(lat: float, lon: float, polygons: list[list[tuple[float, float]]]) -> bool:
    """Return whether a point falls inside any polygon in a hazard area."""

    return any(_point_in_polygon(lat, lon, polygon) for polygon in polygons)


def _segments_intersect_xy(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float], d: tuple[float, float]) -> bool:
    """Planar segment intersection for short route/polygon spans in lon-lat space."""

    def orientation(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> float:
        return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])

    def on_segment(p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]) -> bool:
        return (
            min(p[0], r[0]) - 1e-9 <= q[0] <= max(p[0], r[0]) + 1e-9
            and min(p[1], r[1]) - 1e-9 <= q[1] <= max(p[1], r[1]) + 1e-9
        )

    o1 = orientation(a, b, c)
    o2 = orientation(a, b, d)
    o3 = orientation(c, d, a)
    o4 = orientation(c, d, b)
    if o1 * o2 < 0 and o3 * o4 < 0:
        return True
    return (
        abs(o1) < 1e-9 and on_segment(a, c, b)
        or abs(o2) < 1e-9 and on_segment(a, d, b)
        or abs(o3) < 1e-9 and on_segment(c, a, d)
        or abs(o4) < 1e-9 and on_segment(c, b, d)
    )


def _route_samples_for_interval(
    *,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    mission_distance_nm: float,
    start_distance_nm: float,
    end_distance_nm: float,
    route_plan: RoutePlan | None,
    max_sample_spacing_nm: float,
) -> list[tuple[float, float]]:
    """Sample route coordinates over one distance interval."""

    bounded_start = min(max(start_distance_nm, 0.0), mission_distance_nm)
    bounded_end = min(max(end_distance_nm, bounded_start), mission_distance_nm)
    interval_nm = max(bounded_end - bounded_start, 0.0)
    sample_count = max(3, int(math.ceil(interval_nm / max(max_sample_spacing_nm, 1.0))) + 1)
    samples: list[tuple[float, float]] = []
    for sample_idx in range(sample_count):
        fraction = sample_idx / max(sample_count - 1, 1)
        sample_distance_nm = bounded_start + (interval_nm * fraction)
        samples.append(
            _route_point_at_distance_nm(
                departure_latitude=departure_latitude,
                departure_longitude=departure_longitude,
                destination_latitude=destination_latitude,
                destination_longitude=destination_longitude,
                mission_distance_nm=mission_distance_nm,
                distance_from_departure_nm=sample_distance_nm,
                route_plan=route_plan,
            )
        )
    return samples


def _route_samples_cross_polygon_edges(
    samples: list[tuple[float, float]],
    polygons: list[list[tuple[float, float]]],
) -> bool:
    """Return whether sampled route segments cross any polygon edge."""

    for route_start, route_end in zip(samples, samples[1:]):
        route_a = (route_start[1], route_start[0])
        route_b = (route_end[1], route_end[0])
        for polygon in polygons:
            for poly_start, poly_end in zip(polygon, polygon[1:] + polygon[:1]):
                poly_a = (poly_start[1], poly_start[0])
                poly_b = (poly_end[1], poly_end[0])
                if _segments_intersect_xy(route_a, route_b, poly_a, poly_b):
                    return True
    return False


def _route_interval_intersects_polygons(
    *,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    mission_distance_nm: float,
    start_distance_nm: float,
    end_distance_nm: float,
    polygons: list[list[tuple[float, float]]],
    route_plan: RoutePlan | None = None,
    max_sample_spacing_nm: float = 5.0,
) -> bool:
    """Sample a scored interval so narrow route crossings are not reduced to one midpoint."""

    if not polygons:
        return False
    bounded_start = min(max(start_distance_nm, 0.0), mission_distance_nm)
    bounded_end = min(max(end_distance_nm, bounded_start), mission_distance_nm)
    samples = _route_samples_for_interval(
        departure_latitude=departure_latitude,
        departure_longitude=departure_longitude,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        mission_distance_nm=mission_distance_nm,
        start_distance_nm=bounded_start,
        end_distance_nm=bounded_end,
        route_plan=route_plan,
        max_sample_spacing_nm=max_sample_spacing_nm,
    )
    for sample_latitude, sample_longitude in samples:
        if _point_in_any_polygon(sample_latitude, sample_longitude, polygons):
            return True
    return _route_samples_cross_polygon_edges(samples, polygons)


def _hazard_valid_for_time(hazard: HazardArea, reference_time_utc: dt.datetime | None) -> bool:
    """Return whether a hazard is valid at the route reference time."""

    if reference_time_utc is None:
        return True

    start = hazard.valid_from_utc
    end = hazard.valid_to_utc
    if start and end:
        if start == end:
            return abs((reference_time_utc - start).total_seconds()) <= TCF_VALIDITY_HALF_WINDOW.total_seconds()
        return start <= reference_time_utc <= end
    if start:
        return reference_time_utc >= start
    if end:
        return reference_time_utc <= end
    return True


def _latest_gairmet_valid_to_by_source(hazard_areas: list[HazardArea]) -> dict[str, dt.datetime]:
    """Index the last available G-AIRMET snapshot for each advisory identity."""

    latest: dict[str, dt.datetime] = {}
    for area in hazard_areas:
        if not area.source.startswith("G-AIRMET") or area.valid_to_utc is None:
            continue
        latest[area.source] = max(latest.get(area.source, area.valid_to_utc), area.valid_to_utc)
    return latest


def hazard_applies_at(
    hazard: HazardArea,
    reference_time_utc: dt.datetime | None,
    latest_gairmet_valid_to_by_source: dict[str, dt.datetime],
) -> tuple[bool, bool]:
    """Return (applies, used_horizon_fallback) for one hazard at a reference time.

    Every hazard consumer must share this decision so the segment table and the
    vertical profile can never disagree. G-AIRMET snapshots stop at the product's
    forecast horizon; reference times up to GAIRMET_HORIZON_FALLBACK_LIMIT past
    the last snapshot reuse it (labeled) rather than silently showing clear air.
    Beyond that limit the product carries no meaningful signal for the time in
    question, so the hazard is dropped.
    """

    if _hazard_valid_for_time(hazard, reference_time_utc):
        return True, False
    if (
        reference_time_utc is not None
        and hazard.source.startswith("G-AIRMET")
        and hazard.valid_to_utc is not None
        and hazard.valid_to_utc == latest_gairmet_valid_to_by_source.get(hazard.source)
        and hazard.valid_to_utc
        < reference_time_utc
        <= hazard.valid_to_utc + GAIRMET_HORIZON_FALLBACK_LIMIT
    ):
        return True, True
    return False, False


def hazard_label(score: int) -> str:
    """Translate a numeric hazard score into a display label."""

    return RISK_LABEL_BY_SCORE.get(int(max(0, min(3, score))), "None")


def summarize_segment_hazard(
    segment_hazards: list[SegmentHazard],
    *,
    score_field: str,
) -> str:
    """Summarize the worst scored segment hazard for one hazard dimension."""

    if not segment_hazards:
        return "None"
    scores = [int(getattr(row, score_field, 0)) for row in segment_hazards]
    if not scores:
        return "None"
    max_score = max(scores)
    if max_score <= 0:
        return "None"
    impacted = sum(1 for score in scores if score > 0)
    return f"{hazard_label(max_score)} ({impacted}/{len(scores)} seg)"
