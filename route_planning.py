"""Route parsing and polyline geometry helpers for intermediate FAA waypoints."""

from __future__ import annotations

import math
import re
import datetime as dt
from dataclasses import dataclass
from typing import Sequence

TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{2,8}")


@dataclass(frozen=True)
class RouteWaypoint:
    """Resolved point the planner can use as a route anchor."""

    identifier: str
    latitude: float
    longitude: float
    waypoint_type: str
    source: str
    name: str = ""
    is_fuel_stop: bool = False


@dataclass(frozen=True)
class RouteLeg:
    """One geodesic leg between consecutive route anchors."""

    start_waypoint: RouteWaypoint
    end_waypoint: RouteWaypoint
    distance_nm: float
    initial_track_deg: float


@dataclass(frozen=True)
class RoutePlan:
    """Full route geometry in the exact order the aircraft will fly it."""

    waypoints: tuple[RouteWaypoint, ...]
    legs: tuple[RouteLeg, ...]
    total_distance_nm: float
    route_label: str
    route_text: str
    intermediate_identifiers: tuple[str, ...]


@dataclass(frozen=True)
class RouteFuelSegment:
    """A contiguous route slice bounded by departure, a fuel stop, or destination."""

    start_identifier: str
    end_identifier: str
    route_plan: RoutePlan


@dataclass(frozen=True)
class MultiLegTiming:
    """Absolute departure/arrival times for one chained mission leg."""

    leg_number: int
    departure: dt.datetime
    arrival: dt.datetime


@dataclass(frozen=True)
class FuelStopLegPolicy:
    """Resolved uplift and alternate policy for a chained fuel-stop leg."""

    alternate_code: str | None
    alternate_is_explicit: bool
    alternate_fuel_excluded: bool
    uplift_gal: float | None
    next_start_fuel_gal: float
    uplift_trimmed_gal: float = 0.0


def destination_arrival_fuel_gal(
    nonstop_arrival_fuel_gal: float,
    chained_leg_arrival_fuels_gal: Sequence[float],
) -> float:
    """Use final chained-leg arrival fuel when the mission includes refueling stops."""

    if chained_leg_arrival_fuels_gal:
        return max(float(chained_leg_arrival_fuels_gal[-1]), 0.0)
    return max(float(nonstop_arrival_fuel_gal), 0.0)


def resolve_fuel_stop_leg_policy(
    *,
    destination_identifier: str,
    is_final_leg: bool,
    landing_fuel_gal: float,
    default_start_fuel_gal: float,
    uplifts: dict[str, float],
    alternates: dict[str, str],
    mission_alternate_code: str | None = None,
    usable_fuel_capacity_gal: float | None = None,
) -> FuelStopLegPolicy:
    """Resolve explicit uplift, legacy full reset, and alternate fallback consistently."""

    identifier = destination_identifier.strip().upper()
    explicit_uplift = uplifts.get(identifier)
    explicit_alternate = alternates.get(identifier)
    alternate_code = explicit_alternate or (mission_alternate_code if is_final_leg else None)
    if is_final_leg:
        next_start_fuel = max(float(landing_fuel_gal), 0.0)
    elif explicit_uplift is not None:
        next_start_fuel = max(float(landing_fuel_gal), 0.0) + max(float(explicit_uplift), 0.0)
    else:
        next_start_fuel = max(float(default_start_fuel_gal), 0.0)
    uplift_trimmed_gal = 0.0
    if usable_fuel_capacity_gal is not None and next_start_fuel > float(usable_fuel_capacity_gal):
        uplift_trimmed_gal = next_start_fuel - float(usable_fuel_capacity_gal)
        next_start_fuel = float(usable_fuel_capacity_gal)
    return FuelStopLegPolicy(
        alternate_code=alternate_code,
        alternate_is_explicit=explicit_alternate is not None,
        alternate_fuel_excluded=not is_final_leg and alternate_code is None,
        uplift_gal=max(float(explicit_uplift), 0.0) if explicit_uplift is not None else None,
        next_start_fuel_gal=next_start_fuel,
        uplift_trimmed_gal=uplift_trimmed_gal,
    )


def parse_airborne_ete(ete_text: str) -> dt.timedelta:
    """Parse the planner ETE display format and fail loudly if it is malformed."""

    match = re.fullmatch(r"\s*(?P<hours>\d+)h\s+(?P<minutes>\d+)m\s*", ete_text or "")
    if not match:
        raise ValueError(f"Invalid airborne ETE: {ete_text!r}")
    minutes = int(match.group("minutes"))
    if minutes >= 60:
        raise ValueError(f"Invalid airborne ETE minutes: {ete_text!r}")
    return dt.timedelta(hours=int(match.group("hours")), minutes=minutes)


def chain_multi_leg_timings(
    initial_departure: dt.datetime,
    airborne_etes: Sequence[str | float | dt.timedelta],
    *,
    ground_minutes: float = 0.0,
) -> tuple[MultiLegTiming, ...]:
    """Chain numeric leg durations and intervening ground time without losing timezone context."""

    if initial_departure.tzinfo is None:
        raise ValueError("initial_departure must be timezone-aware")
    ground_time = dt.timedelta(minutes=max(float(ground_minutes), 0.0))
    departure = initial_departure
    timings: list[MultiLegTiming] = []
    for leg_number, ete_value in enumerate(airborne_etes, start=1):
        if isinstance(ete_value, dt.timedelta):
            airborne_duration = ete_value
        elif isinstance(ete_value, (int, float)):
            airborne_duration = dt.timedelta(hours=max(float(ete_value), 0.0))
        else:
            airborne_duration = parse_airborne_ete(ete_value)
        arrival = departure + airborne_duration
        timings.append(MultiLegTiming(leg_number, departure, arrival))
        departure = arrival + ground_time
    return tuple(timings)


def normalize_route_tokens(raw_text: str) -> list[str]:
    """Extract ordered waypoint tokens while tolerating whitespace and punctuation."""

    return [token.upper() for token in TOKEN_PATTERN.findall(raw_text or "")]


def great_circle_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute great-circle distance in nautical miles."""

    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    cos_value = (
        math.sin(lat1_rad) * math.sin(lat2_rad)
        + math.cos(lat1_rad) * math.cos(lat2_rad) * math.cos(lon1_rad - lon2_rad)
    )
    return 3440.06 * math.acos(min(max(cos_value, -1.0), 1.0))


def initial_track_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute the initial true course from the first point to the second."""

    lat1_rad = math.radians(lat1)
    lat2_rad = math.radians(lat2)
    delta_lon_rad = math.radians(lon2 - lon1)
    y = math.sin(delta_lon_rad) * math.cos(lat2_rad)
    x = (
        math.cos(lat1_rad) * math.sin(lat2_rad)
        - math.sin(lat1_rad) * math.cos(lat2_rad) * math.cos(delta_lon_rad)
    )
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def great_circle_interpolate(
    lat1: float,
    lon1: float,
    lat2: float,
    lon2: float,
    fraction: float,
) -> tuple[float, float]:
    """Interpolate along the great-circle arc between two route anchors."""

    bounded_fraction = min(max(fraction, 0.0), 1.0)
    lat1_rad = math.radians(lat1)
    lon1_rad = math.radians(lon1)
    lat2_rad = math.radians(lat2)
    lon2_rad = math.radians(lon2)
    delta = 2.0 * math.asin(
        math.sqrt(
            math.sin((lat2_rad - lat1_rad) / 2.0) ** 2
            + math.cos(lat1_rad)
            * math.cos(lat2_rad)
            * math.sin((lon2_rad - lon1_rad) / 2.0) ** 2
        )
    )
    if abs(delta) < 1e-9:
        return lat1, lon1

    sin_delta = math.sin(delta)
    weight_start = math.sin((1.0 - bounded_fraction) * delta) / sin_delta
    weight_end = math.sin(bounded_fraction * delta) / sin_delta
    x = (
        weight_start * math.cos(lat1_rad) * math.cos(lon1_rad)
        + weight_end * math.cos(lat2_rad) * math.cos(lon2_rad)
    )
    y = (
        weight_start * math.cos(lat1_rad) * math.sin(lon1_rad)
        + weight_end * math.cos(lat2_rad) * math.sin(lon2_rad)
    )
    z = weight_start * math.sin(lat1_rad) + weight_end * math.sin(lat2_rad)
    latitude = math.degrees(math.atan2(z, math.hypot(x, y)))
    longitude = ((math.degrees(math.atan2(y, x)) + 540.0) % 360.0) - 180.0
    return latitude, longitude


def route_waypoint_from_airport(airport: object) -> RouteWaypoint:
    """Promote airport metadata into the shared route waypoint shape."""

    identifier = str(
        getattr(airport, "icao", None)
        or getattr(airport, "identifier", "")
    ).upper()
    return RouteWaypoint(
        identifier=identifier,
        latitude=float(getattr(airport, "latitude")),
        longitude=float(getattr(airport, "longitude")),
        waypoint_type=str(getattr(airport, "waypoint_type", "Airport")),
        source=str(getattr(airport, "source", "airport")),
        name=str(getattr(airport, "name", "") or identifier),
        is_fuel_stop=bool(getattr(airport, "is_fuel_stop", False)),
    )


def build_route_plan(
    departure_airport: object,
    destination_airport: object,
    intermediate_waypoints: Sequence[RouteWaypoint] | None = None,
) -> RoutePlan:
    """Build the ordered route polyline used by weather, performance, and map layers."""

    ordered_waypoints = [route_waypoint_from_airport(departure_airport)]
    ordered_waypoints.extend(intermediate_waypoints or [])
    ordered_waypoints.append(route_waypoint_from_airport(destination_airport))

    legs: list[RouteLeg] = []
    total_distance_nm = 0.0
    for start_waypoint, end_waypoint in zip(ordered_waypoints, ordered_waypoints[1:]):
        leg_distance_nm = great_circle_distance_nm(
            start_waypoint.latitude,
            start_waypoint.longitude,
            end_waypoint.latitude,
            end_waypoint.longitude,
        )
        legs.append(
            RouteLeg(
                start_waypoint=start_waypoint,
                end_waypoint=end_waypoint,
                distance_nm=leg_distance_nm,
                initial_track_deg=initial_track_deg(
                    start_waypoint.latitude,
                    start_waypoint.longitude,
                    end_waypoint.latitude,
                    end_waypoint.longitude,
                ),
            )
        )
        total_distance_nm += leg_distance_nm

    identifiers = [waypoint.identifier for waypoint in ordered_waypoints]
    return RoutePlan(
        waypoints=tuple(ordered_waypoints),
        legs=tuple(legs),
        total_distance_nm=total_distance_nm,
        route_label=" -> ".join(identifiers),
        route_text=" ".join(identifiers),
        intermediate_identifiers=tuple(waypoint.identifier for waypoint in ordered_waypoints[1:-1]),
    )


def split_route_plan_at_fuel_stops(route_plan: RoutePlan) -> tuple[RouteFuelSegment, ...]:
    """Split a full route into independently briefable legs at marked fuel-stop airports."""

    if len(route_plan.waypoints) < 2:
        return ()

    split_indexes = [0]
    split_indexes.extend(
        index
        for index, waypoint in enumerate(route_plan.waypoints[1:-1], start=1)
        if waypoint.is_fuel_stop
    )
    split_indexes.append(len(route_plan.waypoints) - 1)

    segments: list[RouteFuelSegment] = []
    for start_index, end_index in zip(split_indexes, split_indexes[1:]):
        if end_index <= start_index:
            continue
        start_waypoint = route_plan.waypoints[start_index]
        end_waypoint = route_plan.waypoints[end_index]
        intermediate_waypoints = route_plan.waypoints[start_index + 1 : end_index]
        segment_route_plan = build_route_plan(
            start_waypoint,
            end_waypoint,
            intermediate_waypoints=intermediate_waypoints,
        )
        segments.append(
            RouteFuelSegment(
                start_identifier=start_waypoint.identifier,
                end_identifier=end_waypoint.identifier,
                route_plan=segment_route_plan,
            )
        )
    return tuple(segments)


def route_point_at_distance_nm(route_plan: RoutePlan, distance_nm: float) -> tuple[float, float]:
    """Locate a point along the planned polyline by cumulative route distance."""

    if not route_plan.waypoints:
        return 0.0, 0.0
    if not route_plan.legs or route_plan.total_distance_nm <= 0.0:
        first_waypoint = route_plan.waypoints[0]
        return first_waypoint.latitude, first_waypoint.longitude

    remaining_distance_nm = min(max(distance_nm, 0.0), route_plan.total_distance_nm)
    for leg in route_plan.legs:
        if leg.distance_nm <= 0.0:
            continue
        if remaining_distance_nm <= leg.distance_nm:
            fraction = remaining_distance_nm / leg.distance_nm
            return great_circle_interpolate(
                leg.start_waypoint.latitude,
                leg.start_waypoint.longitude,
                leg.end_waypoint.latitude,
                leg.end_waypoint.longitude,
                fraction,
            )
        remaining_distance_nm -= leg.distance_nm

    last_waypoint = route_plan.waypoints[-1]
    return last_waypoint.latitude, last_waypoint.longitude


def route_track_at_distance_nm(route_plan: RoutePlan, distance_nm: float) -> float:
    """Return the great-circle course at the sampled point on the active leg."""

    if not route_plan.legs:
        return 0.0

    remaining_distance_nm = min(max(distance_nm, 0.0), route_plan.total_distance_nm)
    for leg in route_plan.legs:
        if leg.distance_nm <= 0.0:
            continue
        if remaining_distance_nm <= leg.distance_nm:
            fraction = remaining_distance_nm / leg.distance_nm
            # Sample just before the exact endpoint so the forward course remains defined.
            course_fraction = min(fraction, max(0.0, 1.0 - (1.0 / max(leg.distance_nm, 1.0))))
            sample_lat, sample_lon = great_circle_interpolate(
                leg.start_waypoint.latitude,
                leg.start_waypoint.longitude,
                leg.end_waypoint.latitude,
                leg.end_waypoint.longitude,
                course_fraction,
            )
            return initial_track_deg(
                sample_lat,
                sample_lon,
                leg.end_waypoint.latitude,
                leg.end_waypoint.longitude,
            )
        remaining_distance_nm -= leg.distance_nm

    return route_plan.legs[-1].initial_track_deg


def route_midpoint_lat_lon(route_plan: RoutePlan) -> tuple[float, float]:
    """Use the actual route midpoint rather than the direct midpoint for regional sampling."""

    return route_point_at_distance_nm(route_plan, route_plan.total_distance_nm / 2.0)


def route_sample_points(
    route_plan: RoutePlan,
    *,
    samples_per_leg: int = 24,
) -> tuple[tuple[float, float], ...]:
    """Expand the leg sequence into a smooth polyline for map rendering and state selection."""

    if not route_plan.waypoints:
        return ()
    if not route_plan.legs:
        first_waypoint = route_plan.waypoints[0]
        return ((first_waypoint.longitude, first_waypoint.latitude),)

    points: list[tuple[float, float]] = []
    for leg_index, leg in enumerate(route_plan.legs):
        samples = max(samples_per_leg, 1)
        for sample_index in range(samples + 1):
            if leg_index > 0 and sample_index == 0:
                continue
            fraction = sample_index / samples
            latitude, longitude = great_circle_interpolate(
                leg.start_waypoint.latitude,
                leg.start_waypoint.longitude,
                leg.end_waypoint.latitude,
                leg.end_waypoint.longitude,
                fraction,
            )
            points.append((longitude, latitude))
    return tuple(points)


def route_progress_warning(route_plan: RoutePlan) -> str | None:
    """Flag route orders that materially backtrack instead of progressing toward destination."""

    if len(route_plan.waypoints) < 4:
        return None

    departure = route_plan.waypoints[0]
    destination = route_plan.waypoints[-1]
    mean_latitude_rad = math.radians((departure.latitude + destination.latitude) / 2.0)
    east_scale_nm = 60.0 * max(math.cos(mean_latitude_rad), 0.25)
    north_scale_nm = 60.0

    # Project each waypoint onto the direct-course vector so we can flag sequences that move
    # materially backward along the overall trip even if they are only slightly displaced laterally.
    def projected_xy(waypoint: RouteWaypoint) -> tuple[float, float]:
        return (
            (waypoint.longitude - departure.longitude) * east_scale_nm,
            (waypoint.latitude - departure.latitude) * north_scale_nm,
        )

    destination_x, destination_y = projected_xy(destination)
    direct_distance_nm = math.hypot(destination_x, destination_y)
    if direct_distance_nm <= 1.0:
        return None

    unit_x = destination_x / direct_distance_nm
    unit_y = destination_y / direct_distance_nm
    progress_tolerance_nm = max(20.0, direct_distance_nm * 0.08)
    previous_progress_nm = 0.0
    previous_waypoint = route_plan.waypoints[0]

    for current_waypoint in route_plan.waypoints[1:-1]:
        current_x, current_y = projected_xy(current_waypoint)
        progress_nm = (current_x * unit_x) + (current_y * unit_y)
        if progress_nm + progress_tolerance_nm < previous_progress_nm:
            return (
                f"Waypoint order appears to backtrack near "
                f"{previous_waypoint.identifier} -> {current_waypoint.identifier}. Verify the sequence."
            )
        previous_progress_nm = progress_nm
        previous_waypoint = current_waypoint

    if route_plan.total_distance_nm > direct_distance_nm * 1.6:
        return "Waypoint order creates a large detour relative to the direct route. Verify the sequence."
    return None


@dataclass(frozen=True)
class MissionHeadline:
    """Fuel figures the mission summary surfaces must show for the planned mission."""

    basis: str
    reserve_margin_gal: int
    margin_leg_label: str | None
    fob_at_landing_gal: int


def resolve_mission_headline(
    *,
    nonstop_reserve_margin_gal: int,
    nonstop_fob_at_landing_gal: int,
    leg_reserve_margins_gal: Sequence[tuple[str, int]] = (),
    leg_arrival_fuels_gal: Sequence[float] = (),
) -> MissionHeadline:
    """Pick headline fuel numbers: worst planned leg when refueling, else nonstop.

    A refueled mission's nonstop figures describe a flight that will not be
    flown; the headline must track the worst planned leg and the final leg's
    arrival fuel instead.
    """

    if not leg_reserve_margins_gal:
        return MissionHeadline(
            basis="nonstop",
            reserve_margin_gal=int(nonstop_reserve_margin_gal),
            margin_leg_label=None,
            fob_at_landing_gal=int(nonstop_fob_at_landing_gal),
        )
    worst_label, worst_margin_gal = min(leg_reserve_margins_gal, key=lambda item: item[1])
    return MissionHeadline(
        basis="multi-leg",
        reserve_margin_gal=int(worst_margin_gal),
        margin_leg_label=worst_label,
        fob_at_landing_gal=(
            int(round(leg_arrival_fuels_gal[-1]))
            if leg_arrival_fuels_gal
            else int(nonstop_fob_at_landing_gal)
        ),
    )
