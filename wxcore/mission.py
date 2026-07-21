"""Mission assembly: risk, legality, rings, profiles, briefs, legs, and the document."""

from __future__ import annotations

import datetime as dt
import inspect
import math
from dataclasses import dataclass
from typing import Callable

import pytz

from performance_profiles import (
    AircraftPerformanceProfile,
    MIN_PLANNING_RATE_FPM,
    VerticalPerformanceRow,
    sample_climb_rows,
    sample_composite_climb_rows,
    sample_cruise_performance,
    sample_descent_rows,
)
from route_planning import (
    MissionHeadline,
    RouteFuelSegment,
    RoutePlan,
    RouteWaypoint,
    build_route_plan as _route_plan_between_airports,
    resolve_fuel_stop_leg_policy,
    resolve_mission_headline,
    split_route_plan_at_fuel_stops,
)

from .models import (
    FLIGHT_LEVELS,
    SEGMENTS,
    CRUISE_BIN_DISTANCE_NM,
    ALTITUDE_BAND_OVERLAP_TOLERANCE_FT,
    ALTERNATE_DIVERSION_FLIGHT_LEVEL,
    FUEL_BURN_GPH,
    FIXED_FUEL_GAL,
    FEET_PER_NAUTICAL_MILE,
    MISSION_RISK_LABEL_BY_SCORE,
    AirportData,
    build_fuel_ledger,
    MissionPoint,
    MissionBrief,
    MissionRiskSummary,
    MissionRiskThresholds,
    TerminalForecastPeriod,
    LegalAlternateAssessment,
    ForecastQualityCheck,
    AlternateRangeRing,
    NoaaWeather,
    HazardArea,
    SegmentHazard,
    RouteWindModel,
    FlightLevelProfile,
    RouteVerticalProfilePoint,
    RouteVerticalProfileHazardSpan,
    RouteVerticalProfileWaypointMarker,
    RouteVerticalProfile,
    normalize_icao,
    is_westbound_route,
)

from .geo import (
    great_circle_distance_nm,
    _initial_track_deg,
    _route_point_at_distance_nm,
    _route_track_at_distance_nm,
    _route_interval_intersects_polygons,
    _latest_gairmet_valid_to_by_source,
    hazard_applies_at,
    hazard_label,
)

from .feeds import (
    _format_time_12h,
    get_airport_data,
)

from .wind import (
    build_route_wind_model,
    _sample_wind_components_from_model,
    _sample_temperature_from_model,
    _temperature_offset_from_model,
)


def build_mission_risk_summary(
    *,
    weather: NoaaWeather,
    segment_hazards: list[SegmentHazard],
    mission_point: MissionPoint | None,
    thresholds: MissionRiskThresholds | None = None,
    reserve_margin_override_gal: int | None = None,
    reserve_margin_context: str | None = None,
) -> MissionRiskSummary:
    """Score known mission risk while reporting feed health as confidence evidence."""

    active_thresholds = thresholds or MissionRiskThresholds()
    reasons: list[str] = []
    confidence_reasons: list[str] = []
    scores: list[int] = []

    failed_feeds = [
        status.name
        for status in weather.feed_statuses.values()
        if status.status == "failed" and status.name != "GFA/FIP/GTG"
    ]
    empty_feeds = [
        status.name
        for status in weather.feed_statuses.values()
        if status.status == "empty"
    ]
    if failed_feeds:
        confidence_reasons.append(f"Failed feeds: {', '.join(failed_feeds[:3])}")
    elif empty_feeds:
        confidence_reasons.append(f"Empty valid feeds: {', '.join(empty_feeds[:3])}")

    terminal_risks = [
        (icao, risk)
        for icao, airport in weather.airports.items()
        for risk in (airport.metar_risk, airport.taf_risk)
        if risk is not None
    ]
    max_terminal_score = max((risk.score for _, risk in terminal_risks), default=0)
    if max_terminal_score:
        scores.append(max_terminal_score)
        strongest_icao, strongest = next(
            (icao, risk) for icao, risk in terminal_risks if risk.score == max_terminal_score
        )
        reason = strongest.reasons[0] if strongest.reasons else strongest.label
        reasons.append(f"Terminal {strongest.source} {strongest_icao}: {reason}")

    max_route_score = max((row.overall_score for row in segment_hazards), default=0)
    if max_route_score:
        impacted = sum(1 for row in segment_hazards if row.overall_score > 0)
        impacted_fraction = impacted / len(segment_hazards) if segment_hazards else 0.0
        route_score = max_route_score
        if impacted_fraction >= active_thresholds.route_high_fraction:
            route_score = max(route_score, 3)
        elif impacted_fraction >= active_thresholds.route_caution_fraction:
            route_score = max(route_score, 2)
        scores.append(route_score)
        route_reason = (
            f"Route hazards: {hazard_label(max_route_score)} across {impacted}/{len(segment_hazards)} bins"
        )
        if route_score > max_route_score:
            route_reason += " (widespread exposure escalated the score)"
        reasons.append(route_reason)

    if mission_point is not None:
        # A refueled mission is scored on its worst planned leg, not the nonstop fiction.
        margin_gal = (
            reserve_margin_override_gal
            if reserve_margin_override_gal is not None
            else mission_point.reserve_margin_gal
        )
        margin_suffix = f" ({reserve_margin_context})" if reserve_margin_context else ""
        if margin_gal < active_thresholds.fuel_high_margin_gal:
            scores.append(3)
            if margin_gal < 0:
                reasons.append(f"Fuel reserve shortfall {abs(margin_gal)} gal{margin_suffix}")
            else:
                reasons.append(
                    "Fuel reserve margin "
                    f"{margin_gal} gal below high-risk threshold "
                    f"{active_thresholds.fuel_high_margin_gal} gal{margin_suffix}"
                )
        elif margin_gal < active_thresholds.fuel_caution_margin_gal:
            scores.append(2)
            reasons.append(f"Fuel reserve margin {margin_gal} gal{margin_suffix}")
        else:
            reasons.append(f"Fuel reserve margin {margin_gal} gal{margin_suffix}")

    score = max(scores, default=0)
    confidence = weather.data_confidence
    if weather.feed_statuses.get("gfa_fip_gtg") is not None:
        confidence_reasons.append("GFA/FIP/GTG gridded layers evaluated: no public AWC Data API ingestion path in current docs")
    return MissionRiskSummary(
        score=score,
        label=MISSION_RISK_LABEL_BY_SCORE.get(score, "Unknown"),
        confidence=confidence,
        reasons=tuple(reasons[:6]),
        confidence_reasons=tuple(confidence_reasons[:6]),
    )


def _taf_period_overlaps_window(
    period: TerminalForecastPeriod,
    *,
    window_start_utc: dt.datetime,
    window_end_utc: dt.datetime,
) -> bool:
    """Return whether a TAF period applies to any part of the target time window."""

    if period.valid_from_utc is None or period.valid_to_utc is None:
        return False
    return period.valid_from_utc <= window_end_utc and period.valid_to_utc >= window_start_utc


def _taf_period_at_time(
    periods: tuple[TerminalForecastPeriod, ...],
    timestamp_utc: dt.datetime,
) -> TerminalForecastPeriod | None:
    """Find the most specific TAF period valid for a timestamp."""

    matching = [
        period
        for period in periods
        if period.valid_from_utc is not None
        and period.valid_to_utc is not None
        and period.valid_from_utc <= timestamp_utc <= period.valid_to_utc
    ]
    if not matching:
        return None
    # Change groups are returned after the base period in AWC payloads; prefer the shortest
    # matching window so TEMPO/BECMG-like details are not hidden by a broad base forecast.
    return min(
        matching,
        key=lambda period: (period.valid_to_utc - period.valid_from_utc).total_seconds()
        if period.valid_from_utc and period.valid_to_utc
        else float("inf"),
    )


def evaluate_legal_alternate_requirement(
    *,
    weather: NoaaWeather,
    destination_icao: str,
    eta_utc: dt.datetime | None,
    has_destination_approach: bool,
) -> LegalAlternateAssessment:
    """Evaluate the fixed-wing Part 91 destination alternate filing exception."""

    code = normalize_icao(destination_icao)
    if eta_utc is not None and eta_utc.tzinfo is None:
        eta_utc = eta_utc.replace(tzinfo=dt.timezone.utc)
    eta_utc = eta_utc.astimezone(dt.timezone.utc) if eta_utc is not None else None
    window_start = eta_utc - dt.timedelta(hours=1) if eta_utc is not None else None
    window_end = eta_utc + dt.timedelta(hours=1) if eta_utc is not None else None
    reasons: list[str] = []

    if not has_destination_approach:
        return LegalAlternateAssessment(
            is_required=True,
            status="Required",
            label="Alternate required",
            reasons=("Destination approach availability is not confirmed.",),
            window_start_utc=window_start,
            window_end_utc=window_end,
            has_destination_approach=False,
        )

    airport_weather = weather.airports.get(code)
    if airport_weather is None or not airport_weather.taf_periods or eta_utc is None or window_start is None or window_end is None:
        return LegalAlternateAssessment(
            is_required=True,
            status="Unknown",
            label="Alternate required until forecast is confirmed",
            reasons=("Destination TAF coverage for ETA +/- 1 hour is unavailable.",),
            window_start_utc=window_start,
            window_end_utc=window_end,
            has_destination_approach=has_destination_approach,
        )

    relevant_periods = [
        period
        for period in airport_weather.taf_periods
        if _taf_period_overlaps_window(period, window_start_utc=window_start, window_end_utc=window_end)
    ]
    if not relevant_periods:
        return LegalAlternateAssessment(
            is_required=True,
            status="Unknown",
            label="Alternate required until TAF window is covered",
            reasons=("No decoded TAF period overlaps ETA +/- 1 hour.",),
            window_start_utc=window_start,
            window_end_utc=window_end,
            has_destination_approach=has_destination_approach,
        )

    ceilings = [period.ceiling_ft for period in relevant_periods if period.ceiling_ft is not None]
    visibilities = [period.visibility_sm for period in relevant_periods if period.visibility_sm is not None]
    worst_ceiling = min(ceilings) if ceilings else None
    worst_visibility = min(visibilities) if visibilities else None
    unknown_weather = any(
        (period.ceiling_ft is None and not period.ceiling_is_unlimited)
        or period.visibility_sm is None
        for period in relevant_periods
    )
    if unknown_weather:
        reasons.append("One or more overlapping TAF periods lacks decoded ceiling or visibility.")
    if worst_ceiling is not None and worst_ceiling < 2000:
        reasons.append(f"Destination forecast ceiling {worst_ceiling:,} ft is below 2,000 ft.")
    if worst_visibility is not None and worst_visibility < 3.0:
        reasons.append(f"Destination forecast visibility {worst_visibility:g} SM is below 3 SM.")

    if reasons:
        return LegalAlternateAssessment(
            is_required=True,
            status="Required",
            label="Alternate required",
            reasons=tuple(reasons),
            window_start_utc=window_start,
            window_end_utc=window_end,
            worst_ceiling_ft=worst_ceiling,
            worst_visibility_sm=worst_visibility,
            has_destination_approach=has_destination_approach,
        )

    return LegalAlternateAssessment(
        is_required=False,
        status="Not required",
        label="Alternate not required by 1-2-3",
        reasons=("Destination has confirmed approach availability and TAF stays at or above 2,000 ft and 3 SM for ETA +/- 1 hour.",),
        window_start_utc=window_start,
        window_end_utc=window_end,
        worst_ceiling_ft=worst_ceiling,
        worst_visibility_sm=worst_visibility,
        has_destination_approach=has_destination_approach,
    )


def evaluate_terminal_forecast_quality(
    *,
    weather: NoaaWeather,
    phase_airports: dict[str, str],
) -> tuple[ForecastQualityCheck, ...]:
    """Compare recent METAR observations against the applicable TAF without penalizing missing reports."""

    checks: list[ForecastQualityCheck] = []
    for phase, icao in phase_airports.items():
        airport_weather = weather.airports.get(normalize_icao(icao))
        if airport_weather is None or airport_weather.metar_observed_at_utc is None:
            continue
        forecast_period = _taf_period_at_time(
            airport_weather.taf_periods,
            airport_weather.metar_observed_at_utc,
        )
        if forecast_period is None:
            continue

        reasons: list[str] = []
        score = 0
        if (
            airport_weather.metar_ceiling_ft is not None
            and forecast_period.ceiling_ft is not None
            and airport_weather.metar_ceiling_ft + 500 < forecast_period.ceiling_ft
        ):
            score = max(score, 2 if airport_weather.metar_ceiling_ft < 1000 else 1)
            reasons.append(
                f"Observed ceiling {airport_weather.metar_ceiling_ft:,} ft below forecast {forecast_period.ceiling_ft:,} ft"
            )
        if (
            airport_weather.metar_visibility_sm is not None
            and forecast_period.visibility_sm is not None
            and airport_weather.metar_visibility_sm + 1.0 < forecast_period.visibility_sm
        ):
            score = max(score, 2 if airport_weather.metar_visibility_sm < 3.0 else 1)
            reasons.append(
                f"Observed visibility {airport_weather.metar_visibility_sm:g} SM below forecast {forecast_period.visibility_sm:g} SM"
            )

        observed_peak_wind = max(airport_weather.metar_wind_speed_kt or 0, airport_weather.metar_wind_gust_kt or 0)
        forecast_peak_wind = max(forecast_period.wind_speed_kt or 0, forecast_period.wind_gust_kt or 0)
        if observed_peak_wind >= forecast_peak_wind + 10 and observed_peak_wind >= 18:
            score = max(score, 2 if observed_peak_wind >= 30 else 1)
            reasons.append(f"Observed surface wind/gust {observed_peak_wind} kt exceeds forecast {forecast_peak_wind} kt")

        observed_weather = airport_weather.metar_weather or ""
        forecast_weather = forecast_period.weather or ""
        if observed_weather and observed_weather not in forecast_weather:
            wx_score = 3 if any(token in observed_weather for token in ("FZRA", "+TS", "SQ", "FC")) else 2
            score = max(score, wx_score)
            reasons.append(f"Observed weather {observed_weather} not present in applicable TAF period")

        if reasons:
            checks.append(
                ForecastQualityCheck(
                    phase=phase,
                    icao=airport_weather.icao,
                    score=score,
                    label=hazard_label(score),
                    reasons=tuple(reasons[:4]),
                )
            )
    return tuple(checks)


def _destination_point_from_bearing_nm(
    *,
    latitude: float,
    longitude: float,
    bearing_deg: float,
    distance_nm: float,
) -> tuple[float, float]:
    """Project a latitude/longitude point by bearing and distance."""

    earth_radius_nm = 3440.06
    angular_distance = max(distance_nm, 0.0) / earth_radius_nm
    bearing_rad = math.radians(bearing_deg)
    lat1 = math.radians(latitude)
    lon1 = math.radians(longitude)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance) * math.cos(bearing_rad)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing_rad) * math.sin(angular_distance) * math.cos(lat1),
        math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), ((math.degrees(lon2) + 540.0) % 360.0) - 180.0


def _integrate_vertical_bands(
    *,
    lower_altitude_ft: float,
    upper_altitude_ft: float,
    rows: tuple[VerticalPerformanceRow, ...] | list[VerticalPerformanceRow],
    fallback_ias_kts: int,
    fallback_rate_fpm: int,
    fallback_fuel_gph: float,
    default_tailwind_kts: float,
    default_crosswind_kts: float,
    wind_model: RouteWindModel | None,
    sample_position: Callable[[float, float], tuple[float, float, float]],
    integrate_high_to_low: bool = False,
    prefer_nominal_ias_tas: bool = False,
) -> tuple[float, float, float, float]:
    """Integrate vertical bands while delegating only the geographic sample path."""

    low_ft = min(lower_altitude_ft, upper_altitude_ft)
    high_ft = max(lower_altitude_ft, upper_altitude_ft)
    if high_ft <= low_ft:
        return 0.0, 0.0, 0.0, 0.0

    boundaries = _phase_altitude_boundaries_ft(
        lower_altitude_ft=lower_altitude_ft,
        upper_altitude_ft=upper_altitude_ft,
        rows=rows,
    )
    band_pairs = list(zip(boundaries, boundaries[1:]))
    if integrate_high_to_low:
        band_pairs.reverse()

    total_hours = 0.0
    total_distance_nm = 0.0
    total_fuel_gal = 0.0
    weighted_tailwind = 0.0
    for band_low_ft, band_high_ft in band_pairs:
        altitude_delta_ft = abs(band_high_ft - band_low_ft)
        if altitude_delta_ft <= 0.0:
            continue
        midpoint_altitude_ft = (band_low_ft + band_high_ft) / 2.0
        row = _vertical_row_for_altitude(rows, midpoint_altitude_ft)
        ias_kts = float(row.ias_kts if row else fallback_ias_kts)
        rate_fpm = max(int(row.rate_fpm if row else fallback_rate_fpm), MIN_PLANNING_RATE_FPM)
        fuel_gph = float(row.fuel_gph if row else fallback_fuel_gph)
        tas_kts = (
            _ias_to_tas(ias_kts, midpoint_altitude_ft)
            if prefer_nominal_ias_tas
            else float(row.tas_kts)
            if row and row.tas_kts is not None
            else _ias_to_tas(ias_kts, midpoint_altitude_ft)
        )
        band_hours = altitude_delta_ft / rate_fpm / 60.0
        tailwind_kts = default_tailwind_kts
        crosswind_kts = default_crosswind_kts
        band_distance_nm = band_hours * _along_track_ground_speed(
            true_airspeed_kts=tas_kts,
            tailwind_kts=tailwind_kts,
            crosswind_kts=crosswind_kts,
            vertical_rate_fpm=rate_fpm,
        )
        if wind_model is not None and (wind_model.station_profiles or wind_model.station_temperature_profiles):
            for _ in range(2):
                sample_latitude, sample_longitude, sample_track_deg = sample_position(
                    total_distance_nm,
                    band_distance_nm,
                )
                sampled_components = _sample_wind_components_from_model(
                    wind_model=wind_model,
                    sample_latitude=sample_latitude,
                    sample_longitude=sample_longitude,
                    altitude_ft=midpoint_altitude_ft,
                    track_deg=sample_track_deg,
                )
                sampled_temperature_c = _sample_temperature_from_model(
                    wind_model=wind_model,
                    sample_latitude=sample_latitude,
                    sample_longitude=sample_longitude,
                    altitude_ft=midpoint_altitude_ft,
                )
                if prefer_nominal_ias_tas:
                    tas_kts = _ias_to_tas_with_temperature(
                        ias_kts,
                        midpoint_altitude_ft,
                        outside_air_temp_c=sampled_temperature_c,
                    )
                if sampled_components is not None:
                    tailwind_kts, crosswind_kts = sampled_components
                band_distance_nm = band_hours * _along_track_ground_speed(
                    true_airspeed_kts=tas_kts,
                    tailwind_kts=tailwind_kts,
                    crosswind_kts=crosswind_kts,
                    vertical_rate_fpm=rate_fpm,
                )
        total_hours += band_hours
        total_distance_nm += band_distance_nm
        total_fuel_gal += band_hours * fuel_gph
        weighted_tailwind += tailwind_kts * band_hours

    average_tailwind = weighted_tailwind / total_hours if total_hours > 0.0 else 0.0
    return total_hours, total_distance_nm, total_fuel_gal, average_tailwind


def _integrate_radial_vertical_phase(
    *,
    origin_latitude: float,
    origin_longitude: float,
    bearing_deg: float,
    distance_offset_nm: float,
    lower_altitude_ft: float,
    upper_altitude_ft: float,
    rows: tuple[VerticalPerformanceRow, ...] | list[VerticalPerformanceRow],
    fallback_ias_kts: int,
    fallback_rate_fpm: int,
    fallback_fuel_gph: float,
    wind_model: RouteWindModel | None,
    integrate_high_to_low: bool = False,
    prefer_nominal_ias_tas: bool = False,
) -> tuple[float, float, float, float]:
    """Integrate a vertical phase moving away from one waypoint on a fixed bearing."""

    def radial_sample(traversed_nm: float, band_distance_nm: float) -> tuple[float, float, float]:
        latitude, longitude = _destination_point_from_bearing_nm(
            latitude=origin_latitude,
            longitude=origin_longitude,
            bearing_deg=bearing_deg,
            distance_nm=distance_offset_nm + traversed_nm + (band_distance_nm / 2.0),
        )
        return latitude, longitude, bearing_deg

    return _integrate_vertical_bands(
        lower_altitude_ft=lower_altitude_ft,
        upper_altitude_ft=upper_altitude_ft,
        rows=rows,
        fallback_ias_kts=fallback_ias_kts,
        fallback_rate_fpm=fallback_rate_fpm,
        fallback_fuel_gph=fallback_fuel_gph,
        default_tailwind_kts=0.0,
        default_crosswind_kts=0.0,
        wind_model=wind_model,
        sample_position=radial_sample,
        integrate_high_to_low=integrate_high_to_low,
        prefer_nominal_ias_tas=prefer_nominal_ias_tas,
    )


def _radial_cruise_distance_nm(
    *,
    origin_latitude: float,
    origin_longitude: float,
    bearing_deg: float,
    start_distance_nm: float,
    altitude_ft: float,
    endurance_hours: float,
    true_airspeed_kts: float,
    wind_model: RouteWindModel | None,
    segments: int = 8,
) -> tuple[float, float]:
    """Estimate cruise distance on a bearing while resampling forecast winds along it."""

    if endurance_hours <= 0.0:
        return 0.0, 0.0

    segment_count = max(int(segments), 1)
    cruise_distance_nm = endurance_hours * max(true_airspeed_kts, 80.0)
    average_tailwind = 0.0
    for _ in range(3):
        ground_speeds: list[float] = []
        tailwinds: list[float] = []
        for segment_idx in range(segment_count):
            sample_fraction = (segment_idx + 0.5) / segment_count
            sample_distance_nm = start_distance_nm + (cruise_distance_nm * sample_fraction)
            sample_latitude, sample_longitude = _destination_point_from_bearing_nm(
                latitude=origin_latitude,
                longitude=origin_longitude,
                bearing_deg=bearing_deg,
                distance_nm=sample_distance_nm,
            )
            tailwind_kts = 0.0
            crosswind_kts = 0.0
            sampled_components = _sample_wind_components_from_model(
                wind_model=wind_model,
                sample_latitude=sample_latitude,
                sample_longitude=sample_longitude,
                altitude_ft=altitude_ft,
                track_deg=bearing_deg,
            )
            if sampled_components is not None:
                tailwind_kts, crosswind_kts = sampled_components
            ground_speeds.append(
                _along_track_ground_speed(
                    true_airspeed_kts=true_airspeed_kts,
                    tailwind_kts=tailwind_kts,
                    crosswind_kts=crosswind_kts,
                    minimum_groundspeed_kts=80.0,
                )
            )
            tailwinds.append(tailwind_kts)
        cruise_distance_nm = endurance_hours * (sum(ground_speeds) / len(ground_speeds))
        average_tailwind = sum(tailwinds) / len(tailwinds)

    return cruise_distance_nm, average_tailwind


def build_alternate_range_rings(
    *,
    destination: AirportData,
    fuel_at_destination_gal: float,
    performance_profile: AircraftPerformanceProfile | None = None,
    cruise_mode_id: str | None = None,
    climb_schedule_id: str | None = None,
    descent_profile_id: str | None = None,
    descent_profile_rate_fpm: int | None = None,
    cruise_weight_lb: float | None = None,
    climb_weight_lb: float | None = None,
    wind_model: RouteWindModel | None = None,
    alt_missed_approach_fuel_gal: float = 5.0,
    max_altitude_agl_ft: int = 20000,
    altitude_step_agl_ft: int = 5000,
) -> tuple[AlternateRangeRing, ...]:
    """Build wind-shaped contextual post-missed range rings around a waypoint."""

    profile = performance_profile
    if profile is None:
        return ()

    fob_gal = max(float(fuel_at_destination_gal), 0.0)
    alt_missed_fuel_gal = max(float(alt_missed_approach_fuel_gal), 0.0)
    if fob_gal <= 0.0:
        return ()
    alt_climb_rows = sample_climb_rows(
        profile,
        climb_schedule_id=climb_schedule_id,
        weight_lb=climb_weight_lb,
    )
    alt_descent_rows = sample_descent_rows(
        profile,
        descent_profile_id=descent_profile_id,
        vertical_rate_fpm=descent_profile_rate_fpm,
    )

    styles = ("8 5", "2 5", "10 4 2 4", "1 4", "12 5")
    rings: list[AlternateRangeRing] = []
    for idx, altitude_agl_ft in enumerate(range(altitude_step_agl_ft, max_altitude_agl_ft + 1, altitude_step_agl_ft)):
        altitude_msl_ft = int(round(destination.elevation_ft + altitude_agl_ft))
        # Range rings operate well below normal cruise levels, so sample the preserved
        # low-altitude PIM rows instead of clamping every ring to FL190 performance.
        sampled_level = max(0, int(round(altitude_msl_ft / 100.0)))
        cruise_sample = sample_cruise_performance(
            profile,
            flight_level=sampled_level,
            cruise_mode_id=cruise_mode_id,
            weight_lb=cruise_weight_lb,
        )
        _climb_hours, _climb_distance_nm, alt_climb_fuel_gal = _phase_performance(
            lower_altitude_ft=float(destination.elevation_ft),
            upper_altitude_ft=float(altitude_msl_ft),
            rows=alt_climb_rows,
            fallback_ias_kts=124,
            fallback_rate_fpm=1500,
            fallback_fuel_gph=FUEL_BURN_GPH,
            wind_kt=0.0,
            crosswind_kt=0.0,
            prefer_nominal_ias_tas=True,
        )
        _descent_hours, _descent_distance_nm, alt_descent_fuel_gal = _phase_performance(
            lower_altitude_ft=float(destination.elevation_ft),
            upper_altitude_ft=float(altitude_msl_ft),
            rows=alt_descent_rows,
            fallback_ias_kts=230,
            fallback_rate_fpm=1500,
            fallback_fuel_gph=FUEL_BURN_GPH,
            wind_kt=0.0,
            crosswind_kt=0.0,
            prefer_nominal_ias_tas=bool(alt_descent_rows),
        )
        # These rings are context graphics, not legal reserve calculations: only the
        # fixed missed allowance plus post-missed climb/descent fuel are removed from expected FOB.
        alt_cruise_fuel_gal = fob_gal - alt_missed_fuel_gal - alt_climb_fuel_gal - alt_descent_fuel_gal
        if alt_cruise_fuel_gal <= 0.0:
            continue
        alt_cruise_endurance_hours = alt_cruise_fuel_gal / max(cruise_sample.fuel_gph, 1.0)
        alt_ring_points: list[tuple[float, float]] = []
        alt_climb_distances_nm: list[float] = []
        alt_cruise_distances_nm: list[float] = []
        alt_descent_distances_nm: list[float] = []
        alt_total_distances_nm: list[float] = []
        for bearing_deg in range(0, 360, 10):
            _alt_climb_h, alt_climb_distance_nm, _alt_climb_f, _alt_climb_wind = _integrate_radial_vertical_phase(
                origin_latitude=destination.latitude,
                origin_longitude=destination.longitude,
                bearing_deg=float(bearing_deg),
                distance_offset_nm=0.0,
                lower_altitude_ft=float(destination.elevation_ft),
                upper_altitude_ft=float(altitude_msl_ft),
                rows=alt_climb_rows,
                fallback_ias_kts=124,
                fallback_rate_fpm=1500,
                fallback_fuel_gph=FUEL_BURN_GPH,
                wind_model=wind_model,
                prefer_nominal_ias_tas=True,
            )
            alt_cruise_distance_nm, _alt_cruise_wind = _radial_cruise_distance_nm(
                origin_latitude=destination.latitude,
                origin_longitude=destination.longitude,
                bearing_deg=float(bearing_deg),
                start_distance_nm=alt_climb_distance_nm,
                altitude_ft=float(altitude_msl_ft),
                endurance_hours=alt_cruise_endurance_hours,
                true_airspeed_kts=float(cruise_sample.tas_kts),
                wind_model=wind_model,
            )
            _alt_descent_h, alt_descent_distance_nm, _alt_descent_f, _alt_descent_wind = _integrate_radial_vertical_phase(
                origin_latitude=destination.latitude,
                origin_longitude=destination.longitude,
                bearing_deg=float(bearing_deg),
                distance_offset_nm=alt_climb_distance_nm + alt_cruise_distance_nm,
                lower_altitude_ft=float(destination.elevation_ft),
                upper_altitude_ft=float(altitude_msl_ft),
                rows=alt_descent_rows,
                fallback_ias_kts=230,
                fallback_rate_fpm=1500,
                fallback_fuel_gph=FUEL_BURN_GPH,
                wind_model=wind_model,
                integrate_high_to_low=True,
                prefer_nominal_ias_tas=bool(alt_descent_rows),
            )
            alt_total_distance_nm = alt_climb_distance_nm + alt_cruise_distance_nm + alt_descent_distance_nm
            point_lat, point_lon = _destination_point_from_bearing_nm(
                latitude=destination.latitude,
                longitude=destination.longitude,
                bearing_deg=float(bearing_deg),
                distance_nm=alt_total_distance_nm,
            )
            alt_ring_points.append((point_lon, point_lat))
            alt_climb_distances_nm.append(alt_climb_distance_nm)
            alt_cruise_distances_nm.append(alt_cruise_distance_nm)
            alt_descent_distances_nm.append(alt_descent_distance_nm)
            alt_total_distances_nm.append(alt_total_distance_nm)
        alt_average_climb_distance_nm = sum(alt_climb_distances_nm) / len(alt_climb_distances_nm)
        alt_average_cruise_distance_nm = sum(alt_cruise_distances_nm) / len(alt_cruise_distances_nm)
        alt_average_descent_distance_nm = sum(alt_descent_distances_nm) / len(alt_descent_distances_nm)
        alt_average_total_distance_nm = sum(alt_total_distances_nm) / len(alt_total_distances_nm)
        rings.append(
            AlternateRangeRing(
                altitude_agl_ft=altitude_agl_ft,
                altitude_msl_ft=altitude_msl_ft,
                alt_cruise_fuel_gal=alt_cruise_fuel_gal,
                alt_climb_fuel_gal=alt_climb_fuel_gal,
                alt_descent_fuel_gal=alt_descent_fuel_gal,
                alt_missed_approach_fuel_gal=alt_missed_fuel_gal,
                alt_climb_distance_nm=alt_average_climb_distance_nm,
                alt_cruise_distance_nm=alt_average_cruise_distance_nm,
                alt_descent_distance_nm=alt_average_descent_distance_nm,
                alt_min_range_nm=min(alt_total_distances_nm),
                alt_max_range_nm=max(alt_total_distances_nm),
                alt_average_range_nm=alt_average_total_distance_nm,
                points=tuple(alt_ring_points),
                line_style=styles[idx % len(styles)],
                label=f"{altitude_agl_ft // 1000}k AGL",
            )
        )
    return tuple(rings)


def _reference_time_at_distance_utc(
    *,
    departure_time_utc: dt.datetime | None,
    mission_distance_nm: float,
    profile: FlightLevelProfile,
    distance_nm: float,
) -> dt.datetime | None:
    """Estimate elapsed time at an arbitrary route distance for adaptive hazard bins."""

    if departure_time_utc is None:
        return None
    if mission_distance_nm <= 0.0:
        return departure_time_utc

    clamped_distance_nm = min(max(distance_nm, 0.0), mission_distance_nm)
    descent_start_distance_nm = mission_distance_nm - profile.descent_distance_nm
    cruise_hours_total = sum(profile.cruise_segment_hours)

    if profile.climb_distance_nm > 0.0 and clamped_distance_nm <= profile.climb_distance_nm:
        elapsed_hours = profile.climb_hours * (clamped_distance_nm / profile.climb_distance_nm)
    elif profile.descent_distance_nm > 0.0 and clamped_distance_nm >= descent_start_distance_nm:
        distance_into_descent_nm = clamped_distance_nm - descent_start_distance_nm
        elapsed_hours = (
            profile.climb_hours
            + cruise_hours_total
            + profile.descent_hours * (distance_into_descent_nm / profile.descent_distance_nm)
        )
    elif profile.remaining_distance_nm > 0.0 and cruise_hours_total > 0.0:
        distance_into_cruise_nm = max(clamped_distance_nm - profile.climb_distance_nm, 0.0)
        cruise_ratio = min(max(distance_into_cruise_nm / profile.remaining_distance_nm, 0.0), 1.0)
        elapsed_hours = profile.climb_hours + (cruise_hours_total * cruise_ratio)
    else:
        elapsed_hours = min(profile.total_hours, profile.climb_hours)

    return departure_time_utc + dt.timedelta(hours=elapsed_hours)


def _altitude_at_distance_nm(
    *,
    distance_nm: float,
    mission_distance_nm: float,
    cruise_altitude_ft: float,
    departure_elevation_ft: float,
    destination_elevation_ft: float,
    profile: FlightLevelProfile,
) -> float:
    """Return aircraft altitude at a route distance from the integrated profile."""

    if mission_distance_nm <= 0.0:
        return departure_elevation_ft

    clamped_distance_nm = min(max(distance_nm, 0.0), mission_distance_nm)
    descent_start_distance_nm = mission_distance_nm - profile.descent_distance_nm

    if profile.climb_distance_nm > 0.0 and clamped_distance_nm <= profile.climb_distance_nm:
        climb_ratio = clamped_distance_nm / profile.climb_distance_nm
        return departure_elevation_ft + ((cruise_altitude_ft - departure_elevation_ft) * climb_ratio)

    if profile.descent_distance_nm > 0.0 and clamped_distance_nm >= descent_start_distance_nm:
        descent_ratio = (clamped_distance_nm - descent_start_distance_nm) / profile.descent_distance_nm
        return cruise_altitude_ft + ((destination_elevation_ft - cruise_altitude_ft) * descent_ratio)

    return cruise_altitude_ft


def _interval_altitude_band_ft(
    *,
    start_distance_nm: float,
    end_distance_nm: float,
    mission_distance_nm: float,
    cruise_altitude_ft: float,
    departure_elevation_ft: float,
    destination_elevation_ft: float,
    profile: FlightLevelProfile,
) -> tuple[int, int]:
    """Return the min/max aircraft altitude across a route distance interval."""

    if mission_distance_nm <= 0.0:
        altitude_ft = int(round(departure_elevation_ft))
        return altitude_ft, altitude_ft

    climb_end_distance_nm = profile.climb_distance_nm
    descent_start_distance_nm = mission_distance_nm - profile.descent_distance_nm

    sample_distances = [start_distance_nm, end_distance_nm]
    for breakpoint_nm in (climb_end_distance_nm, descent_start_distance_nm):
        if start_distance_nm < breakpoint_nm < end_distance_nm:
            sample_distances.append(breakpoint_nm)

    altitudes_ft = [
        _altitude_at_distance_nm(
            distance_nm=distance_nm,
            mission_distance_nm=mission_distance_nm,
            cruise_altitude_ft=cruise_altitude_ft,
            departure_elevation_ft=departure_elevation_ft,
            destination_elevation_ft=destination_elevation_ft,
            profile=profile,
        )
        for distance_nm in sample_distances
    ]
    return int(min(altitudes_ft)), int(max(altitudes_ft))


def _altitude_band_overlaps(
    *,
    band_low_ft: int,
    band_high_ft: int,
    hazard_base_ft: int,
    hazard_top_ft: int,
    tolerance_ft: int = ALTITUDE_BAND_OVERLAP_TOLERANCE_FT,
) -> bool:
    """Return whether an aircraft altitude band intersects a hazard altitude band."""

    return (band_low_ft - tolerance_ft) <= hazard_top_ft and (band_high_ft + tolerance_ft) >= hazard_base_ft


def _adaptive_route_breakpoints_nm(
    *,
    mission_distance_nm: float,
    base_segments: int,
    profile: FlightLevelProfile,
    route_plan: RoutePlan | None,
) -> list[float]:
    """Build route bins around equal spacing, route waypoints, and vertical transitions."""

    if mission_distance_nm <= 0.0:
        return [0.0]
    breakpoints = {0.0, mission_distance_nm}
    for idx in range(1, max(base_segments, 1)):
        breakpoints.add((mission_distance_nm * idx) / max(base_segments, 1))
    for breakpoint_nm in (profile.climb_distance_nm, mission_distance_nm - profile.descent_distance_nm):
        if 0.0 < breakpoint_nm < mission_distance_nm:
            breakpoints.add(breakpoint_nm)
    if route_plan is not None and route_plan.legs:
        cumulative_nm = 0.0
        for leg in route_plan.legs[:-1]:
            cumulative_nm += leg.distance_nm
            if 0.0 < cumulative_nm < mission_distance_nm:
                breakpoints.add(cumulative_nm)
    ordered = sorted(breakpoints)
    return [min(max(value, 0.0), mission_distance_nm) for value in ordered if 0.0 <= value <= mission_distance_nm]


def evaluate_route_hazards(
    departure: AirportData,
    destination: AirportData,
    *,
    hazard_areas: list[HazardArea],
    reference_time_utc: dt.datetime | None,
    flight_levels: list[int] | None = None,
    segments: int = SEGMENTS,
    climb_rate_fpm: int = 2200,
    descent_rate_fpm: int = 1500,
    cruise_tas_kts: int = 315,
    climb_ias_kts: int = 124,
    descent_ias_kts: int = 220,
    performance_profile: AircraftPerformanceProfile | None = None,
    cruise_mode_id: str | None = None,
    climb_schedule_id: str | None = None,
    upper_climb_schedule_id: str | None = None,
    climb_transition_altitude_ft: float | None = None,
    descent_profile_id: str | None = None,
    descent_profile_rate_fpm: int | None = None,
    cruise_weight_lb: float | None = None,
    climb_weight_lb: float | None = None,
    wind_model: RouteWindModel | None = None,
    route_plan: RoutePlan | None = None,
) -> dict[int, list[SegmentHazard]]:
    """Score route segments by hazard overlap, altitude band, and segment ETA."""

    if reference_time_utc is not None:
        reference_time_utc = (
            reference_time_utc.replace(tzinfo=dt.timezone.utc)
            if reference_time_utc.tzinfo is None
            else reference_time_utc.astimezone(dt.timezone.utc)
        )
    levels = flight_levels or FLIGHT_LEVELS
    by_fl: dict[int, list[SegmentHazard]] = {}
    latest_gairmet_valid_to = _latest_gairmet_valid_to_by_source(hazard_areas)
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
    is_westbound = is_westbound_route(departure, destination)

    for fl in levels:
        cruise_altitude_ft = float(fl * 100)
        profile = _flight_level_profile(
            flight_level=fl,
            is_return=is_westbound,
            mission_distance_nm=mission_distance_nm,
            departure_latitude=departure.latitude,
            departure_longitude=departure.longitude,
            destination_latitude=destination.latitude,
            destination_longitude=destination.longitude,
            departure_elevation_ft=departure.elevation_ft,
            destination_elevation_ft=destination.elevation_ft,
            climb_rate_fpm=climb_rate_fpm,
            descent_rate_fpm=descent_rate_fpm,
            cruise_tas_kts=cruise_tas_kts,
            climb_ias_kts=climb_ias_kts,
            descent_ias_kts=descent_ias_kts,
            performance_profile=performance_profile,
            cruise_mode_id=cruise_mode_id,
            climb_schedule_id=climb_schedule_id,
            upper_climb_schedule_id=upper_climb_schedule_id,
            climb_transition_altitude_ft=climb_transition_altitude_ft,
            descent_profile_id=descent_profile_id,
            descent_profile_rate_fpm=descent_profile_rate_fpm,
            cruise_weight_lb=cruise_weight_lb,
            climb_weight_lb=climb_weight_lb,
            wind_model=wind_model,
            route_plan=route_plan,
            segment_count=segments,
        )
        breakpoints_nm = _adaptive_route_breakpoints_nm(
            mission_distance_nm=mission_distance_nm,
            base_segments=segments,
            profile=profile,
            route_plan=route_plan,
        )
        rows: list[SegmentHazard] = []
        for idx, (start_distance_nm, end_distance_nm) in enumerate(zip(breakpoints_nm, breakpoints_nm[1:])):
            segment_distance_nm = max(end_distance_nm - start_distance_nm, 0.0)
            midpoint_distance_nm = (start_distance_nm + end_distance_nm) / 2.0
            lat, lon = _route_point_at_distance_nm(
                departure_latitude=departure.latitude,
                departure_longitude=departure.longitude,
                destination_latitude=destination.latitude,
                destination_longitude=destination.longitude,
                mission_distance_nm=mission_distance_nm,
                distance_from_departure_nm=midpoint_distance_nm,
                route_plan=route_plan,
            )
            segment_altitude_low_ft, segment_altitude_high_ft = _interval_altitude_band_ft(
                start_distance_nm=start_distance_nm,
                end_distance_nm=end_distance_nm,
                mission_distance_nm=mission_distance_nm,
                cruise_altitude_ft=cruise_altitude_ft,
                departure_elevation_ft=departure.elevation_ft,
                destination_elevation_ft=destination.elevation_ft,
                profile=profile,
            )

            icing_score = 0
            turbulence_score = 0
            convective_score = 0
            ifr_score = 0
            mountain_obscuration_score = 0
            surface_wind_score = 0
            llws_score = 0
            sources: list[str] = []
            segment_reference_time_utc = _reference_time_at_distance_utc(
                departure_time_utc=reference_time_utc,
                mission_distance_nm=mission_distance_nm,
                profile=profile,
                distance_nm=midpoint_distance_nm,
            )

            # A hazard only counts when time, altitude, and geometry all line up for the segment.
            for area in hazard_areas:
                applies, use_latest_gairmet = hazard_applies_at(
                    area, segment_reference_time_utc, latest_gairmet_valid_to
                )
                if not applies:
                    continue
                if not _altitude_band_overlaps(
                    band_low_ft=segment_altitude_low_ft,
                    band_high_ft=segment_altitude_high_ft,
                    hazard_base_ft=area.base_ft,
                    hazard_top_ft=area.top_ft,
                ):
                    continue
                if not _route_interval_intersects_polygons(
                    departure_latitude=departure.latitude,
                    departure_longitude=departure.longitude,
                    destination_latitude=destination.latitude,
                    destination_longitude=destination.longitude,
                    mission_distance_nm=mission_distance_nm,
                    start_distance_nm=start_distance_nm,
                    end_distance_nm=end_distance_nm,
                    polygons=area.polygons,
                    route_plan=route_plan,
                ):
                    continue

                if area.hazard_type == "icing":
                    icing_score = max(icing_score, area.severity_score)
                elif area.hazard_type == "turbulence":
                    turbulence_score = max(turbulence_score, area.severity_score)
                elif area.hazard_type == "convective":
                    convective_score = max(convective_score, area.severity_score)
                elif area.hazard_type == "ifr":
                    ifr_score = max(ifr_score, area.severity_score)
                elif area.hazard_type == "mountain_obscuration":
                    mountain_obscuration_score = max(mountain_obscuration_score, area.severity_score)
                elif area.hazard_type == "surface_wind":
                    surface_wind_score = max(surface_wind_score, area.severity_score)
                elif area.hazard_type == "llws":
                    llws_score = max(llws_score, area.severity_score)

                source_label = (
                    f"{area.source} (latest snapshot beyond forecast horizon)"
                    if use_latest_gairmet
                    else area.source
                )
                if source_label not in sources and len(sources) < 4:
                    sources.append(source_label)

            overall_score = max(
                icing_score,
                turbulence_score,
                convective_score,
                ifr_score,
                mountain_obscuration_score,
                surface_wind_score,
                llws_score,
            )

            rows.append(
                SegmentHazard(
                    segment_index=idx + 1,
                    segment_distance_nm=round(segment_distance_nm, 1),
                    latitude=round(lat, 2),
                    longitude=round(lon, 2),
                    icing_score=icing_score,
                    turbulence_score=turbulence_score,
                    convective_score=convective_score,
                    overall_score=overall_score,
                    sources="; ".join(sources) if sources else "None",
                    ifr_score=ifr_score,
                    mountain_obscuration_score=mountain_obscuration_score,
                    surface_wind_score=surface_wind_score,
                    llws_score=llws_score,
                )
            )

        by_fl[fl] = rows

    return by_fl


def _group_contiguous_indexes(indexes: list[int]) -> list[tuple[int, int]]:
    """Collapse sorted or unsorted indexes into inclusive contiguous ranges."""

    if not indexes:
        return []

    ordered_indexes = sorted(set(indexes))
    groups: list[tuple[int, int]] = []
    group_start = ordered_indexes[0]
    group_end = ordered_indexes[0]

    for value in ordered_indexes[1:]:
        if value == group_end + 1:
            group_end = value
            continue
        groups.append((group_start, group_end))
        group_start = value
        group_end = value

    groups.append((group_start, group_end))
    return groups


def build_route_vertical_profile(
    departure: AirportData,
    destination: AirportData,
    *,
    hazard_areas: list[HazardArea],
    reference_time_utc: dt.datetime | None,
    flight_level: int,
    segments: int = SEGMENTS,
    climb_rate_fpm: int = 2200,
    descent_rate_fpm: int = 1500,
    cruise_tas_kts: int = 315,
    climb_ias_kts: int = 124,
    descent_ias_kts: int = 220,
    performance_profile: AircraftPerformanceProfile | None = None,
    cruise_mode_id: str | None = None,
    climb_schedule_id: str | None = None,
    upper_climb_schedule_id: str | None = None,
    climb_transition_altitude_ft: float | None = None,
    descent_profile_id: str | None = None,
    descent_profile_rate_fpm: int | None = None,
    cruise_weight_lb: float | None = None,
    climb_weight_lb: float | None = None,
    wind_model: RouteWindModel | None = None,
    route_plan: RoutePlan | None = None,
) -> RouteVerticalProfile:
    """Build the side-profile geometry and collapse impacted segments into hazard spans."""

    waypoint_markers: list[RouteVerticalProfileWaypointMarker] = []
    if route_plan is not None and len(route_plan.waypoints) > 2:
        cumulative_distance_nm = 0.0
        for leg_index, leg in enumerate(route_plan.legs):
            cumulative_distance_nm += leg.distance_nm
            if leg_index >= len(route_plan.legs) - 1:
                continue
            # Intermediate markers use cumulative route distance so the profile x-axis matches the flown path.
            waypoint_markers.append(
                RouteVerticalProfileWaypointMarker(
                    identifier=leg.end_waypoint.identifier,
                    distance_nm=round(cumulative_distance_nm, 1),
                    is_fuel_stop=bool(getattr(leg.end_waypoint, "is_fuel_stop", False)),
                )
            )

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
    is_westbound = is_westbound_route(departure, destination)
    cruise_altitude_ft = int(flight_level * 100)
    profile = _flight_level_profile(
        flight_level=flight_level,
        is_return=is_westbound,
        mission_distance_nm=mission_distance_nm,
        departure_latitude=departure.latitude,
        departure_longitude=departure.longitude,
        destination_latitude=destination.latitude,
        destination_longitude=destination.longitude,
        departure_elevation_ft=departure.elevation_ft,
        destination_elevation_ft=destination.elevation_ft,
        climb_rate_fpm=climb_rate_fpm,
        descent_rate_fpm=descent_rate_fpm,
        cruise_tas_kts=cruise_tas_kts,
        climb_ias_kts=climb_ias_kts,
        descent_ias_kts=descent_ias_kts,
        performance_profile=performance_profile,
        cruise_mode_id=cruise_mode_id,
        climb_schedule_id=climb_schedule_id,
        upper_climb_schedule_id=upper_climb_schedule_id,
        climb_transition_altitude_ft=climb_transition_altitude_ft,
        descent_profile_id=descent_profile_id,
        descent_profile_rate_fpm=descent_profile_rate_fpm,
        cruise_weight_lb=cruise_weight_lb,
        climb_weight_lb=climb_weight_lb,
        wind_model=wind_model,
        route_plan=route_plan,
        segment_count=segments,
    )

    sample_distances = {0.0, mission_distance_nm}
    if mission_distance_nm > 0.0:
        # Include climb/descent breakpoints and evenly spaced cruise samples so the rendered path
        # bends where the vertical profile actually transitions instead of looking like one segment.
        sample_distances.add(profile.climb_distance_nm)
        sample_distances.add(max(mission_distance_nm - profile.descent_distance_nm, 0.0))
        for idx in range((segments * 2) + 1):
            sample_distances.add((mission_distance_nm * idx) / max(segments * 2, 1))

    path_points = [
        RouteVerticalProfilePoint(
            distance_nm=round(distance_nm, 1),
            altitude_ft=int(
                round(
                    _altitude_at_distance_nm(
                        distance_nm=distance_nm,
                        mission_distance_nm=mission_distance_nm,
                        cruise_altitude_ft=float(cruise_altitude_ft),
                        departure_elevation_ft=departure.elevation_ft,
                        destination_elevation_ft=destination.elevation_ft,
                        profile=profile,
                    )
                )
            ),
        )
        for distance_nm in sorted(sample_distances)
    ]

    if segments <= 0 or mission_distance_nm <= 0.0:
        return RouteVerticalProfile(
            flight_level=flight_level,
            mission_distance_nm=mission_distance_nm,
            cruise_altitude_ft=cruise_altitude_ft,
            departure_elevation_ft=int(round(departure.elevation_ft)),
            destination_elevation_ft=int(round(destination.elevation_ft)),
            path_points=path_points,
            hazard_spans=[],
            waypoint_markers=waypoint_markers,
        )

    breakpoints_nm = _adaptive_route_breakpoints_nm(
        mission_distance_nm=mission_distance_nm,
        base_segments=segments,
        profile=profile,
        route_plan=route_plan,
    )
    hazard_spans: list[RouteVerticalProfileHazardSpan] = []
    latest_gairmet_valid_to = _latest_gairmet_valid_to_by_source(hazard_areas)
    for area in hazard_areas:
        intersecting_indexes: list[int] = []
        area_used_horizon_fallback = False
        for idx, (start_distance_nm, end_distance_nm) in enumerate(zip(breakpoints_nm, breakpoints_nm[1:])):
            midpoint_distance_nm = (start_distance_nm + end_distance_nm) / 2.0
            segment_reference_time_utc = _reference_time_at_distance_utc(
                departure_time_utc=reference_time_utc,
                mission_distance_nm=mission_distance_nm,
                profile=profile,
                distance_nm=midpoint_distance_nm,
            )
            applies, used_horizon_fallback = hazard_applies_at(
                area, segment_reference_time_utc, latest_gairmet_valid_to
            )
            if not applies:
                continue
            area_used_horizon_fallback = area_used_horizon_fallback or used_horizon_fallback
            if not _route_interval_intersects_polygons(
                departure_latitude=departure.latitude,
                departure_longitude=departure.longitude,
                destination_latitude=destination.latitude,
                destination_longitude=destination.longitude,
                mission_distance_nm=mission_distance_nm,
                start_distance_nm=start_distance_nm,
                end_distance_nm=end_distance_nm,
                polygons=area.polygons,
                route_plan=route_plan,
            ):
                continue
            intersecting_indexes.append(idx)

        span_source = (
            f"{area.source} (latest snapshot beyond forecast horizon)"
            if area_used_horizon_fallback
            else area.source
        )
        for start_idx, end_idx in _group_contiguous_indexes(intersecting_indexes):
            hazard_spans.append(
                RouteVerticalProfileHazardSpan(
                    hazard_type=area.hazard_type,
                    severity_score=int(area.severity_score),
                    base_ft=int(area.base_ft),
                    top_ft=int(area.top_ft),
                    start_distance_nm=round(breakpoints_nm[start_idx], 1),
                    end_distance_nm=round(breakpoints_nm[end_idx + 1], 1),
                    source=span_source,
                )
            )

    hazard_priority = {"convective": 0, "turbulence": 1, "icing": 2}
    hazard_spans.sort(
        key=lambda span: (
            hazard_priority.get(span.hazard_type, 9),
            span.start_distance_nm,
            span.base_ft,
            span.top_ft,
            span.source,
        )
    )

    return RouteVerticalProfile(
        flight_level=flight_level,
        mission_distance_nm=mission_distance_nm,
        cruise_altitude_ft=cruise_altitude_ft,
        departure_elevation_ft=int(round(departure.elevation_ft)),
        destination_elevation_ft=int(round(destination.elevation_ft)),
        path_points=path_points,
        hazard_spans=hazard_spans,
        waypoint_markers=waypoint_markers,
    )


def _segment_wind(fl: int, segment_idx: int, is_return: bool) -> float:
    """Return the deterministic fallback segment wind used when no NOAA model exists."""

    base_wind = 40 + (fl - 260) // 2
    segment_wind = base_wind + (25 if 4 <= segment_idx <= 8 else 0)
    if is_return:
        return -segment_wind
    return segment_wind * 0.45


def _ias_to_tas(ias_kts: float, avg_altitude_ft: float) -> float:
    """
    Convert IAS to TAS using a standard-atmosphere density ratio.

    IAS is treated as an EAS-like input, which is a better approximation than the
    older fixed +2% per 1,000 ft shortcut for the mission-planning use case here.
    """
    return _ias_to_tas_with_temperature(ias_kts, avg_altitude_ft, outside_air_temp_c=None)


def _ias_to_tas_with_temperature(
    ias_kts: float,
    avg_altitude_ft: float,
    *,
    outside_air_temp_c: float | None,
) -> float:
    """
    Convert IAS to TAS using pressure altitude plus either sampled OAT or ISA.

    When forecast temperatures are available from NOAA FD windtemps, using them here
    lets climb/descent distance respond to the selected IAS profile instead of being
    locked to the source-table TAS baked into another schedule.
    """
    altitude_m = max(avg_altitude_ft, 0.0) * 0.3048
    sea_level_temp_k = 288.15
    lapse_rate_k_per_m = 0.0065
    gravity_m_per_s2 = 9.80665
    gas_constant_air = 287.05
    gamma_air = 1.4
    sea_level_pressure_pa = 101325.0
    meters_per_second_per_knot = 0.514444

    standard_temperature_k = sea_level_temp_k - (lapse_rate_k_per_m * altitude_m)
    temperature_k = (
        float(outside_air_temp_c) + 273.15
        if outside_air_temp_c is not None
        else standard_temperature_k
    )
    if temperature_k <= 0.0 or standard_temperature_k <= 0.0:
        return max(ias_kts, 60.0)

    # Treat IAS as CAS for mission-planning purposes, then convert to Mach/TAS using the
    # standard pitot-static compressible-flow relationship at the sampled pressure altitude.
    pressure_ratio = (standard_temperature_k / sea_level_temp_k) ** (
        gravity_m_per_s2 / (gas_constant_air * lapse_rate_k_per_m)
    )
    static_pressure_pa = sea_level_pressure_pa * pressure_ratio
    if static_pressure_pa <= 0.0:
        return max(ias_kts, 60.0)

    calibrated_speed_mps = max(float(ias_kts), 0.0) * meters_per_second_per_knot
    sea_level_sound_speed_mps = math.sqrt(gamma_air * gas_constant_air * sea_level_temp_k)
    impact_pressure_pa = sea_level_pressure_pa * (
        (
            1.0
            + ((gamma_air - 1.0) / 2.0)
            * ((calibrated_speed_mps / max(sea_level_sound_speed_mps, 1e-6)) ** 2)
        ) ** (gamma_air / (gamma_air - 1.0))
        - 1.0
    )
    mach_squared = (2.0 / (gamma_air - 1.0)) * (
        ((impact_pressure_pa / static_pressure_pa) + 1.0) ** ((gamma_air - 1.0) / gamma_air) - 1.0
    )
    mach = math.sqrt(max(mach_squared, 0.0))
    sound_speed_mps = math.sqrt(gamma_air * gas_constant_air * temperature_k)
    tas_kts = (mach * sound_speed_mps) / meters_per_second_per_knot
    return max(tas_kts, 60.0)


def _vertical_speed_kts(rate_fpm: float) -> float:
    """Convert vertical speed in feet per minute to nautical miles per hour."""

    return abs(rate_fpm) * 60.0 / FEET_PER_NAUTICAL_MILE


def _along_track_ground_speed(
    *,
    true_airspeed_kts: float,
    tailwind_kts: float,
    crosswind_kts: float,
    vertical_rate_fpm: float = 0.0,
    minimum_groundspeed_kts: float = 60.0,
) -> float:
    """Compute along-track groundspeed after vertical rate and crosswind components."""

    vertical_speed_kts = _vertical_speed_kts(vertical_rate_fpm)
    horizontal_airspeed_kts = math.sqrt(max((true_airspeed_kts ** 2) - (vertical_speed_kts ** 2), 0.0))
    required_crosswind_kts = abs(crosswind_kts)
    along_air_component_kts = math.sqrt(
        max((horizontal_airspeed_kts ** 2) - (required_crosswind_kts ** 2), 0.0)
    )
    return max(along_air_component_kts + tailwind_kts, minimum_groundspeed_kts)


def _vertical_row_for_altitude(
    rows: tuple[VerticalPerformanceRow, ...] | list[VerticalPerformanceRow],
    altitude_ft: float,
) -> VerticalPerformanceRow | None:
    """Select the vertical performance table row covering a target altitude."""

    if not rows:
        return None

    sorted_rows = sorted(rows, key=lambda row: (row.start_altitude_ft, row.end_altitude_ft))
    for row in sorted_rows:
        if row.start_altitude_ft <= altitude_ft < row.end_altitude_ft:
            return row

    if altitude_ft < sorted_rows[0].start_altitude_ft:
        return sorted_rows[0]
    return sorted_rows[-1]


def _phase_altitude_boundaries_ft(
    *,
    lower_altitude_ft: float,
    upper_altitude_ft: float,
    rows: tuple[VerticalPerformanceRow, ...] | list[VerticalPerformanceRow],
) -> list[float]:
    """Return altitude breakpoints where vertical performance table rows change."""

    low_ft = min(lower_altitude_ft, upper_altitude_ft)
    high_ft = max(lower_altitude_ft, upper_altitude_ft)
    boundaries_ft = {float(low_ft), float(high_ft)}

    for row in rows:
        if low_ft < row.start_altitude_ft < high_ft:
            boundaries_ft.add(float(row.start_altitude_ft))
        if low_ft < row.end_altitude_ft < high_ft:
            boundaries_ft.add(float(row.end_altitude_ft))

    return sorted(boundaries_ft)


def _phase_performance(
    *,
    lower_altitude_ft: float,
    upper_altitude_ft: float,
    rows: tuple[VerticalPerformanceRow, ...] | list[VerticalPerformanceRow],
    fallback_ias_kts: int,
    fallback_rate_fpm: int,
    fallback_fuel_gph: float,
    wind_kt: float,
    crosswind_kt: float,
    prefer_nominal_ias_tas: bool = False,
) -> tuple[float, float, float]:
    """Integrate one vertical phase when only a single representative wind is available."""

    hours, distance_nm, fuel_gal, _average_tailwind = _integrate_vertical_bands(
        lower_altitude_ft=lower_altitude_ft,
        upper_altitude_ft=upper_altitude_ft,
        rows=rows,
        fallback_ias_kts=fallback_ias_kts,
        fallback_rate_fpm=fallback_rate_fpm,
        fallback_fuel_gph=fallback_fuel_gph,
        default_tailwind_kts=wind_kt,
        default_crosswind_kts=crosswind_kt,
        # No model means the sampler is never invoked; the constant winds apply.
        wind_model=None,
        sample_position=lambda traversed_nm, band_distance_nm: (0.0, 0.0, 0.0),
        prefer_nominal_ias_tas=prefer_nominal_ias_tas,
    )
    return hours, distance_nm, fuel_gal


def _integrate_vertical_phase(
    *,
    lower_altitude_ft: float,
    upper_altitude_ft: float,
    rows: tuple[VerticalPerformanceRow, ...] | list[VerticalPerformanceRow],
    fallback_ias_kts: int,
    fallback_rate_fpm: int,
    fallback_fuel_gph: float,
    default_tailwind_kts: float,
    default_crosswind_kts: float,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    mission_distance_nm: float,
    track_deg: float,
    wind_model: RouteWindModel | None,
    route_plan: RoutePlan | None = None,
    integrate_from_destination: bool = False,
    prefer_nominal_ias_tas: bool = False,
) -> tuple[float, float, float, float]:
    """Integrate climb/descent bands while sampling the flown route geometry."""

    def route_sample(traversed_nm: float, band_distance_nm: float) -> tuple[float, float, float]:
        distance_nm = (
            mission_distance_nm - traversed_nm - (band_distance_nm / 2.0)
            if integrate_from_destination
            else traversed_nm + (band_distance_nm / 2.0)
        )
        latitude, longitude = _route_point_at_distance_nm(
            departure_latitude=departure_latitude,
            departure_longitude=departure_longitude,
            destination_latitude=destination_latitude,
            destination_longitude=destination_longitude,
            mission_distance_nm=mission_distance_nm,
            distance_from_departure_nm=distance_nm,
            route_plan=route_plan,
        )
        sampled_track = _route_track_at_distance_nm(
            departure_latitude=departure_latitude,
            departure_longitude=departure_longitude,
            destination_latitude=destination_latitude,
            destination_longitude=destination_longitude,
            distance_from_departure_nm=distance_nm,
            route_plan=route_plan,
        )
        return latitude, longitude, sampled_track if sampled_track is not None else track_deg

    return _integrate_vertical_bands(
        lower_altitude_ft=lower_altitude_ft,
        upper_altitude_ft=upper_altitude_ft,
        rows=rows,
        fallback_ias_kts=fallback_ias_kts,
        fallback_rate_fpm=fallback_rate_fpm,
        fallback_fuel_gph=fallback_fuel_gph,
        default_tailwind_kts=default_tailwind_kts,
        default_crosswind_kts=default_crosswind_kts,
        wind_model=wind_model,
        sample_position=route_sample,
        # Bands must ascend so `traversed` accumulates from the destination end of the
        # route: pairing high-first ordering with the from-destination position formula
        # mirrors the descent geography (touchdown winds sampled at TOD and vice versa).
        integrate_high_to_low=False,
        prefer_nominal_ias_tas=prefer_nominal_ias_tas,
    )


def _flight_level_profile(
    *,
    flight_level: int,
    is_return: bool,
    mission_distance_nm: float,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    departure_elevation_ft: float,
    destination_elevation_ft: float,
    climb_rate_fpm: int,
    descent_rate_fpm: int,
    cruise_tas_kts: int,
    climb_ias_kts: int,
    descent_ias_kts: int,
    performance_profile: AircraftPerformanceProfile | None = None,
    cruise_mode_id: str | None = None,
    climb_schedule_id: str | None = None,
    upper_climb_schedule_id: str | None = None,
    climb_transition_altitude_ft: float | None = None,
    descent_profile_id: str | None = None,
    descent_profile_rate_fpm: int | None = None,
    fixed_fuel_gal_override: float | None = None,
    cruise_weight_lb: float | None = None,
    climb_weight_lb: float | None = None,
    wind_model: RouteWindModel | None = None,
    route_plan: RoutePlan | None = None,
    segment_count: int = SEGMENTS,
) -> FlightLevelProfile:
    """Combine selected performance tables with route-aware winds for one flight level."""

    cruise_altitude_ft = float(flight_level * 100)
    track_deg = _initial_track_deg(
        departure_latitude,
        departure_longitude,
        destination_latitude,
        destination_longitude,
    )
    if route_plan is not None:
        track_deg = _route_track_at_distance_nm(
            departure_latitude=departure_latitude,
            departure_longitude=departure_longitude,
            destination_latitude=destination_latitude,
            destination_longitude=destination_longitude,
            distance_from_departure_nm=mission_distance_nm / 2.0 if mission_distance_nm > 0.0 else 0.0,
            route_plan=route_plan,
        )
    if wind_model is not None and wind_model.track_deg is not None:
        track_deg = wind_model.track_deg

    if (
        wind_model
        and flight_level in wind_model.segment_tailwind_by_fl
        and len(wind_model.segment_tailwind_by_fl[flight_level]) == segment_count
    ):
        default_segment_winds = wind_model.segment_tailwind_by_fl[flight_level]
        default_segment_crosswinds = wind_model.segment_crosswind_by_fl.get(
            flight_level,
            [0.0 for _ in range(segment_count)],
        )
        default_climb_wind = wind_model.climb_tailwind_by_fl.get(flight_level, default_segment_winds[0])
        default_climb_crosswind = wind_model.climb_crosswind_by_fl.get(
            flight_level,
            default_segment_crosswinds[0],
        )
        default_descent_wind = wind_model.descent_tailwind_by_fl.get(
            flight_level,
            default_segment_winds[-1],
        )
        default_descent_crosswind = wind_model.descent_crosswind_by_fl.get(
            flight_level,
            default_segment_crosswinds[-1],
        )
    else:
        # A present NOAA model with uncovered dynamic bins falls back to calm;
        # the synthetic profile is reserved for missions with no NOAA model at all.
        default_segment_winds = (
            [0.0 for _ in range(segment_count)]
            if wind_model is not None
            else [_segment_wind(flight_level, idx, is_return) for idx in range(segment_count)]
        )
        default_segment_crosswinds = [0.0 for _ in range(segment_count)]
        default_climb_wind = default_segment_winds[0] * 0.6
        default_climb_crosswind = 0.0
        default_descent_wind = default_segment_winds[-1] * 0.6
        default_descent_crosswind = 0.0

    active_cruise_tas_kts = float(cruise_tas_kts)
    cruise_fuel_gph = FUEL_BURN_GPH
    fixed_fuel_gal = FIXED_FUEL_GAL
    climb_rows: tuple[VerticalPerformanceRow, ...] | list[VerticalPerformanceRow] = ()
    descent_rows: tuple[VerticalPerformanceRow, ...] | list[VerticalPerformanceRow] = ()
    performance_limit_notes: list[str] = []

    if performance_profile is not None:
        # Cruise/climb/descent each sample their own forecast temperature because the aircraft
        # sees materially different altitude and route positions in each phase.
        cruise_temperature_offset_c = _temperature_offset_from_model(
            wind_model=wind_model,
            departure_latitude=departure_latitude,
            departure_longitude=departure_longitude,
            destination_latitude=destination_latitude,
            destination_longitude=destination_longitude,
            mission_distance_nm=mission_distance_nm,
            altitude_ft=cruise_altitude_ft,
            sample_distances_nm=[
                mission_distance_nm * ((segment_idx + 0.5) / max(segment_count, 1))
                for segment_idx in range(max(segment_count, 1))
            ],
            route_plan=route_plan,
        )
        climb_temperature_offset_c = _temperature_offset_from_model(
            wind_model=wind_model,
            departure_latitude=departure_latitude,
            departure_longitude=departure_longitude,
            destination_latitude=destination_latitude,
            destination_longitude=destination_longitude,
            mission_distance_nm=mission_distance_nm,
            altitude_ft=(departure_elevation_ft + cruise_altitude_ft) / 2.0,
            sample_distances_nm=[0.0],
            route_plan=route_plan,
        )
        cruise_sample = sample_cruise_performance(
            performance_profile,
            flight_level=flight_level,
            cruise_mode_id=cruise_mode_id,
            temperature_offset_c=cruise_temperature_offset_c,
            weight_lb=cruise_weight_lb,
        )
        if cruise_temperature_offset_c is not None and cruise_sample.temperature_offset_c != cruise_temperature_offset_c:
            performance_limit_notes.append(
                f"FL{flight_level} cruise ISA deviation {cruise_temperature_offset_c:+.0f}C "
                f"clamped to {cruise_sample.temperature_offset_c:+.0f}C table limit"
            )
        active_mode = next(
            mode for mode in performance_profile.cruise_modes if mode.mode_id == cruise_sample.mode_id
        )
        if cruise_weight_lb is not None and not (
            min(active_mode.available_weights_lb) <= cruise_weight_lb <= max(active_mode.available_weights_lb)
        ):
            performance_limit_notes.append(
                f"FL{flight_level} cruise weight {cruise_weight_lb:,.0f} lb clamped to published table limits"
            )
        active_climb_schedule = next(
            (
                schedule
                for schedule in performance_profile.climb_schedules
                if schedule.schedule_id == (climb_schedule_id or performance_profile.default_climb_schedule_id)
            ),
            performance_profile.climb_schedules[0],
        )
        if climb_temperature_offset_c is not None and not (
            min(active_climb_schedule.available_temperature_offsets_c)
            <= climb_temperature_offset_c
            <= max(active_climb_schedule.available_temperature_offsets_c)
        ):
            performance_limit_notes.append(
                f"FL{flight_level} climb ISA deviation {climb_temperature_offset_c:+.0f}C clamped to published table limits"
            )
        if climb_weight_lb is not None and not (
            min(active_climb_schedule.available_weights_lb)
            <= climb_weight_lb
            <= max(active_climb_schedule.available_weights_lb)
        ):
            performance_limit_notes.append(
                f"FL{flight_level} climb weight {climb_weight_lb:,.0f} lb clamped to published table limits"
            )
        active_cruise_tas_kts = float(cruise_sample.tas_kts)
        cruise_fuel_gph = float(cruise_sample.fuel_gph)
        fixed_fuel_gal = float(performance_profile.fixed_fuel_gal)
        if upper_climb_schedule_id and climb_transition_altitude_ft is not None:
            climb_rows = sample_composite_climb_rows(
                performance_profile,
                lower_schedule_id=climb_schedule_id or performance_profile.default_climb_schedule_id,
                upper_schedule_id=upper_climb_schedule_id,
                transition_altitude_ft=climb_transition_altitude_ft,
                temperature_offset_c=climb_temperature_offset_c,
                weight_lb=climb_weight_lb,
            )
        else:
            climb_rows = sample_climb_rows(
                performance_profile,
                climb_schedule_id=climb_schedule_id,
                temperature_offset_c=climb_temperature_offset_c,
                weight_lb=climb_weight_lb,
            )
        descent_rows = sample_descent_rows(
            performance_profile,
            descent_profile_id=descent_profile_id,
            vertical_rate_fpm=descent_profile_rate_fpm,
        )

    if fixed_fuel_gal_override is not None:
        fixed_fuel_gal = max(float(fixed_fuel_gal_override), 0.0)

    if wind_model is not None and (wind_model.station_profiles or wind_model.station_temperature_profiles):
        climb_hours, climb_distance_nm, climb_fuel_gal, climb_wind = _integrate_vertical_phase(
            lower_altitude_ft=departure_elevation_ft,
            upper_altitude_ft=cruise_altitude_ft,
            rows=climb_rows,
            fallback_ias_kts=int(climb_ias_kts),
            fallback_rate_fpm=int(climb_rate_fpm),
            fallback_fuel_gph=FUEL_BURN_GPH,
            default_tailwind_kts=default_climb_wind,
            default_crosswind_kts=default_climb_crosswind,
            departure_latitude=departure_latitude,
            departure_longitude=departure_longitude,
            destination_latitude=destination_latitude,
            destination_longitude=destination_longitude,
            mission_distance_nm=mission_distance_nm,
            track_deg=track_deg,
            wind_model=wind_model,
            route_plan=route_plan,
            prefer_nominal_ias_tas=False,
        )
        descent_hours, descent_distance_nm, descent_fuel_gal, descent_wind = _integrate_vertical_phase(
            lower_altitude_ft=destination_elevation_ft,
            upper_altitude_ft=cruise_altitude_ft,
            rows=descent_rows,
            fallback_ias_kts=int(descent_ias_kts),
            fallback_rate_fpm=int(descent_rate_fpm),
            fallback_fuel_gph=FUEL_BURN_GPH,
            default_tailwind_kts=default_descent_wind,
            default_crosswind_kts=default_descent_crosswind,
            departure_latitude=departure_latitude,
            departure_longitude=departure_longitude,
            destination_latitude=destination_latitude,
            destination_longitude=destination_longitude,
            mission_distance_nm=mission_distance_nm,
            track_deg=track_deg,
            wind_model=wind_model,
            route_plan=route_plan,
            integrate_from_destination=True,
            prefer_nominal_ias_tas=bool(descent_rows),
        )
    else:
        climb_hours, climb_distance_nm, climb_fuel_gal = _phase_performance(
            lower_altitude_ft=departure_elevation_ft,
            upper_altitude_ft=cruise_altitude_ft,
            rows=climb_rows,
            fallback_ias_kts=int(climb_ias_kts),
            fallback_rate_fpm=int(climb_rate_fpm),
            fallback_fuel_gph=FUEL_BURN_GPH,
            wind_kt=default_climb_wind,
            crosswind_kt=default_climb_crosswind,
            prefer_nominal_ias_tas=False,
        )
        descent_hours, descent_distance_nm, descent_fuel_gal = _phase_performance(
            lower_altitude_ft=destination_elevation_ft,
            upper_altitude_ft=cruise_altitude_ft,
            rows=descent_rows,
            fallback_ias_kts=int(descent_ias_kts),
            fallback_rate_fpm=int(descent_rate_fpm),
            fallback_fuel_gph=FUEL_BURN_GPH,
            wind_kt=default_descent_wind,
            crosswind_kt=default_descent_crosswind,
            prefer_nominal_ias_tas=bool(descent_rows),
        )
        climb_wind = default_climb_wind
        descent_wind = default_descent_wind
    remaining_distance_nm = mission_distance_nm - climb_distance_nm - descent_distance_nm

    if remaining_distance_nm < 0.0:
        transition_distance_nm = climb_distance_nm + descent_distance_nm
        if transition_distance_nm > 0.0:
            scale = mission_distance_nm / transition_distance_nm
            climb_hours *= scale
            descent_hours *= scale
            climb_distance_nm *= scale
            descent_distance_nm *= scale
            climb_fuel_gal *= scale
            descent_fuel_gal *= scale
        remaining_distance_nm = 0.0

    cruise_segment_hours = [0.0 for _ in range(segment_count)]
    segment_winds = list(default_segment_winds)
    total_hours = climb_hours + descent_hours
    cruise_fuel_gal = 0.0
    weighted_wind = (climb_wind * climb_hours) + (descent_wind * descent_hours)

    if remaining_distance_nm > 0.0 and segment_count > 0:
        distance_per_segment = remaining_distance_nm / segment_count
        cruise_start_distance_nm = climb_distance_nm
        for segment_idx in range(segment_count):
            current_wind = default_segment_winds[segment_idx]
            current_crosswind = default_segment_crosswinds[segment_idx]
            if wind_model is not None and wind_model.station_profiles:
                sample_distance_nm = cruise_start_distance_nm + ((segment_idx + 0.5) * distance_per_segment)
                sample_latitude, sample_longitude = _route_point_at_distance_nm(
                    departure_latitude=departure_latitude,
                    departure_longitude=departure_longitude,
                    destination_latitude=destination_latitude,
                    destination_longitude=destination_longitude,
                    mission_distance_nm=mission_distance_nm,
                    distance_from_departure_nm=sample_distance_nm,
                    route_plan=route_plan,
                )
                sample_track_deg = _route_track_at_distance_nm(
                    departure_latitude=departure_latitude,
                    departure_longitude=departure_longitude,
                    destination_latitude=destination_latitude,
                    destination_longitude=destination_longitude,
                    distance_from_departure_nm=sample_distance_nm,
                    route_plan=route_plan,
                )
                sampled_components = _sample_wind_components_from_model(
                    wind_model=wind_model,
                    sample_latitude=sample_latitude,
                    sample_longitude=sample_longitude,
                    altitude_ft=cruise_altitude_ft,
                    track_deg=sample_track_deg if sample_track_deg is not None else track_deg,
                )
                if sampled_components is not None:
                    current_wind, current_crosswind = sampled_components
            segment_winds[segment_idx] = current_wind
            ground_speed = _along_track_ground_speed(
                true_airspeed_kts=active_cruise_tas_kts,
                tailwind_kts=current_wind,
                crosswind_kts=current_crosswind,
            )
            segment_hours = distance_per_segment / ground_speed
            cruise_segment_hours[segment_idx] = segment_hours
            total_hours += segment_hours
            cruise_fuel_gal += segment_hours * cruise_fuel_gph
            weighted_wind += current_wind * segment_hours

    avg_wind = int(weighted_wind / total_hours) if total_hours > 0 else 0
    total_fuel_gal = climb_fuel_gal + descent_fuel_gal + cruise_fuel_gal + fixed_fuel_gal
    return FlightLevelProfile(
        climb_hours=climb_hours,
        descent_hours=descent_hours,
        climb_distance_nm=climb_distance_nm,
        descent_distance_nm=descent_distance_nm,
        remaining_distance_nm=remaining_distance_nm,
        cruise_segment_hours=cruise_segment_hours,
        climb_fuel_gal=climb_fuel_gal,
        descent_fuel_gal=descent_fuel_gal,
        cruise_fuel_gal=cruise_fuel_gal,
        total_fuel_gal=total_fuel_gal,
        total_hours=total_hours,
        avg_wind=avg_wind,
        performance_limit_notes=tuple(performance_limit_notes),
    )


def _flight_level_point(
    *,
    flight_level: int,
    is_return: bool,
    mission_distance_nm: float,
    departure_latitude: float,
    departure_longitude: float,
    destination_latitude: float,
    destination_longitude: float,
    departure_dt: dt.datetime,
    destination_tz: pytz.BaseTzInfo,
    start_fuel_gal: int,
    departure_elevation_ft: float,
    destination_elevation_ft: float,
    climb_rate_fpm: int,
    descent_rate_fpm: int,
    cruise_tas_kts: int,
    climb_ias_kts: int,
    descent_ias_kts: int,
    performance_profile: AircraftPerformanceProfile | None = None,
    cruise_mode_id: str | None = None,
    climb_schedule_id: str | None = None,
    upper_climb_schedule_id: str | None = None,
    climb_transition_altitude_ft: float | None = None,
    descent_profile_id: str | None = None,
    descent_profile_rate_fpm: int | None = None,
    fixed_fuel_gal_override: float | None = None,
    cruise_weight_lb: float | None = None,
    climb_weight_lb: float | None = None,
    alternate_distance_nm: float = 0.0,
    reserve_minutes: float = 45.0,
    landing_minimum_gal: float = 0.0,
    reserve_floor_gal: float | None = None,
    wind_model: RouteWindModel | None = None,
    route_plan: RoutePlan | None = None,
    segment_count: int = SEGMENTS,
) -> tuple[MissionPoint, int]:
    """Convert the integrated profile into the brief row shown to the user."""

    profile = _flight_level_profile(
        flight_level=flight_level,
        is_return=is_return,
        mission_distance_nm=mission_distance_nm,
        departure_latitude=departure_latitude,
        departure_longitude=departure_longitude,
        destination_latitude=destination_latitude,
        destination_longitude=destination_longitude,
        departure_elevation_ft=departure_elevation_ft,
        destination_elevation_ft=destination_elevation_ft,
        climb_rate_fpm=climb_rate_fpm,
        descent_rate_fpm=descent_rate_fpm,
        cruise_tas_kts=cruise_tas_kts,
        climb_ias_kts=climb_ias_kts,
        descent_ias_kts=descent_ias_kts,
        performance_profile=performance_profile,
        cruise_mode_id=cruise_mode_id,
        climb_schedule_id=climb_schedule_id,
        upper_climb_schedule_id=upper_climb_schedule_id,
        climb_transition_altitude_ft=climb_transition_altitude_ft,
        descent_profile_id=descent_profile_id,
        descent_profile_rate_fpm=descent_profile_rate_fpm,
        cruise_weight_lb=cruise_weight_lb,
        climb_weight_lb=climb_weight_lb,
        fixed_fuel_gal_override=fixed_fuel_gal_override,
        wind_model=wind_model,
        route_plan=route_plan,
        segment_count=segment_count,
    )
    cruise_hours = sum(profile.cruise_segment_hours)
    cruise_fuel_gph = profile.cruise_fuel_gal / cruise_hours if cruise_hours > 0.0 else FUEL_BURN_GPH
    # An alternate can lie in any direction from the destination. Reusing the
    # enroute groundspeed would silently apply the mission's head/tailwind to a
    # different course, so calculate this planning allowance with still-air TAS.
    if performance_profile is not None:
        alternate_performance = sample_cruise_performance(
            performance_profile,
            flight_level=ALTERNATE_DIVERSION_FLIGHT_LEVEL,
            cruise_mode_id=cruise_mode_id,
            weight_lb=cruise_weight_lb,
        )
        alternate_cruise_speed_kts = alternate_performance.tas_kts
        alternate_fuel_gph = alternate_performance.fuel_gph
    else:
        alternate_cruise_speed_kts = max(float(cruise_tas_kts), 120.0)
        alternate_fuel_gph = cruise_fuel_gph
    # Post-destination planning follows the IFR reserve shape: alternate leg, then final reserve.
    # The operator landing minimum is a destination-arrival floor that may already
    # cover those components, so compare the requirements instead of adding twice.
    bounded_alternate_distance_nm = max(float(alternate_distance_nm), 0.0)
    bounded_reserve_minutes = max(float(reserve_minutes), 0.0)
    alternate_fuel_gal = int(
        math.ceil((bounded_alternate_distance_nm / max(alternate_cruise_speed_kts, 120.0)) * alternate_fuel_gph)
    )
    reserve_fuel_gal = int(math.ceil((bounded_reserve_minutes / 60.0) * cruise_fuel_gph))
    # One ledger derives every displayed fuel figure; the MissionPoint fields below
    # are projections of it, never independent arithmetic.
    ledger = build_fuel_ledger(
        start_fuel_gal=int(start_fuel_gal),
        taxi_fuel_gal=max(
            profile.total_fuel_gal
            - profile.climb_fuel_gal
            - profile.cruise_fuel_gal
            - profile.descent_fuel_gal,
            0.0,
        ),
        climb_fuel_gal=profile.climb_fuel_gal,
        cruise_fuel_gal=profile.cruise_fuel_gal,
        descent_fuel_gal=profile.descent_fuel_gal,
        alternate_fuel_gal=alternate_fuel_gal,
        reserve_fuel_gal=reserve_fuel_gal,
        landing_minimum_gal=int(math.ceil(max(float(landing_minimum_gal), 0.0))),
        pilot_floor_gal=(
            int(math.ceil(max(float(reserve_floor_gal), 0.0)))
            if reserve_floor_gal is not None
            else 0
        ),
    )
    eta_dt = departure_dt + dt.timedelta(hours=profile.total_hours)
    eta_departure_zone = _format_time_12h(eta_dt.astimezone(departure_dt.tzinfo))
    eta_arrival_zone = _format_time_12h(eta_dt.astimezone(destination_tz))

    displayed_ete_minutes = int(math.ceil((profile.total_hours * 60.0) - 1e-9))
    point = MissionPoint(
        flight_level=f"FL{flight_level}",
        wind_knots=f"{profile.avg_wind}k",
        ete=f"{displayed_ete_minutes // 60}h {displayed_ete_minutes % 60}m",
        eta_arrival_zone=eta_arrival_zone,
        eta_departure_zone=eta_departure_zone,
        fuel_burn=ledger.total_burn_gal,
        fuel_at_dest=ledger.fob_at_landing_gal,
        airborne_hours=profile.total_hours,
        alternate_fuel_gal=ledger.alternate_fuel_gal,
        reserve_fuel_gal=ledger.reserve_fuel_gal,
        calculated_required_landing_fuel_gal=ledger.alternate_plus_reserve_gal,
        reserve_floor_gal=ledger.pilot_floor_gal,
        required_landing_fuel_gal=ledger.effective_requirement_gal,
        reserve_margin_gal=ledger.reserve_margin_gal,
        fuel_status=ledger.fuel_status,
        performance_limit_notes=profile.performance_limit_notes,
        fuel_ledger=ledger,
    )
    return point, profile.avg_wind


def build_mission_brief(
    departure: AirportData,
    destination: AirportData,
    *,
    departure_date: dt.date,
    departure_time_local: dt.time,
    is_return_leg: bool = False,
    start_fuel_gal: int,
    climb_rate_fpm: int = 2200,
    descent_rate_fpm: int = 1500,
    cruise_tas_kts: int = 315,
    climb_ias_kts: int = 124,
    descent_ias_kts: int = 220,
    performance_profile: AircraftPerformanceProfile | None = None,
    cruise_mode_id: str | None = None,
    climb_schedule_id: str | None = None,
    upper_climb_schedule_id: str | None = None,
    climb_transition_altitude_ft: float | None = None,
    descent_profile_id: str | None = None,
    descent_profile_rate_fpm: int | None = None,
    fixed_fuel_gal_override: float | None = None,
    cruise_weight_lb: float | None = None,
    climb_weight_lb: float | None = None,
    alternate_distance_nm: float = 0.0,
    reserve_minutes: float = 45.0,
    landing_minimum_gal: float = 0.0,
    reserve_floor_gal: float | None = None,
    wind_model: RouteWindModel | None = None,
    flight_levels: list[int] | None = None,
    route_plan: RoutePlan | None = None,
    derive_direction: bool = False,
) -> MissionBrief:
    """Assemble the mission brief table for the requested route and ETD.

    With derive_direction=True the westbound/eastbound convention is owned here:
    callers pass (departure, destination) as flown and is_return_leg is ignored.
    """

    if derive_direction:
        # The caller's order is as-flown; is_return_leg only drives labels/parity.
        is_return_leg = is_westbound_route(departure, destination)
        route_from, route_to = departure, destination
    else:
        # Legacy convention: westbound callers pre-swap endpoints and set the flag,
        # which this internal swap undoes.
        route_from = destination if is_return_leg else departure
        route_to = departure if is_return_leg else destination

    active_departure_tz = pytz.timezone(route_from.timezone)
    destination_tz = pytz.timezone(route_to.timezone)

    departure_dt = active_departure_tz.localize(
        dt.datetime.combine(departure_date, departure_time_local)
    )
    distance_nm = (
        route_plan.total_distance_nm
        if route_plan is not None
        else great_circle_distance_nm(
            departure.latitude,
            departure.longitude,
            destination.latitude,
            destination.longitude,
        )
    )
    mission_segment_count = max(SEGMENTS, int(math.ceil(distance_nm / CRUISE_BIN_DISTANCE_NM)))

    points: list[MissionPoint] = []
    numeric_average_winds_kts: list[float] = []
    active_flight_levels = list(flight_levels or FLIGHT_LEVELS)

    for fl in active_flight_levels:
        point, avg_wind = _flight_level_point(
            flight_level=fl,
            is_return=is_return_leg,
            mission_distance_nm=distance_nm,
            departure_latitude=route_from.latitude,
            departure_longitude=route_from.longitude,
            destination_latitude=route_to.latitude,
            destination_longitude=route_to.longitude,
            departure_dt=departure_dt,
            destination_tz=destination_tz,
            start_fuel_gal=start_fuel_gal,
            departure_elevation_ft=route_from.elevation_ft,
            destination_elevation_ft=route_to.elevation_ft,
            climb_rate_fpm=climb_rate_fpm,
            descent_rate_fpm=descent_rate_fpm,
            cruise_tas_kts=cruise_tas_kts,
            climb_ias_kts=climb_ias_kts,
            descent_ias_kts=descent_ias_kts,
            performance_profile=performance_profile,
            cruise_mode_id=cruise_mode_id,
            climb_schedule_id=climb_schedule_id,
            upper_climb_schedule_id=upper_climb_schedule_id,
            climb_transition_altitude_ft=climb_transition_altitude_ft,
            descent_profile_id=descent_profile_id,
            descent_profile_rate_fpm=descent_profile_rate_fpm,
            cruise_weight_lb=cruise_weight_lb,
            climb_weight_lb=climb_weight_lb,
            fixed_fuel_gal_override=fixed_fuel_gal_override,
            alternate_distance_nm=alternate_distance_nm,
            reserve_minutes=reserve_minutes,
            landing_minimum_gal=landing_minimum_gal,
            reserve_floor_gal=reserve_floor_gal,
            wind_model=wind_model,
            route_plan=route_plan,
            segment_count=mission_segment_count,
        )
        points.append(point)
        numeric_average_winds_kts.append(float(avg_wind))

    # The parenthetical must reflect the computed winds, not the route direction:
    # easterly winds aloft make an eastbound leg a headwind mission.
    average_mission_wind_kts = (
        sum(numeric_average_winds_kts) / len(numeric_average_winds_kts)
        if numeric_average_winds_kts
        else 0.0
    )
    if average_mission_wind_kts > 0:
        wind_type = "Tailwind"
    elif average_mission_wind_kts < 0:
        wind_type = "Headwind"
    else:
        wind_type = "Calm"
    direction = "Westbound" if is_return_leg else "Eastbound"

    return MissionBrief(
        route_label=route_plan.route_label if route_plan is not None else f"{route_from.icao} -> {route_to.icao}",
        departure_zone_time=_format_time_12h(departure_dt),
        distance_nm=int(distance_nm),
        direction_label=f"{direction} ({wind_type})",
        points=points,
    )


@dataclass(frozen=True)
class MissionLegPlan:
    """One fully computed chained leg of a fuel-stop mission."""

    leg_number: int
    is_final_leg: bool
    start_identifier: str
    end_identifier: str
    departure_airport: AirportData
    destination_airport: AirportData
    departure_utc: dt.datetime
    arrival_utc: dt.datetime
    start_fuel_gal: float
    brief: MissionBrief
    point: MissionPoint
    alternate_distance_nm: float
    alternate_route_label: str
    uplift_gal: float | None
    next_start_fuel_gal: float
    has_approach_confirmed: bool
    legal_alternate: LegalAlternateAssessment
    forecast_quality: ForecastQualityCheck | None
    fuel_stop_rings: tuple[AlternateRangeRing, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class MultiLegPlan:
    """The complete chained fuel-stop mission the UI renders without arithmetic."""

    legs: tuple[MissionLegPlan, ...]
    final_arrival_utc: dt.datetime | None
    leg_reserve_margins_gal: tuple[tuple[str, int], ...]
    leg_arrival_fuels_gal: tuple[float, ...]


def route_waypoint_airport_data(
    waypoint: RouteWaypoint,
    *,
    fallback_timezone: str,
) -> AirportData:
    """Build AirportData for route-leg math while preserving FAA coordinates."""

    try:
        airport = get_airport_data(waypoint.identifier) if waypoint.waypoint_type == "Airport" else None
    except ValueError:
        airport = None
    timezone = airport.timezone if airport else fallback_timezone
    elevation_ft = airport.elevation_ft if airport else 0.0
    source = airport.source if airport else waypoint.source
    return AirportData(
        icao=waypoint.identifier,
        latitude=waypoint.latitude,
        longitude=waypoint.longitude,
        timezone=timezone,
        source=source,
        elevation_ft=elevation_ft,
    )


def build_multi_leg_plan(
    *,
    fuel_stop_segments: tuple[RouteFuelSegment, ...],
    departure_dt: dt.datetime,
    start_fuel_gal: float,
    ground_minutes: float,
    uplifts: dict[str, float],
    alternates: dict[str, str],
    mission_alternate_code: str | None,
    mission_alternate_distance_nm: float,
    mission_alternate_route_label: str,
    approach_confirmed_icaos: set[str],
    destination_has_approach: bool,
    departure_fallback_timezone: str,
    destination_fallback_timezone: str,
    weather: NoaaWeather,
    usable_fuel_capacity_gal: float | None,
    focus_flight_level: int,
    mission_brief_kwargs: dict[str, object],
    stop_ring_kwargs: dict[str, object] | None = None,
) -> MultiLegPlan:
    """Compute every chained fuel-stop leg: fuel handoff, timing, alternates, and rings.

    All mission arithmetic lives here so the UI layer only formats and renders.
    mission_brief_kwargs carries the leg-invariant performance/reserve settings;
    per-leg values (dates, fuel, wind model, alternate distance, route plan) are
    supplied by this engine. stop_ring_kwargs enables post-missed range rings at
    intermediate stops; ring failures degrade to a warning rather than aborting.
    """

    legs: list[MissionLegPlan] = []
    leg_margins: list[tuple[str, int]] = []
    leg_arrival_fuels: list[float] = []
    final_arrival_utc: dt.datetime | None = None
    current_departure_dt = departure_dt
    current_start_fuel = float(start_fuel_gal)
    total_legs = len(fuel_stop_segments)
    for leg_number, fuel_segment in enumerate(fuel_stop_segments, start=1):
        is_final_leg = leg_number == total_legs
        warnings: list[str] = []
        leg_departure = route_waypoint_airport_data(
            fuel_segment.route_plan.waypoints[0],
            fallback_timezone=departure_fallback_timezone,
        )
        leg_destination = route_waypoint_airport_data(
            fuel_segment.route_plan.waypoints[-1],
            fallback_timezone=destination_fallback_timezone,
        )
        departure_local = current_departure_dt.astimezone(pytz.timezone(leg_departure.timezone))

        # First policy pass resolves only the alternate choice (landing fuel is a
        # placeholder); it is re-run after the leg brief with real landing fuel to
        # compute the fuel handoff. Do not merge the two calls.
        alternate_policy = resolve_fuel_stop_leg_policy(
            destination_identifier=leg_destination.icao,
            is_final_leg=is_final_leg,
            landing_fuel_gal=0.0,
            default_start_fuel_gal=float(start_fuel_gal),
            uplifts=uplifts,
            alternates=alternates,
            mission_alternate_code=mission_alternate_code,
        )
        alternate_distance_nm = 0.0
        alternate_route_label = ""
        if alternate_policy.alternate_is_explicit and alternate_policy.alternate_code:
            try:
                leg_alternate = get_airport_data(alternate_policy.alternate_code)
            except ValueError:
                leg_alternate = None
            if leg_alternate is not None:
                alternate_route = _route_plan_between_airports(leg_destination, leg_alternate)
                alternate_distance_nm = float(alternate_route.total_distance_nm)
                alternate_route_label = f"{leg_destination.icao} -> {leg_alternate.icao}"
            else:
                alternate_route_label = (
                    f"{alternate_policy.alternate_code} unresolved — alternate fuel excluded"
                )
                warnings.append(
                    f"Leg {leg_number} alternate {alternate_policy.alternate_code} could not be "
                    "resolved; alternate fuel is excluded from that leg. Correct the identifier "
                    "before relying on the plan."
                )
        elif is_final_leg:
            alternate_distance_nm = float(mission_alternate_distance_nm)
            alternate_route_label = mission_alternate_route_label
        else:
            alternate_route_label = "Not specified — alternate fuel excluded"

        try:
            leg_brief = build_mission_brief(
                leg_departure,
                leg_destination,
                departure_date=departure_local.date(),
                departure_time_local=departure_local.time().replace(second=0, microsecond=0, tzinfo=None),
                is_return_leg=False,
                start_fuel_gal=int(round(current_start_fuel)),
                alternate_distance_nm=alternate_distance_nm,
                # Per-leg model: reusing the full-route model would hand this leg's
                # uncovered bins another geography's precomputed winds.
                wind_model=build_route_wind_model(
                    leg_departure,
                    leg_destination,
                    weather.windtemps,
                    route_plan=fuel_segment.route_plan,
                ),
                flight_levels=[focus_flight_level],
                route_plan=fuel_segment.route_plan,
                **mission_brief_kwargs,
            )
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError(f"Leg {leg_number} mission calculations failed: {exc}") from exc
        leg_point = leg_brief.points[0]
        leg_airborne_hours = float(leg_point.airborne_hours)
        if not math.isfinite(leg_airborne_hours) or leg_airborne_hours < 0.0:
            raise ValueError(
                f"Leg {leg_number} timing could not be chained: airborne hours {leg_airborne_hours!r}"
            )
        leg_arrival_dt = current_departure_dt + dt.timedelta(hours=leg_airborne_hours)
        arrival_utc = leg_arrival_dt.astimezone(dt.timezone.utc)
        if is_final_leg:
            final_arrival_utc = arrival_utc

        has_approach = (
            bool(destination_has_approach)
            if is_final_leg
            else leg_destination.icao in approach_confirmed_icaos
        )
        legal_alternate = evaluate_legal_alternate_requirement(
            weather=weather,
            destination_icao=leg_destination.icao,
            eta_utc=arrival_utc,
            has_destination_approach=has_approach,
        )
        quality_checks = evaluate_terminal_forecast_quality(
            weather=weather,
            phase_airports={f"Leg {leg_number} arrival": leg_destination.icao},
        )

        fuel_stop_rings: tuple[AlternateRangeRing, ...] = ()
        if not is_final_leg and stop_ring_kwargs is not None:
            try:
                fuel_stop_rings = build_alternate_range_rings(
                    destination=leg_destination,
                    fuel_at_destination_gal=float(leg_point.fuel_at_dest),
                    **stop_ring_kwargs,
                )
            except (ValueError, TypeError, KeyError) as exc:
                warnings.append(
                    f"Range rings for {leg_destination.icao} could not be calculated: {exc}"
                )

        handoff_policy = resolve_fuel_stop_leg_policy(
            destination_identifier=leg_destination.icao,
            is_final_leg=is_final_leg,
            landing_fuel_gal=float(leg_point.fuel_at_dest),
            default_start_fuel_gal=float(start_fuel_gal),
            uplifts=uplifts,
            alternates=alternates,
            mission_alternate_code=mission_alternate_code,
            usable_fuel_capacity_gal=usable_fuel_capacity_gal,
        )
        if handoff_policy.uplift_trimmed_gal > 0 and usable_fuel_capacity_gal is not None:
            warnings.append(
                f"Leg {leg_number} uplift at {leg_destination.icao} exceeds the "
                f"{int(usable_fuel_capacity_gal)} gal usable capacity; next start fuel trimmed by "
                f"{handoff_policy.uplift_trimmed_gal:.0f} gal to tank capacity."
            )

        legs.append(
            MissionLegPlan(
                leg_number=leg_number,
                is_final_leg=is_final_leg,
                start_identifier=fuel_segment.start_identifier,
                end_identifier=fuel_segment.end_identifier,
                departure_airport=leg_departure,
                destination_airport=leg_destination,
                departure_utc=current_departure_dt.astimezone(dt.timezone.utc),
                arrival_utc=arrival_utc,
                start_fuel_gal=current_start_fuel,
                brief=leg_brief,
                point=leg_point,
                alternate_distance_nm=alternate_distance_nm,
                alternate_route_label=alternate_route_label,
                uplift_gal=handoff_policy.uplift_gal,
                next_start_fuel_gal=handoff_policy.next_start_fuel_gal,
                has_approach_confirmed=has_approach,
                legal_alternate=legal_alternate,
                forecast_quality=quality_checks[0] if quality_checks else None,
                fuel_stop_rings=fuel_stop_rings,
                warnings=tuple(warnings),
            )
        )
        leg_margins.append((f"Leg {leg_number}", int(leg_point.reserve_margin_gal)))
        leg_arrival_fuels.append(float(leg_point.fuel_at_dest))
        current_departure_dt = leg_arrival_dt + dt.timedelta(minutes=float(ground_minutes))
        current_start_fuel = handoff_policy.next_start_fuel_gal

    return MultiLegPlan(
        legs=tuple(legs),
        final_arrival_utc=final_arrival_utc,
        leg_reserve_margins_gal=tuple(leg_margins),
        leg_arrival_fuels_gal=tuple(leg_arrival_fuels),
    )


@dataclass(frozen=True)
class MissionBriefDocument:
    """One immutable computed mission: every number the UI renders, no UI arithmetic.

    The document is assembled by build_mission_brief_document in a single pass so
    rendering surfaces cannot recompute or disagree with each other.
    """

    wind_model: RouteWindModel | None
    brief: MissionBrief
    route_hazards_by_fl: dict[int, list[SegmentHazard]]
    focus_flight_level: int
    focus_point: MissionPoint | None
    nonstop_focus_eta_utc: dt.datetime | None
    fuel_stop_segments: tuple[RouteFuelSegment, ...]
    multi_leg_plan: MultiLegPlan | None
    mission_arrival_eta_utc: dt.datetime | None
    mission_headline: "MissionHeadline"
    legal_alternate: LegalAlternateAssessment
    forecast_quality_checks: tuple[ForecastQualityCheck, ...]
    risk_summary: MissionRiskSummary


def build_mission_brief_document(
    *,
    departure: AirportData,
    destination: AirportData,
    weather: NoaaWeather,
    route_plan: RoutePlan | None,
    departure_dt: dt.datetime,
    departure_date: dt.date,
    departure_time_local: dt.time,
    start_fuel_gal: float,
    flight_levels: list[int],
    selected_flight_level: int | None,
    preview_flight_level: int,
    ground_minutes: float,
    uplifts: dict[str, float],
    alternates: dict[str, str],
    mission_alternate_code: str | None,
    mission_alternate_distance_nm: float,
    mission_alternate_route_label: str,
    approach_confirmed_icaos: set[str],
    destination_has_approach: bool,
    forecast_phase_airports: dict[str, str],
    usable_fuel_capacity_gal: float | None,
    thresholds: MissionRiskThresholds | None,
    mission_brief_kwargs: dict[str, object],
    stop_ring_kwargs: dict[str, object] | None = None,
) -> MissionBriefDocument:
    """Assemble the complete mission document: winds, brief, hazards, legs, and risk.

    mission_brief_kwargs carries the performance/reserve settings shared by the
    nonstop brief, the hazard evaluation (which takes the applicable subset), and
    every chained leg.
    """

    wind_model = build_route_wind_model(
        departure,
        destination,
        weather.windtemps,
        route_plan=route_plan,
    )
    if stop_ring_kwargs is not None and stop_ring_kwargs.get("wind_model") is None:
        # Stop rings sample the mission-wide wind field, which only exists here.
        stop_ring_kwargs = {**stop_ring_kwargs, "wind_model": wind_model}
    brief = build_mission_brief(
        departure,
        destination,
        departure_date=departure_date,
        departure_time_local=departure_time_local,
        derive_direction=True,
        start_fuel_gal=int(start_fuel_gal),
        alternate_distance_nm=float(mission_alternate_distance_nm),
        wind_model=wind_model,
        flight_levels=flight_levels,
        route_plan=route_plan,
        **mission_brief_kwargs,
    )
    hazard_parameter_names = set(inspect.signature(evaluate_route_hazards).parameters)
    route_hazards_by_fl = evaluate_route_hazards(
        departure,
        destination,
        hazard_areas=weather.hazard_areas,
        reference_time_utc=departure_dt.astimezone(dt.timezone.utc),
        flight_levels=flight_levels,
        wind_model=wind_model,
        route_plan=route_plan,
        **{
            key: value
            for key, value in mission_brief_kwargs.items()
            if key in hazard_parameter_names
        },
    )

    focus_flight_level = (
        selected_flight_level if selected_flight_level in flight_levels else preview_flight_level
    )
    focus_point = next(
        (point for point in brief.points if point.flight_level == f"FL{focus_flight_level}"),
        None,
    )
    if focus_point is None and brief.points:
        focus_point = brief.points[0]
        parsed_level = str(focus_point.flight_level).replace("FL", "")
        focus_flight_level = int(parsed_level) if parsed_level.isdigit() else flight_levels[0]

    nonstop_focus_eta_utc = (
        (departure_dt + dt.timedelta(hours=float(focus_point.airborne_hours))).astimezone(dt.timezone.utc)
        if focus_point is not None
        else None
    )
    mission_arrival_eta_utc = nonstop_focus_eta_utc

    fuel_stop_segments: tuple[RouteFuelSegment, ...] = (
        split_route_plan_at_fuel_stops(route_plan)
        if route_plan is not None
        and any(getattr(waypoint, "is_fuel_stop", False) for waypoint in route_plan.waypoints)
        else ()
    )
    multi_leg_plan: MultiLegPlan | None = None
    if fuel_stop_segments:
        multi_leg_plan = build_multi_leg_plan(
            fuel_stop_segments=fuel_stop_segments,
            departure_dt=departure_dt,
            start_fuel_gal=float(start_fuel_gal),
            ground_minutes=float(ground_minutes),
            uplifts=uplifts,
            alternates=alternates,
            mission_alternate_code=mission_alternate_code,
            mission_alternate_distance_nm=float(mission_alternate_distance_nm),
            mission_alternate_route_label=mission_alternate_route_label,
            approach_confirmed_icaos=approach_confirmed_icaos,
            destination_has_approach=destination_has_approach,
            departure_fallback_timezone=departure.timezone,
            destination_fallback_timezone=destination.timezone,
            weather=weather,
            usable_fuel_capacity_gal=usable_fuel_capacity_gal,
            focus_flight_level=focus_flight_level,
            mission_brief_kwargs=mission_brief_kwargs,
            stop_ring_kwargs=stop_ring_kwargs,
        )
        mission_arrival_eta_utc = multi_leg_plan.final_arrival_utc

    legal_alternate = evaluate_legal_alternate_requirement(
        weather=weather,
        destination_icao=destination.icao,
        eta_utc=mission_arrival_eta_utc,
        has_destination_approach=destination_has_approach,
    )
    forecast_quality_checks = tuple(
        evaluate_terminal_forecast_quality(
            weather=weather,
            phase_airports=forecast_phase_airports,
        )
    )

    mission_headline = resolve_mission_headline(
        nonstop_reserve_margin_gal=int(focus_point.reserve_margin_gal) if focus_point else 0,
        nonstop_fob_at_landing_gal=int(focus_point.fuel_at_dest) if focus_point else 0,
        leg_reserve_margins_gal=(
            list(multi_leg_plan.leg_reserve_margins_gal) if multi_leg_plan is not None else []
        ),
        leg_arrival_fuels_gal=(
            list(multi_leg_plan.leg_arrival_fuels_gal) if multi_leg_plan is not None else []
        ),
    )
    risk_summary = build_mission_risk_summary(
        weather=weather,
        segment_hazards=route_hazards_by_fl.get(focus_flight_level, []),
        mission_point=focus_point,
        thresholds=thresholds,
        reserve_margin_override_gal=(
            mission_headline.reserve_margin_gal if mission_headline.basis == "multi-leg" else None
        ),
        reserve_margin_context=mission_headline.margin_leg_label,
    )

    return MissionBriefDocument(
        wind_model=wind_model,
        brief=brief,
        route_hazards_by_fl=route_hazards_by_fl,
        focus_flight_level=focus_flight_level,
        focus_point=focus_point,
        nonstop_focus_eta_utc=nonstop_focus_eta_utc,
        fuel_stop_segments=fuel_stop_segments,
        multi_leg_plan=multi_leg_plan,
        mission_arrival_eta_utc=mission_arrival_eta_utc,
        mission_headline=mission_headline,
        legal_alternate=legal_alternate,
        forecast_quality_checks=forecast_quality_checks,
        risk_summary=risk_summary,
    )
