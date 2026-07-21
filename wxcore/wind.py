"""Route wind and temperature models interpolated from FD windtemp stations."""

from __future__ import annotations

import math


from performance_profiles import (
    _interpolate_scalar,
)
from route_planning import (
    RoutePlan,
)

from .models import (
    FLIGHT_LEVELS,
    SEGMENTS,
    MAX_WIND_STATION_DISTANCE_NM,
    WIND_COVERAGE_PROBE_ALTITUDE_FT,
    IDW_DISTANCE_SOFTENING_NM,
    IDW_MAX_STATIONS,
    AirportData,
    WindTempPoint,
    RouteWindModel,
    normalize_icao,
)


from .geo import (
    great_circle_distance_nm,
    _route_point_at_distance_nm,
    _route_track_at_distance_nm,
)
from .feeds import (
    _lookup_windtemp_station_coords,
)


def _wind_from_to_uv(direction_from_deg: int, speed_kt: int) -> tuple[float, float]:
    """Convert meteorological wind-from direction into east/north velocity components."""

    toward_deg = (direction_from_deg + 180.0) % 360.0
    toward_rad = math.radians(toward_deg)
    u_east = float(speed_kt) * math.sin(toward_rad)
    v_north = float(speed_kt) * math.cos(toward_rad)
    return u_east, v_north


def _interpolate_uv_by_altitude(
    altitude_to_uv: dict[int, tuple[float, float]],
    altitude_ft: float,
) -> tuple[float, float] | None:
    """Interpolate wind vector components at a requested altitude."""

    if not altitude_to_uv:
        return None

    levels = sorted(altitude_to_uv.keys())
    if altitude_ft <= levels[0]:
        return altitude_to_uv[levels[0]]
    if altitude_ft >= levels[-1]:
        return altitude_to_uv[levels[-1]]

    for idx in range(1, len(levels)):
        lower = levels[idx - 1]
        upper = levels[idx]
        if lower <= altitude_ft <= upper:
            lower_uv = altitude_to_uv[lower]
            upper_uv = altitude_to_uv[upper]
            if upper == lower:
                return lower_uv
            ratio = (altitude_ft - lower) / (upper - lower)
            u = lower_uv[0] + (upper_uv[0] - lower_uv[0]) * ratio
            v = lower_uv[1] + (upper_uv[1] - lower_uv[1]) * ratio
            return u, v
    return None


def _project_wind_to_track(
    *,
    mean_u: float,
    mean_v: float,
    track_deg: float,
) -> tuple[float, float]:
    """Project east/north wind components into tailwind and crosswind on a track."""

    track_rad = math.radians(track_deg)
    track_u = math.sin(track_rad)
    track_v = math.cos(track_rad)
    right_u = math.cos(track_rad)
    right_v = -math.sin(track_rad)
    tailwind_kt = (mean_u * track_u) + (mean_v * track_v)
    crosswind_kt = (mean_u * right_u) + (mean_v * right_v)
    return tailwind_kt, crosswind_kt


def _estimate_wind_components(
    *,
    station_profiles: list[tuple[float, float, dict[int, tuple[float, float]]]],
    sample_lat: float,
    sample_lon: float,
    track_deg: float,
    altitude_ft: float,
) -> tuple[float, float] | None:
    """Estimate route-relative wind from nearby winds-aloft stations."""

    weighted_u = 0.0
    weighted_v = 0.0
    weight_sum = 0.0

    station_samples: list[tuple[float, tuple[float, float]]] = []
    for station_lat, station_lon, altitude_to_uv in station_profiles:
        uv = _interpolate_uv_by_altitude(altitude_to_uv, altitude_ft)
        if uv is None:
            continue
        distance_nm = great_circle_distance_nm(sample_lat, sample_lon, station_lat, station_lon)
        if distance_nm <= MAX_WIND_STATION_DISTANCE_NM:
            station_samples.append((distance_nm, uv))

    if not station_samples:
        return None

    station_samples.sort(key=lambda item: item[0])
    for distance_nm, uv in station_samples[:IDW_MAX_STATIONS]:
        weight = 1.0 / ((distance_nm + IDW_DISTANCE_SOFTENING_NM) ** 2)
        weighted_u += uv[0] * weight
        weighted_v += uv[1] * weight
        weight_sum += weight

    if weight_sum <= 0.0:
        return None

    mean_u = weighted_u / weight_sum
    mean_v = weighted_v / weight_sum
    return _project_wind_to_track(
        mean_u=mean_u,
        mean_v=mean_v,
        track_deg=track_deg,
    )


def build_route_wind_model(
    departure: AirportData,
    destination: AirportData,
    windtemps: list[WindTempPoint],
    route_plan: RoutePlan | None = None,
) -> RouteWindModel | None:
    """Project NOAA FD stations into route-relative wind and temperature samples."""

    station_altitude_uv: dict[str, tuple[float, float, dict[int, tuple[float, float]]]] = {}
    station_altitude_temp: dict[str, tuple[float, float, dict[int, float]]] = {}

    for point in windtemps:
        coords = _lookup_windtemp_station_coords(point.station)
        if coords is None:
            continue

        station_key = normalize_icao(point.station)
        if point.direction_deg is not None and point.speed_kt is not None:
            if station_key not in station_altitude_uv:
                station_altitude_uv[station_key] = (coords[0], coords[1], {})
            altitude_map = station_altitude_uv[station_key][2]
            altitude_map[point.altitude_ft] = _wind_from_to_uv(point.direction_deg, point.speed_kt)

        if point.temperature_c is not None:
            if station_key not in station_altitude_temp:
                station_altitude_temp[station_key] = (coords[0], coords[1], {})
            temperature_map = station_altitude_temp[station_key][2]
            temperature_map[point.altitude_ft] = float(point.temperature_c)

    station_profiles = [value for value in station_altitude_uv.values() if value[2]]
    station_temperature_profiles = [value for value in station_altitude_temp.values() if value[2]]
    if not station_profiles and not station_temperature_profiles:
        return None

    mission_distance_nm = (
        route_plan.total_distance_nm
        if route_plan is not None
        else great_circle_distance_nm(
            departure.latitude,
            departure.longitude,
            destination.latitude,
            destination.longitude,
        )
    )
    track_deg = _route_track_at_distance_nm(
        departure_latitude=departure.latitude,
        departure_longitude=departure.longitude,
        destination_latitude=destination.latitude,
        destination_longitude=destination.longitude,
        distance_from_departure_nm=mission_distance_nm / 2.0 if mission_distance_nm > 0.0 else 0.0,
        route_plan=route_plan,
    )

    segment_tailwind_by_fl: dict[int, list[float]] = {}
    climb_tailwind_by_fl: dict[int, float] = {}
    descent_tailwind_by_fl: dict[int, float] = {}
    segment_crosswind_by_fl: dict[int, list[float]] = {}
    climb_crosswind_by_fl: dict[int, float] = {}
    descent_crosswind_by_fl: dict[int, float] = {}
    uncovered_segment_count = 0
    for segment_idx in range(SEGMENTS):
        sample_distance_nm = mission_distance_nm * ((segment_idx + 0.5) / SEGMENTS)
        sample_lat, sample_lon = _route_point_at_distance_nm(
            departure_latitude=departure.latitude,
            departure_longitude=departure.longitude,
            destination_latitude=destination.latitude,
            destination_longitude=destination.longitude,
            mission_distance_nm=mission_distance_nm,
            distance_from_departure_nm=sample_distance_nm,
            route_plan=route_plan,
        )
        sample_track = _route_track_at_distance_nm(
            departure_latitude=departure.latitude,
            departure_longitude=departure.longitude,
            destination_latitude=destination.latitude,
            destination_longitude=destination.longitude,
            distance_from_departure_nm=sample_distance_nm,
            route_plan=route_plan,
        )
        if _estimate_wind_components(
            station_profiles=station_profiles,
            sample_lat=sample_lat,
            sample_lon=sample_lon,
            track_deg=sample_track,
            # This probes horizontal station coverage; altitude interpolation
            # clamps to each station's published FD levels.
            altitude_ft=WIND_COVERAGE_PROBE_ALTITUDE_FT,
        ) is None:
            uncovered_segment_count += 1

    for fl in FLIGHT_LEVELS:
        cruise_altitude_ft = float(fl * 100)
        segment_components: list[float] = []
        segment_crosswinds: list[float] = []
        # Cruise samples are taken at segment midpoints so each level gets route-aware winds.
        for segment_idx in range(SEGMENTS):
            sample_distance_nm = mission_distance_nm * ((segment_idx + 0.5) / SEGMENTS)
            lat, lon = _route_point_at_distance_nm(
                departure_latitude=departure.latitude,
                departure_longitude=departure.longitude,
                destination_latitude=destination.latitude,
                destination_longitude=destination.longitude,
                mission_distance_nm=mission_distance_nm,
                distance_from_departure_nm=sample_distance_nm,
                route_plan=route_plan,
            )
            segment_track_deg = _route_track_at_distance_nm(
                departure_latitude=departure.latitude,
                departure_longitude=departure.longitude,
                destination_latitude=destination.latitude,
                destination_longitude=destination.longitude,
                distance_from_departure_nm=sample_distance_nm,
                route_plan=route_plan,
            )
            components = _estimate_wind_components(
                station_profiles=station_profiles,
                sample_lat=lat,
                sample_lon=lon,
                track_deg=segment_track_deg,
                altitude_ft=cruise_altitude_ft,
            )
            if components is None:
                segment_components.append(0.0)
                segment_crosswinds.append(0.0)
            else:
                segment_components.append(components[0])
                segment_crosswinds.append(components[1])
        segment_tailwind_by_fl[fl] = segment_components
        segment_crosswind_by_fl[fl] = segment_crosswinds

        # Climb/descent use endpoint-biased samples as the baseline for the vertical integrator.
        climb_altitude_ft = (departure.elevation_ft + cruise_altitude_ft) / 2.0
        descent_altitude_ft = (destination.elevation_ft + cruise_altitude_ft) / 2.0
        climb_components = _estimate_wind_components(
            station_profiles=station_profiles,
            sample_lat=departure.latitude,
            sample_lon=departure.longitude,
            track_deg=_route_track_at_distance_nm(
                departure_latitude=departure.latitude,
                departure_longitude=departure.longitude,
                destination_latitude=destination.latitude,
                destination_longitude=destination.longitude,
                distance_from_departure_nm=0.0,
                route_plan=route_plan,
            ),
            altitude_ft=climb_altitude_ft,
        )
        descent_components = _estimate_wind_components(
            station_profiles=station_profiles,
            sample_lat=destination.latitude,
            sample_lon=destination.longitude,
            track_deg=_route_track_at_distance_nm(
                departure_latitude=departure.latitude,
                departure_longitude=departure.longitude,
                destination_latitude=destination.latitude,
                destination_longitude=destination.longitude,
                distance_from_departure_nm=mission_distance_nm,
                route_plan=route_plan,
            ),
            altitude_ft=descent_altitude_ft,
        )
        climb_tailwind_by_fl[fl] = (
            climb_components[0] if climb_components is not None else segment_components[0]
        )
        climb_crosswind_by_fl[fl] = (
            climb_components[1] if climb_components is not None else segment_crosswinds[0]
        )
        descent_tailwind_by_fl[fl] = (
            descent_components[0] if descent_components is not None else segment_components[-1]
        )
        descent_crosswind_by_fl[fl] = (
            descent_components[1] if descent_components is not None else segment_crosswinds[-1]
        )

    return RouteWindModel(
        segment_tailwind_by_fl=segment_tailwind_by_fl,
        climb_tailwind_by_fl=climb_tailwind_by_fl,
        descent_tailwind_by_fl=descent_tailwind_by_fl,
        segment_crosswind_by_fl=segment_crosswind_by_fl,
        climb_crosswind_by_fl=climb_crosswind_by_fl,
        descent_crosswind_by_fl=descent_crosswind_by_fl,
        station_profiles=tuple(station_profiles),
        station_temperature_profiles=tuple(station_temperature_profiles),
        track_deg=track_deg,
        station_count=max(len(station_profiles), len(station_temperature_profiles)),
        usable_sample_count=(
            sum(len(profile[2]) for profile in station_profiles)
            + sum(len(profile[2]) for profile in station_temperature_profiles)
        ),
        uncovered_segment_count=uncovered_segment_count,
        coverage_fraction=(SEGMENTS - uncovered_segment_count) / SEGMENTS,
    )


def _sample_wind_components_from_model(
    *,
    wind_model: RouteWindModel | None,
    sample_latitude: float,
    sample_longitude: float,
    altitude_ft: float,
    track_deg: float,
) -> tuple[float, float] | None:
    """Sample tailwind and crosswind from a built route wind model."""

    if wind_model is None or not wind_model.station_profiles:
        return None

    return _estimate_wind_components(
        station_profiles=list(wind_model.station_profiles),
        sample_lat=sample_latitude,
        sample_lon=sample_longitude,
        track_deg=track_deg,
        altitude_ft=altitude_ft,
    )


def _interpolate_temperature_by_altitude(
    altitude_to_temperature_c: dict[int, float],
    altitude_ft: float,
) -> float | None:
    """Interpolate winds-aloft temperature at a requested altitude."""

    if not altitude_to_temperature_c:
        return None

    ordered_altitudes_ft = sorted(altitude_to_temperature_c)
    if altitude_ft <= ordered_altitudes_ft[0]:
        return altitude_to_temperature_c[ordered_altitudes_ft[0]]
    if altitude_ft >= ordered_altitudes_ft[-1]:
        return altitude_to_temperature_c[ordered_altitudes_ft[-1]]

    for low_altitude_ft, high_altitude_ft in zip(ordered_altitudes_ft, ordered_altitudes_ft[1:]):
        if low_altitude_ft <= altitude_ft <= high_altitude_ft:
            low_temperature_c = altitude_to_temperature_c[low_altitude_ft]
            high_temperature_c = altitude_to_temperature_c[high_altitude_ft]
            return _interpolate_scalar(
                low_temperature_c,
                high_temperature_c,
                x_low=float(low_altitude_ft),
                x_high=float(high_altitude_ft),
                x_target=float(altitude_ft),
            )

    return altitude_to_temperature_c[ordered_altitudes_ft[-1]]


def _estimate_temperature_c(
    *,
    station_temperature_profiles: list[tuple[float, float, dict[int, float]]],
    sample_lat: float,
    sample_lon: float,
    altitude_ft: float,
) -> float | None:
    """Estimate temperature from nearby station temperature profiles."""

    weighted_temperature_c = 0.0
    weight_sum = 0.0
    station_samples: list[tuple[float, float]] = []

    for station_lat, station_lon, altitude_to_temperature_c in station_temperature_profiles:
        temperature_c = _interpolate_temperature_by_altitude(altitude_to_temperature_c, altitude_ft)
        if temperature_c is None:
            continue
        distance_nm = great_circle_distance_nm(sample_lat, sample_lon, station_lat, station_lon)
        if distance_nm <= MAX_WIND_STATION_DISTANCE_NM:
            station_samples.append((distance_nm, temperature_c))

    if not station_samples:
        return None

    station_samples.sort(key=lambda item: item[0])
    for distance_nm, temperature_c in station_samples[:IDW_MAX_STATIONS]:
        weight = 1.0 / ((distance_nm + IDW_DISTANCE_SOFTENING_NM) ** 2)
        weighted_temperature_c += temperature_c * weight
        weight_sum += weight

    if weight_sum <= 0.0:
        return None
    return weighted_temperature_c / weight_sum


def _sample_temperature_from_model(
    *,
    wind_model: RouteWindModel | None,
    sample_latitude: float,
    sample_longitude: float,
    altitude_ft: float,
) -> float | None:
    """Sample outside-air temperature from a built route wind model."""

    if wind_model is None or not wind_model.station_temperature_profiles:
        return None
    return _estimate_temperature_c(
        station_temperature_profiles=list(wind_model.station_temperature_profiles),
        sample_lat=sample_latitude,
        sample_lon=sample_longitude,
        altitude_ft=altitude_ft,
    )


def _standard_atmosphere_temperature_c(altitude_ft: float) -> float:
    """Return ISA temperature in Celsius for a pressure altitude."""

    altitude_m = max(altitude_ft, 0.0) * 0.3048
    sea_level_temp_k = 288.15
    lapse_rate_k_per_m = 0.0065
    temperature_k = sea_level_temp_k - (lapse_rate_k_per_m * altitude_m)
    return temperature_k - 273.15


def _average_route_temperature_c(
    *,
    wind_model: RouteWindModel | None,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    mission_distance_nm: float,
    altitude_ft: float,
    sample_distances_nm: list[float],
    route_plan: RoutePlan | None = None,
) -> float | None:
    """Average forecast temperature across route samples at one altitude."""

    sampled_temperatures_c: list[float] = []

    for sample_distance_nm in sample_distances_nm:
        sample_latitude, sample_longitude = _route_point_at_distance_nm(
            departure_latitude=departure_latitude,
            departure_longitude=departure_longitude,
            destination_latitude=destination_latitude,
            destination_longitude=destination_longitude,
            mission_distance_nm=mission_distance_nm,
            distance_from_departure_nm=sample_distance_nm,
            route_plan=route_plan,
        )
        sampled_temperature_c = _sample_temperature_from_model(
            wind_model=wind_model,
            sample_latitude=sample_latitude,
            sample_longitude=sample_longitude,
            altitude_ft=altitude_ft,
        )
        if sampled_temperature_c is not None:
            sampled_temperatures_c.append(sampled_temperature_c)

    if not sampled_temperatures_c:
        return None
    return sum(sampled_temperatures_c) / len(sampled_temperatures_c)


def _temperature_offset_from_model(
    *,
    wind_model: RouteWindModel | None,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    mission_distance_nm: float,
    altitude_ft: float,
    sample_distances_nm: list[float],
    route_plan: RoutePlan | None = None,
) -> float | None:
    """Return forecast temperature offset from ISA for performance-table sampling."""

    average_temperature_c = _average_route_temperature_c(
        wind_model=wind_model,
        departure_latitude=departure_latitude,
        departure_longitude=departure_longitude,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        mission_distance_nm=mission_distance_nm,
        altitude_ft=altitude_ft,
        sample_distances_nm=sample_distances_nm,
        route_plan=route_plan,
    )
    if average_temperature_c is None:
        return None
    return average_temperature_c - _standard_atmosphere_temperature_c(altitude_ft)
