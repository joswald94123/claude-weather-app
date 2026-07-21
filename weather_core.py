"""Core weather, hazard, and mission-calculation logic for the TBM 960 brief."""

from __future__ import annotations

import datetime as dt
import math
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from functools import lru_cache
from typing import Callable

import pytz
import requests

from performance_profiles import (
    AircraftPerformanceProfile,
    VerticalPerformanceRow,
    sample_climb_rows,
    sample_composite_climb_rows,
    sample_cruise_performance,
    sample_descent_rows,
)
from route_planning import (
    RoutePlan,
    great_circle_distance_nm as _route_great_circle_distance_nm,
    initial_track_deg as _route_initial_track_deg,
    route_midpoint_lat_lon,
    route_point_at_distance_nm as _route_plan_point_at_distance_nm,
    route_track_at_distance_nm as _route_plan_track_at_distance_nm,
)

DEFAULT_LAT = 39.8283
DEFAULT_LON = -98.5795
DEFAULT_TZ = "US/Central"

FLIGHT_LEVELS = [190, 200, 210, 220, 230, 240, 250, 260, 270, 280, 290, 300, 310]
EASTBOUND_CRUISE_LEVELS = [190, 210, 230, 250, 270, 290, 310]
WESTBOUND_CRUISE_LEVELS = [200, 220, 240, 260, 280, 300]
SEGMENTS = 12
MAX_WIND_STATION_DISTANCE_NM = 300.0
MULTI_REGION_ROUTE_DISTANCE_NM = 500.0
CRUISE_BIN_DISTANCE_NM = 75.0
GAIRMET_SNAPSHOT_HALF_WINDOW = dt.timedelta(hours=1, minutes=30)
GAIRMET_HORIZON_FALLBACK_LIMIT = dt.timedelta(hours=6)
TCF_VALIDITY_HALF_WINDOW = dt.timedelta(hours=3)
PIREP_ALTITUDE_HALF_BAND_FT = 3000
PIREP_VALID_BEFORE = dt.timedelta(hours=1)
PIREP_VALID_AFTER = dt.timedelta(hours=2)
PIREP_BBOX_PADDING_SINGLE_DEG = 2.5
PIREP_BBOX_PADDING_MULTI_DEG = 1.5
ALTITUDE_BAND_OVERLAP_TOLERANCE_FT = 500
DEFAULT_HAZARD_TOP_FT = 60000
WIND_COVERAGE_PROBE_ALTITUDE_FT = 30000.0
IDW_DISTANCE_SOFTENING_NM = 20.0
IDW_MAX_STATIONS = 4
ALTERNATE_DIVERSION_FLIGHT_LEVEL = 100
TRUE_AIRSPEED_KTS = 330.0
FUEL_BURN_GPH = 57.0
FIXED_FUEL_GAL = 8
CLIMB_DESCENT_AIRSPEED_KTS = 220.0
FEET_PER_NAUTICAL_MILE = 6076.12
NOAA_API_BASE_URL = "https://aviationweather.gov/api/data"
WINDTEMP_TOKEN_PATTERN = re.compile(r"^(?P<dd>\d{2})(?P<ff>\d{2})(?P<tt>[+-]\d{2}|\d{2})?$")
WINDTEMP_GROUP_PATTERN = re.compile(r"/{4,7}|\d{4}(?:[+-]\d{2}|\d{2})?")
RISK_LABEL_BY_SCORE = {0: "None", 1: "Low", 2: "Moderate", 3: "High"}
MISSION_RISK_LABEL_BY_SCORE = {0: "Clear", 1: "Monitor", 2: "Caution", 3: "High"}


# Data containers are shared across the mission engine, Streamlit UI, and tests.
@dataclass(frozen=True)
class AirportData:
    """Normalized airport metadata used throughout the planning pipeline."""

    icao: str
    latitude: float
    longitude: float
    timezone: str
    source: str
    elevation_ft: float = 0.0


@dataclass(frozen=True)
class MissionPoint:
    """Calculated fuel, time, and reserve outcome for one candidate flight level."""

    flight_level: str
    wind_knots: str
    ete: str
    eta_arrival_zone: str
    eta_departure_zone: str
    fuel_burn: int
    fuel_at_dest: int
    airborne_hours: float = 0.0
    alternate_fuel_gal: int = 0
    reserve_fuel_gal: int = 0
    calculated_required_landing_fuel_gal: int = 0
    reserve_floor_gal: int = 0
    required_landing_fuel_gal: int = 0
    reserve_margin_gal: int = 0
    fuel_status: str = "Unknown"
    performance_limit_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MissionBrief:
    """User-facing mission summary for a route and a set of flight levels."""

    route_label: str
    departure_zone_time: str
    distance_nm: int
    direction_label: str
    baseline_wind_knots: int
    points: list[MissionPoint]


@dataclass(frozen=True)
class MissionRiskSummary:
    """Composite mission posture that keeps severity and confidence separate."""

    score: int
    label: str
    confidence: str
    reasons: tuple[str, ...] = ()
    confidence_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MissionRiskThresholds:
    """Pilot-adjustable thresholds for relative mission-risk interpretation."""

    fuel_high_margin_gal: int = 0
    fuel_caution_margin_gal: int = 15
    route_caution_fraction: float = 0.25
    route_high_fraction: float = 0.50


@dataclass(frozen=True)
class TerminalRisk:
    """Terminal weather risk derived from METAR or TAF fields."""

    source: str
    score: int
    label: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class TerminalForecastPeriod:
    """Decoded TAF period fields needed for legal and forecast-quality checks."""

    valid_from_utc: dt.datetime | None
    valid_to_utc: dt.datetime | None
    ceiling_ft: int | None
    visibility_sm: float | None
    wind_speed_kt: int | None
    wind_gust_kt: int | None
    weather: str | None = None
    change_type: str | None = None
    ceiling_is_unlimited: bool = False


@dataclass(frozen=True)
class AirportWeather:
    """Normalized METAR and TAF evidence plus derived terminal risk for one airport."""

    icao: str
    metar_raw: str | None
    metar_time_utc: str | None
    flight_category: str | None
    metar_summary: str | None
    taf_raw: str | None
    taf_issue_time_utc: str | None
    taf_summary: str | None
    metar_risk: TerminalRisk | None = None
    taf_risk: TerminalRisk | None = None
    metar_observed_at_utc: dt.datetime | None = None
    metar_ceiling_ft: int | None = None
    metar_visibility_sm: float | None = None
    metar_wind_speed_kt: int | None = None
    metar_wind_gust_kt: int | None = None
    metar_weather: str | None = None
    taf_periods: tuple[TerminalForecastPeriod, ...] = ()


@dataclass(frozen=True)
class LegalAlternateAssessment:
    """Part 91 fixed-wing destination-alternate requirement assessment."""

    is_required: bool
    status: str
    label: str
    reasons: tuple[str, ...]
    window_start_utc: dt.datetime | None = None
    window_end_utc: dt.datetime | None = None
    worst_ceiling_ft: int | None = None
    worst_visibility_sm: float | None = None
    has_destination_approach: bool = True


@dataclass(frozen=True)
class ForecastQualityCheck:
    """Material differences between an observation and its applicable forecast."""

    phase: str
    icao: str
    score: int
    label: str
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class AlternateRangeRing:
    """One wind-shaped post-missed alternate range boundary."""

    altitude_agl_ft: int
    altitude_msl_ft: int
    alt_cruise_fuel_gal: float
    alt_climb_fuel_gal: float
    alt_descent_fuel_gal: float
    alt_missed_approach_fuel_gal: float
    alt_climb_distance_nm: float
    alt_cruise_distance_nm: float
    alt_descent_distance_nm: float
    alt_min_range_nm: float
    alt_max_range_nm: float
    alt_average_range_nm: float
    points: tuple[tuple[float, float], ...]
    line_style: str
    label: str


@dataclass(frozen=True)
class WindTempPoint:
    """Single decoded winds-aloft forecast point for one station and altitude."""

    station: str
    altitude_ft: int
    direction_deg: int | None
    speed_kt: int | None
    temperature_c: int | None
    raw_code: str


@dataclass(frozen=True)
class FeedStatus:
    """Health and provenance metadata for one external weather feed request."""

    name: str
    endpoint: str
    status: str
    fetched_at_utc: dt.datetime
    row_count: int = 0
    error_message: str | None = None
    params: dict[str, object] = field(default_factory=dict)
    issue_time_utc: dt.datetime | None = None
    valid_from_utc: dt.datetime | None = None
    valid_to_utc: dt.datetime | None = None

    @property
    def is_available(self) -> bool:
        return self.status in {"ok", "empty", "partial"}


@dataclass(frozen=True)
class NoaaWeather:
    """Combined NOAA payloads after the app normalizes each source feed."""

    airports: dict[str, AirportWeather]
    windtemps: list[WindTempPoint]
    windtemp_region: str
    windtemp_level: str
    windtemp_fcst: str
    hazard_areas: list["HazardArea"]
    feed_statuses: dict[str, FeedStatus] = field(default_factory=dict)
    data_confidence: str = "Unknown"


@dataclass(frozen=True)
class HazardArea:
    """Unified hazard polygon model across G-AIRMET, AIRSIGMET, and TCF feeds."""

    hazard_type: str  # icing, turbulence, convective
    severity_score: int
    base_ft: int
    top_ft: int
    polygons: list[list[tuple[float, float]]]  # each point is (lat, lon)
    source: str
    valid_from_utc: dt.datetime | None = None
    valid_to_utc: dt.datetime | None = None


@dataclass(frozen=True)
class SegmentHazard:
    """Hazard scores sampled at one route segment midpoint."""

    segment_index: int
    segment_distance_nm: float
    latitude: float
    longitude: float
    icing_score: int
    turbulence_score: int
    convective_score: int
    overall_score: int
    sources: str
    ifr_score: int = 0
    mountain_obscuration_score: int = 0
    surface_wind_score: int = 0
    llws_score: int = 0


@dataclass(frozen=True)
class RouteWindModel:
    """Route-sampled wind and temperature fields derived from NOAA FD data."""

    segment_tailwind_by_fl: dict[int, list[float]]
    climb_tailwind_by_fl: dict[int, float]
    descent_tailwind_by_fl: dict[int, float]
    segment_crosswind_by_fl: dict[int, list[float]] = field(default_factory=dict)
    climb_crosswind_by_fl: dict[int, float] = field(default_factory=dict)
    descent_crosswind_by_fl: dict[int, float] = field(default_factory=dict)
    station_profiles: tuple[tuple[float, float, dict[int, tuple[float, float]]], ...] = ()
    station_temperature_profiles: tuple[tuple[float, float, dict[int, float]], ...] = ()
    track_deg: float = 0.0
    source: str = "NOAA FD windtemp interpolation"
    station_count: int = 0
    usable_sample_count: int = 0
    uncovered_segment_count: int = 0
    coverage_fraction: float = 1.0


@dataclass(frozen=True)
class FlightLevelProfile:
    """Integrated climb, cruise, and descent performance for one flight level."""

    segment_winds: list[float]
    climb_wind: float
    descent_wind: float
    climb_hours: float
    descent_hours: float
    climb_distance_nm: float
    descent_distance_nm: float
    remaining_distance_nm: float
    cruise_segment_hours: list[float]
    climb_fuel_gal: float
    descent_fuel_gal: float
    cruise_fuel_gal: float
    total_fuel_gal: float
    total_hours: float
    avg_wind: int
    performance_limit_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RouteVerticalProfilePoint:
    """One distance and altitude vertex in the route side-profile path."""

    distance_nm: float
    altitude_ft: int


@dataclass(frozen=True)
class RouteVerticalProfileHazardSpan:
    """Distance and altitude extent of one hazard overlay in the side profile."""

    hazard_type: str
    severity_score: int
    base_ft: int
    top_ft: int
    start_distance_nm: float
    end_distance_nm: float
    source: str


@dataclass(frozen=True)
class RouteVerticalProfileWaypointMarker:
    """Named route point shown along the side-profile distance axis."""

    identifier: str
    distance_nm: float
    is_fuel_stop: bool = False


@dataclass(frozen=True)
class RouteVerticalProfile:
    """Geometry needed to render the route side-profile and hazard overlays."""

    flight_level: int
    mission_distance_nm: float
    cruise_altitude_ft: int
    departure_elevation_ft: int
    destination_elevation_ft: int
    path_points: list[RouteVerticalProfilePoint]
    hazard_spans: list[RouteVerticalProfileHazardSpan]
    waypoint_markers: list[RouteVerticalProfileWaypointMarker] = field(default_factory=list)


def normalize_icao(raw: str) -> str:
    """Normalize user or feed ICAO text before downstream lookups."""

    return (raw or "").strip().upper()


def cruise_flight_levels_for_direction(*, is_westbound: bool) -> list[int]:
    """Return the standard TBM cruise levels for the route direction."""

    return WESTBOUND_CRUISE_LEVELS if is_westbound else EASTBOUND_CRUISE_LEVELS


# Small parsing and display helpers keep the NOAA/data-model code readable.
def _format_time_12h(value: dt.datetime) -> str:
    """Format a local datetime as compact 12-hour clock text."""

    return value.strftime("%I:%M %p").lstrip("0")


def _safe_int(value: object) -> int | None:
    """Convert a feed value to int while treating blanks and bad data as missing."""

    try:
        if value is None or value == "":
            return None
        return int(value)
    except Exception:
        return None


def _safe_float(value: object) -> float | None:
    """Convert a feed value to float while treating blanks and bad data as missing."""

    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def _interpolate_scalar(
    value_low: float,
    value_high: float,
    *,
    x_low: float,
    x_high: float,
    x_target: float,
) -> float:
    """Linearly interpolate one scalar value between two sample points."""

    if x_high == x_low:
        return value_low
    ratio = (x_target - x_low) / (x_high - x_low)
    return value_low + ((value_high - value_low) * ratio)


def _format_local_with_zulu(
    stamp_utc: dt.datetime,
    *,
    timezone_name: str | None,
) -> str:
    """Format a UTC timestamp in a local timezone while preserving Zulu time."""

    try:
        local_tz = pytz.timezone(timezone_name or "UTC")
    except Exception:
        local_tz = pytz.timezone("UTC")

    local_stamp = stamp_utc.astimezone(local_tz)
    return f"{local_stamp.strftime('%b %d')} {_format_time_12h(local_stamp)} ({stamp_utc.strftime('%H%M')}Z)"


def _format_iso_time_local_with_zulu(
    value: object,
    *,
    timezone_name: str | None,
) -> str | None:
    """Parse an ISO timestamp and format it for terminal-weather summaries."""

    if not isinstance(value, str) or not value:
        return None
    try:
        stamp_utc = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
            dt.timezone.utc
        )
        return _format_local_with_zulu(stamp_utc, timezone_name=timezone_name)
    except Exception:
        return None


def _format_unix_time_local_with_zulu(
    value: object,
    *,
    timezone_name: str | None,
) -> str | None:
    """Parse a Unix timestamp and format it for terminal-weather summaries."""

    epoch = _safe_int(value)
    if epoch is None:
        return None
    try:
        stamp_utc = dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
        return _format_local_with_zulu(stamp_utc, timezone_name=timezone_name)
    except Exception:
        return None


def _summarize_wind(
    *,
    direction: int | None,
    speed: int | None,
    gust: int | None,
) -> str | None:
    """Summarize wind direction, speed, and gusts from decoded METAR or TAF fields."""

    if speed is None and gust is None:
        return None

    speed_part = speed if speed is not None else 0
    if direction is not None:
        text = f"Wind {direction:03d} deg at {speed_part} kt"
    else:
        text = f"Wind {speed_part} kt"

    if gust is not None and gust > speed_part:
        text += f", gusting {gust} kt"
    return text


def _score_label(score: int) -> str:
    """Translate a numeric terminal-weather score into a human label."""

    return RISK_LABEL_BY_SCORE.get(int(max(0, min(3, score))), "None")


def _parse_visibility_sm(value: object) -> float | None:
    """Parse statute-mile visibility from numeric or fractional feed text."""

    text = str(value or "").strip().upper().replace("SM", "").replace("+", "")
    if not text:
        return None
    try:
        if " " in text:
            whole, fraction = text.split(" ", 1)
            numerator, denominator = fraction.split("/", 1)
            return float(whole) + (float(numerator) / float(denominator))
        if "/" in text:
            numerator, denominator = text.split("/", 1)
            return float(numerator) / float(denominator)
        return float(text)
    except Exception:
        return None


def _lowest_ceiling_ft(
    *,
    cover: object = None,
    clouds: object = None,
    vertical_visibility: object = None,
) -> int | None:
    """Return the lowest broken, overcast, or vertical-visibility ceiling in feet."""

    ceiling_covers = {"BKN", "OVC", "VV", "OVX"}
    ceiling_values: list[int] = []
    if isinstance(clouds, list):
        for layer in clouds:
            if not isinstance(layer, dict):
                continue
            layer_cover = str(layer.get("cover") or "").strip().upper()
            layer_base = _safe_int(layer.get("base"))
            if layer_cover in ceiling_covers and layer_base is not None:
                ceiling_values.append(layer_base)
    if ceiling_values:
        return min(ceiling_values)
    vertical_visibility_ft = _safe_int(vertical_visibility)
    if vertical_visibility_ft is not None:
        # AWC's decoded JSON vertVis field is already expressed in feet.
        return vertical_visibility_ft
    cover_text = str(cover or "").strip().upper()
    if cover_text.startswith("VV"):
        # Raw METAR VV groups encode hundreds of feet ("VV003" = 300 ft).
        encoded_hundreds = _safe_int(cover_text.replace("VV", ""))
        return encoded_hundreds * 100 if encoded_hundreds is not None else None
    return None


def _clouds_confirm_unlimited_ceiling(clouds: object) -> bool:
    """Return true only when decoded cloud layers contain no ceiling cover at all."""

    if not isinstance(clouds, list):
        return False
    ceiling_covers = {"BKN", "OVC", "VV", "OVX"}
    return not any(
        str(layer.get("cover") or "").strip().upper() in ceiling_covers
        for layer in clouds
        if isinstance(layer, dict)
    )


def _add_terminal_reason(
    reasons: list[str],
    *,
    score: int,
    text: str,
) -> int:
    """Append a terminal-risk reason when a scoring rule finds material risk."""

    if score > 0:
        reasons.append(text)
    return score


def _flight_category_risk(category: object, reasons: list[str]) -> int:
    """Score VFR/MVFR/IFR/LIFR category and record the reason when material."""

    category_text = str(category or "").strip().upper()
    score = {"LIFR": 3, "IFR": 2, "MVFR": 1, "VFR": 0}.get(category_text, 0)
    return _add_terminal_reason(reasons, score=score, text=f"{category_text} flight category") if category_text else 0


def _visibility_risk(visibility: object, reasons: list[str]) -> int:
    """Score reduced visibility using pilot-facing statute-mile thresholds."""

    visibility_sm = _parse_visibility_sm(visibility)
    if visibility_sm is None:
        return 0
    if visibility_sm < 1:
        return _add_terminal_reason(reasons, score=3, text=f"Visibility {visibility_sm:g} SM")
    if visibility_sm < 3:
        return _add_terminal_reason(reasons, score=2, text=f"Visibility {visibility_sm:g} SM")
    if visibility_sm < 5:
        return _add_terminal_reason(reasons, score=1, text=f"Visibility {visibility_sm:g} SM")
    return 0


def _ceiling_risk(ceiling_ft: int | None, reasons: list[str]) -> int:
    """Score low ceiling risk using feet AGL thresholds."""

    if ceiling_ft is None:
        return 0
    if ceiling_ft < 500:
        return _add_terminal_reason(reasons, score=3, text=f"Ceiling {ceiling_ft:,} ft")
    if ceiling_ft < 1000:
        return _add_terminal_reason(reasons, score=2, text=f"Ceiling {ceiling_ft:,} ft")
    if ceiling_ft < 3000:
        return _add_terminal_reason(reasons, score=1, text=f"Ceiling {ceiling_ft:,} ft")
    return 0


def _surface_wind_risk(*, speed: int | None, gust: int | None, reasons: list[str]) -> int:
    """Score surface wind and gust risk for departure or destination operations."""

    peak = max(value for value in (speed or 0, gust or 0))
    if peak >= 35:
        return _add_terminal_reason(reasons, score=3, text=f"Surface wind/gust {peak} kt")
    if peak >= 25:
        return _add_terminal_reason(reasons, score=2, text=f"Surface wind/gust {peak} kt")
    if peak >= 18:
        return _add_terminal_reason(reasons, score=1, text=f"Surface wind/gust {peak} kt")
    return 0


def _weather_string_risk(weather: object, reasons: list[str]) -> int:
    """Score significant present-weather tokens such as TS, FZRA, SN, or FG."""

    wx = str(weather or "").strip().upper()
    if not wx:
        return 0
    compact = wx.replace(" ", "")
    if any(token in compact for token in ("FZRA", "FZDZ", "+TS", "SQ", "FC")):
        return _add_terminal_reason(reasons, score=3, text=f"Weather {wx}")
    if "VCTS" in compact:
        return _add_terminal_reason(reasons, score=1, text=f"Weather {wx}")
    if "TS" in compact or any(token in compact for token in ("+RA", "+SN", "PL", "GR", "GS", "FZFG")):
        return _add_terminal_reason(reasons, score=2, text=f"Weather {wx}")
    if any(token in compact for token in ("RA", "SN", "BR", "FG", "HZ")):
        return _add_terminal_reason(reasons, score=1, text=f"Weather {wx}")
    return 0


def _llws_risk(period: dict[str, object], reasons: list[str]) -> int:
    """Score low-level wind shear groups in decoded TAF periods."""

    ws_hgt = _safe_int(period.get("wshearHgt"))
    ws_dir = _safe_int(period.get("wshearDir"))
    ws_spd = _safe_int(period.get("wshearSpd"))
    if ws_hgt is None or ws_dir is None or ws_spd is None:
        return 0
    score = 3 if ws_spd >= 40 else 2
    return _add_terminal_reason(reasons, score=score, text=f"LLWS {ws_dir:03d}/{ws_spd} kt at {ws_hgt:,} ft")


def _terminal_risk_from_fields(
    *,
    source: str,
    category: object = None,
    visibility: object = None,
    cover: object = None,
    clouds: object = None,
    vertical_visibility: object = None,
    weather: object = None,
    wind_speed: int | None = None,
    wind_gust: int | None = None,
    taf_period: dict[str, object] | None = None,
) -> TerminalRisk:
    """Combine terminal weather fields into one bounded risk score and reasons."""

    reasons: list[str] = []
    scores = [
        _flight_category_risk(category, reasons),
        _visibility_risk(visibility, reasons),
        _ceiling_risk(
            _lowest_ceiling_ft(
                cover=cover,
                clouds=clouds,
                vertical_visibility=vertical_visibility,
            ),
            reasons,
        ),
        _surface_wind_risk(speed=wind_speed, gust=wind_gust, reasons=reasons),
        _weather_string_risk(weather, reasons),
    ]
    if taf_period is not None:
        scores.append(_llws_risk(taf_period, reasons))
    score = max(scores) if scores else 0
    return TerminalRisk(source=source, score=score, label=_score_label(score), reasons=tuple(reasons))


def _terminal_risk_from_metar_row(row: dict[str, object]) -> TerminalRisk | None:
    """Build terminal risk from one decoded METAR API row."""

    if not row:
        return None
    return _terminal_risk_from_fields(
        source="METAR",
        category=row.get("fltCat"),
        visibility=row.get("visib"),
        cover=row.get("cover"),
        clouds=row.get("clouds"),
        vertical_visibility=row.get("vertVis"),
        weather=row.get("wxString"),
        wind_speed=_safe_int(row.get("wspd")),
        wind_gust=_safe_int(row.get("wgst")),
    )


def _terminal_risk_from_taf_row(row: dict[str, object]) -> TerminalRisk | None:
    """Build terminal risk from one decoded TAF API row."""

    if not row:
        return None
    periods = row.get("fcsts")
    if not isinstance(periods, list) or not periods:
        return TerminalRisk(source="TAF", score=0, label="None", reasons=())

    period_risks = [
        _terminal_risk_from_fields(
            source="TAF",
            visibility=period.get("visib"),
            clouds=period.get("clouds"),
            vertical_visibility=period.get("vertVis"),
            weather=period.get("wxString"),
            wind_speed=_safe_int(period.get("wspd")),
            wind_gust=_safe_int(period.get("wgst")),
            taf_period=period,
        )
        for period in periods
        if isinstance(period, dict)
    ]
    if not period_risks:
        return TerminalRisk(source="TAF", score=0, label="None", reasons=())

    max_score = max(risk.score for risk in period_risks)
    reasons: list[str] = []
    for risk in period_risks:
        if risk.score == max_score:
            reasons.extend(reason for reason in risk.reasons if reason not in reasons)
    return TerminalRisk(source="TAF", score=max_score, label=_score_label(max_score), reasons=tuple(reasons))


def _taf_period_from_dict(period: dict[str, object]) -> TerminalForecastPeriod:
    """Normalize one AviationWeather TAF forecast period for later checks."""

    clouds = period.get("clouds")
    ceiling_ft = _lowest_ceiling_ft(
        clouds=clouds,
        vertical_visibility=period.get("vertVis"),
    )
    return TerminalForecastPeriod(
        valid_from_utc=_parse_epoch_utc(period.get("timeFrom")),
        valid_to_utc=_parse_epoch_utc(period.get("timeTo")),
        ceiling_ft=ceiling_ft,
        visibility_sm=_parse_visibility_sm(period.get("visib")),
        wind_speed_kt=_safe_int(period.get("wspd")),
        wind_gust_kt=_safe_int(period.get("wgst")),
        weather=str(period.get("wxString") or "").strip().upper() or None,
        change_type=str(period.get("fcstChange") or "").strip().upper() or None,
        # A ceiling-significant layer with an undecodable height is unknown, not clear sky.
        ceiling_is_unlimited=ceiling_ft is None and _clouds_confirm_unlimited_ceiling(clouds),
    )


def _taf_periods_from_row(row: dict[str, object]) -> tuple[TerminalForecastPeriod, ...]:
    """Extract decoded TAF periods, discarding malformed entries only."""

    periods = row.get("fcsts")
    if not isinstance(periods, list):
        return ()
    return tuple(
        _taf_period_from_dict(period)
        for period in periods
        if isinstance(period, dict)
    )


def _summarize_clouds(cover: object, clouds: object) -> str | None:
    """Summarize decoded cloud cover and bases into readable sky-condition text."""

    layer_parts: list[str] = []
    if isinstance(cover, str) and cover.strip():
        layer_parts.append(cover.strip().upper())

    if isinstance(clouds, list):
        for layer in clouds:
            if not isinstance(layer, dict):
                continue
            layer_cover = str(layer.get("cover") or "").strip().upper()
            if not layer_cover:
                continue
            layer_base = _safe_int(layer.get("base"))
            if layer_base is None:
                layer_parts.append(layer_cover)
            else:
                layer_parts.append(f"{layer_cover} {layer_base} ft")

    if not layer_parts:
        return None
    return "Sky " + ", ".join(layer_parts)


def _decode_signed_tenths(group: str) -> float | None:
    """Decode METAR remark temperature groups stored as signed tenths of Celsius."""

    if len(group) != 4 or group[0] not in {"0", "1"} or not group[1:].isdigit():
        return None
    value = int(group[1:]) / 10.0
    if group[0] == "1":
        value *= -1
    return value


def _summarize_metar_remarks(raw_ob: str) -> list[str]:
    """Translate common METAR remarks into readable operational notes."""

    if " RMK " not in raw_ob:
        return []

    remarks_text = raw_ob.split(" RMK ", 1)[1].strip()
    if not remarks_text:
        return []

    tokens = remarks_text.split()
    decoded: list[str] = []
    leftovers: list[str] = []
    idx = 0

    while idx < len(tokens):
        token = tokens[idx]

        if token == "AO1":
            decoded.append("Automated station without precipitation discriminator (AO1)")
            idx += 1
            continue
        if token == "AO2":
            decoded.append("Automated station with precipitation discriminator (AO2)")
            idx += 1
            continue

        if token == "PK" and idx + 2 < len(tokens) and tokens[idx + 1] == "WND":
            peak_group = tokens[idx + 2]
            peak_match = re.match(r"^(\d{3})(\d{2,3})/(\d{4})$", peak_group)
            if peak_match:
                peak_dir = peak_match.group(1)
                peak_spd = int(peak_match.group(2))
                peak_time = peak_match.group(3)
                decoded.append(
                    f"Peak wind {peak_dir} deg at {peak_spd} kt at {peak_time[:2]}:{peak_time[2:]}Z"
                )
            else:
                decoded.append(f"Peak wind {peak_group}")
            idx += 3
            continue

        slp_match = re.match(r"^SLP(\d{3})$", token)
        if slp_match:
            slp_raw = int(slp_match.group(1))
            sea_level_hpa = 1000.0 + (slp_raw / 10.0) if slp_raw < 500 else 900.0 + (slp_raw / 10.0)
            decoded.append(f"Sea-level pressure {sea_level_hpa:.1f} hPa")
            idx += 1
            continue

        temp_group_match = re.match(r"^T([01]\d{3})([01]\d{3})$", token)
        if temp_group_match:
            exact_temp = _decode_signed_tenths(temp_group_match.group(1))
            exact_dewp = _decode_signed_tenths(temp_group_match.group(2))
            if exact_temp is not None and exact_dewp is not None:
                decoded.append(f"Exact temp/dewpoint {exact_temp:.1f}C/{exact_dewp:.1f}C")
            idx += 1
            continue

        max6_match = re.match(r"^1([01]\d{3})$", token)
        if max6_match:
            max_temp = _decode_signed_tenths(max6_match.group(1))
            if max_temp is not None:
                decoded.append(f"6h max temp {max_temp:.1f}C")
            idx += 1
            continue

        min6_match = re.match(r"^2([01]\d{3})$", token)
        if min6_match:
            min_temp = _decode_signed_tenths(min6_match.group(1))
            if min_temp is not None:
                decoded.append(f"6h min temp {min_temp:.1f}C")
            idx += 1
            continue

        maxmin24_match = re.match(r"^4([01]\d{3})([01]\d{3})$", token)
        if maxmin24_match:
            max24 = _decode_signed_tenths(maxmin24_match.group(1))
            min24 = _decode_signed_tenths(maxmin24_match.group(2))
            if max24 is not None and min24 is not None:
                decoded.append(f"24h max/min temp {max24:.1f}C/{min24:.1f}C")
            idx += 1
            continue

        pressure_tendency_match = re.match(r"^5([0-8])(\d{3})$", token)
        if pressure_tendency_match:
            tendency_code = pressure_tendency_match.group(1)
            tendency_mag = int(pressure_tendency_match.group(2)) / 10.0
            decoded.append(f"3h pressure tendency code {tendency_code}, change {tendency_mag:.1f} hPa")
            idx += 1
            continue

        if token == "$":
            decoded.append("Maintenance indicator ($)")
            idx += 1
            continue

        leftovers.append(token)
        idx += 1

    if leftovers:
        decoded.append("Additional remarks: " + " ".join(leftovers))
    return decoded


def _summarize_metar_row(
    row: dict[str, object],
    *,
    timezone_name: str | None,
) -> str | None:
    """Build an English METAR summary from a decoded AviationWeather API row."""

    if not row:
        return None

    parts: list[str] = []
    category = str(row.get("fltCat") or "").strip().upper()
    if category:
        parts.append(f"{category} conditions")

    wind = _summarize_wind(
        direction=_safe_int(row.get("wdir")),
        speed=_safe_int(row.get("wspd")),
        gust=_safe_int(row.get("wgst")),
    )
    if wind:
        parts.append(wind)

    visibility = str(row.get("visib") or "").strip()
    if visibility:
        parts.append(f"Visibility {visibility} SM")

    weather = str(row.get("wxString") or "").strip()
    if weather:
        parts.append(f"Weather {weather}")

    cloud_summary = _summarize_clouds(row.get("cover"), row.get("clouds"))
    if cloud_summary:
        parts.append(cloud_summary)

    temperature_c = _safe_float(row.get("temp"))
    dewpoint_c = _safe_float(row.get("dewp"))
    if temperature_c is not None and dewpoint_c is not None:
        parts.append(f"Temp {temperature_c:.1f}C / Dewpoint {dewpoint_c:.1f}C")
    elif temperature_c is not None:
        parts.append(f"Temp {temperature_c:.1f}C")

    altimeter_hpa = _safe_float(row.get("altim"))
    if altimeter_hpa is not None:
        altimeter_inhg = altimeter_hpa * 0.0295299831
        parts.append(f"Altimeter {altimeter_inhg:.2f} inHg")

    remark_parts = _summarize_metar_remarks(str(row.get("rawOb") or ""))
    if remark_parts:
        parts.append("Remarks: " + "; ".join(remark_parts))

    observed_time = _format_unix_time_local_with_zulu(
        row.get("obsTime"),
        timezone_name=timezone_name,
    )
    if not observed_time:
        observed_time = _format_iso_time_local_with_zulu(
            row.get("reportTime"),
            timezone_name=timezone_name,
        )
    if observed_time:
        parts.append(f"Observed {observed_time}")

    if parts:
        return ". ".join(parts) + "."
    if row.get("rawOb"):
        return "Raw METAR available."
    return None


def _summarize_taf_period(
    period: dict[str, object],
    *,
    timezone_name: str | None,
) -> str | None:
    """Summarize one decoded TAF forecast period for display."""

    period_bits: list[str] = []

    from_local = _format_unix_time_local_with_zulu(
        period.get("timeFrom"),
        timezone_name=timezone_name,
    )
    to_local = _format_unix_time_local_with_zulu(
        period.get("timeTo"),
        timezone_name=timezone_name,
    )
    if from_local and to_local:
        period_bits.append(f"{from_local} to {to_local}")

    change_type = str(period.get("fcstChange") or "").strip().upper()
    if change_type:
        period_bits.append(change_type)

    probability = _safe_int(period.get("probability"))
    if probability is not None and probability > 0:
        period_bits.append(f"Prob {probability}%")

    wind = _summarize_wind(
        direction=_safe_int(period.get("wdir")),
        speed=_safe_int(period.get("wspd")),
        gust=_safe_int(period.get("wgst")),
    )
    if wind:
        period_bits.append(wind)

    visibility = str(period.get("visib") or "").strip()
    if visibility:
        period_bits.append(f"Visibility {visibility} SM")

    weather = str(period.get("wxString") or "").strip()
    if weather:
        period_bits.append(f"Weather {weather}")

    cloud_summary = _summarize_clouds(None, period.get("clouds"))
    if cloud_summary:
        period_bits.append(cloud_summary)

    ws_hgt = _safe_int(period.get("wshearHgt"))
    ws_dir = _safe_int(period.get("wshearDir"))
    ws_spd = _safe_int(period.get("wshearSpd"))
    if ws_hgt is not None and ws_dir is not None and ws_spd is not None:
        period_bits.append(f"LLWS {ws_dir:03d}/{ws_spd}kt at {ws_hgt} ft")

    if not period_bits:
        return None
    return ", ".join(period_bits)


def _summarize_taf_row(
    row: dict[str, object],
    *,
    timezone_name: str | None,
) -> str | None:
    """Build an English TAF summary from decoded AviationWeather API fields."""

    if not row:
        return None

    parts: list[str] = []
    valid_from = _format_unix_time_local_with_zulu(
        row.get("validTimeFrom"),
        timezone_name=timezone_name,
    )
    valid_to = _format_unix_time_local_with_zulu(
        row.get("validTimeTo"),
        timezone_name=timezone_name,
    )
    if valid_from and valid_to:
        parts.append(f"Valid {valid_from}-{valid_to}")

    period_texts: list[str] = []
    fcsts = row.get("fcsts")
    if isinstance(fcsts, list):
        for period in fcsts[:4]:
            if not isinstance(period, dict):
                continue
            period_text = _summarize_taf_period(period, timezone_name=timezone_name)
            if period_text:
                period_texts.append(period_text)
    if period_texts:
        parts.append("Forecast periods: " + " | ".join(period_texts))

    issue_time = _format_iso_time_local_with_zulu(
        row.get("issueTime"),
        timezone_name=timezone_name,
    )
    if issue_time:
        parts.append(f"Issued {issue_time}")

    if parts:
        return ". ".join(parts) + "."
    if row.get("rawTAF"):
        return "Raw TAF available."
    return None


# Airport and station lookup live here so every NOAA parser uses one source of truth.
@lru_cache(maxsize=1)
def _load_airport_db() -> dict[str, dict[str, object]]:
    """Load the bundled airportsdata ICAO table once, returning empty data on failure."""

    try:
        import airportsdata

        return airportsdata.load("ICAO")
    except Exception:
        return {}


@lru_cache(maxsize=1)
def _load_station_alias_coords() -> dict[str, tuple[float, float]]:
    """Build lookup coordinates for ICAO, IATA, and local station identifiers."""

    alias_coords: dict[str, tuple[float, float]] = {}
    for row in _load_airport_db().values():
        try:
            lat = float(row["lat"])
            lon = float(row["lon"])
        except Exception:
            continue

        for key in ("icao", "iata", "lid"):
            alias = str(row.get(key) or "").strip().upper()
            if alias:
                alias_coords[alias] = (lat, lon)
    return alias_coords


def _lookup_windtemp_station_coords(station: str) -> tuple[float, float] | None:
    """Return coordinates for a winds-aloft station identifier when known."""

    return _load_station_alias_coords().get(normalize_icao(station))


def _lookup_airport_from_avwx(
    code: str,
    *,
    session: requests.Session,
    api_token: str,
    timeout_seconds: int,
) -> AirportData | None:
    """Resolve airport metadata through AVWX when the user provides a token."""

    try:
        response = session.get(
            f"https://avwx.rest/api/station/{code}",
            headers={"Authorization": api_token},
            timeout=timeout_seconds,
        )
        response.raise_for_status()
        payload = response.json()
        return AirportData(
            icao=code,
            latitude=float(payload["latitude"]),
            longitude=float(payload["longitude"]),
            timezone=str(payload["timezone"]),
            source="avwx",
            # AVWX station payloads expose feet explicitly. Retain the legacy key
            # as a compatibility fallback for older/self-hosted responses.
            elevation_ft=float(payload.get("elevation_ft") or payload.get("elevation") or 0.0),
        )
    except Exception:
        return None


def _lookup_airport_from_builtin_db(code: str) -> AirportData | None:
    """Resolve airport metadata from the local airportsdata package."""

    airport_db = _load_airport_db()
    row = airport_db.get(code)
    if not row:
        return None

    try:
        return AirportData(
            icao=code,
            latitude=float(row["lat"]),
            longitude=float(row["lon"]),
            timezone=str(row.get("tz") or DEFAULT_TZ),
            source="airportsdata",
            elevation_ft=float(row.get("elevation") or 0.0),
        )
    except Exception:
        return None


def get_airport_data(
    icao: str,
    *,
    session: requests.Session | None = None,
    api_token: str | None = None,
    timeout_seconds: int = 5,
) -> AirportData:
    """
    Resolve airport coordinates/timezone with no-token support.
    Priority:
    1) AVWX if token is provided.
    2) Built-in airportsdata dataset.
    3) Refuse unresolved identifiers so callers cannot plan from fabricated coordinates.
    """
    code = normalize_icao(icao)
    client = session or requests.Session()
    token = api_token or os.getenv("AVWX_API_TOKEN")

    if token:
        from_avwx = _lookup_airport_from_avwx(
            code,
            session=client,
            api_token=token,
            timeout_seconds=timeout_seconds,
        )
        if from_avwx:
            return from_avwx

    from_builtin = _lookup_airport_from_builtin_db(code)
    if from_builtin:
        return from_builtin

    raise ValueError(f"Airport {code or '<blank>'} could not be resolved by AVWX or the built-in database.")


def infer_windtemp_region(
    departure: AirportData,
    destination: AirportData,
    route_plan: RoutePlan | None = None,
) -> str:
    """Infer one or more NOAA FD regions covering the route, returned comma-separated."""

    if route_plan is not None:
        midpoint_lat, midpoint_lon = route_midpoint_lat_lon(route_plan)
    else:
        midpoint_lat = (departure.latitude + destination.latitude) / 2.0
        midpoint_lon = (departure.longitude + destination.longitude) / 2.0

    def region_for(latitude: float, longitude: float) -> str:
        if 18.0 <= latitude <= 23.0 and -162.0 <= longitude <= -153.0:
            return "hawaii"
        if latitude >= 54.0 and longitude <= -130.0:
            return "alaska"
        if longitude <= -117.0:
            return "sfo"
        if longitude <= -105.0:
            return "slc"
        if longitude <= -92.0:
            return "dfw"
        if latitude <= 33.5:
            return "mia"
        if latitude >= 40.5:
            return "bos"
        return "chi"

    midpoint_region = region_for(midpoint_lat, midpoint_lon)
    route_distance_nm = (
        route_plan.total_distance_nm
        if route_plan is not None
        else great_circle_distance_nm(
            departure.latitude,
            departure.longitude,
            destination.latitude,
            destination.longitude,
        )
    )
    if route_distance_nm <= MULTI_REGION_ROUTE_DISTANCE_NM:
        return midpoint_region
    regions = [
        region_for(departure.latitude, departure.longitude),
        midpoint_region,
        region_for(destination.latitude, destination.longitude),
    ]
    return ",".join(dict.fromkeys(regions))


def _coerce_latest_rows(
    rows: list[dict[str, object]],
    *,
    score_field: str,
) -> dict[str, dict[str, object]]:
    """Keep the highest-scoring latest row per airport from NOAA API results."""

    latest: dict[str, dict[str, object]] = {}
    latest_scores: dict[str, int] = {}

    for row in rows:
        code = normalize_icao(str(row.get("icaoId") or ""))
        if not code:
            continue
        score = _safe_int(row.get(score_field)) or 0
        if code not in latest_scores or score >= latest_scores[code]:
            latest[code] = row
            latest_scores[code] = score
    return latest


def _decode_windtemp_group(
    group: str,
    *,
    altitude_ft: int,
) -> tuple[int | None, int | None, int | None]:
    """Decode one NOAA FD winds-aloft group into direction, speed, and temperature."""

    token = group.strip()
    if not token or token.startswith("/"):
        return None, None, None

    match = WINDTEMP_TOKEN_PATTERN.match(token)
    if not match:
        return None, None, None

    dd = int(match.group("dd"))
    ff = int(match.group("ff"))
    temp_part = match.group("tt")

    if dd == 99 and ff == 0:
        # NOAA 9900 means light and variable; a zero vector contributes calm
        # without inventing a direction or being discarded as invalid.
        direction_deg = 0
        speed_kt = 0
    else:
        if dd >= 51:
            dd -= 50
            ff += 100
        direction_deg = dd * 10
        speed_kt = ff
        if direction_deg < 10 or direction_deg > 360:
            direction_deg = None
            speed_kt = None

    temperature_c: int | None = None
    if temp_part:
        if temp_part.startswith("+") or temp_part.startswith("-"):
            temperature_c = int(temp_part)
        else:
            raw_temp = int(temp_part)
            temperature_c = -raw_temp if altitude_ft >= 24000 else raw_temp

    return direction_deg, speed_kt, temperature_c


def parse_windtemp_text(raw_text: str) -> list[WindTempPoint]:
    """Parse NOAA FD text into station/altitude wind and temperature samples."""

    lines = [line.rstrip() for line in raw_text.splitlines() if line.strip()]
    header_idx = -1
    for idx, line in enumerate(lines):
        if line.lstrip().startswith("FT"):
            header_idx = idx
            break

    if header_idx < 0:
        return []

    header_line = lines[header_idx]
    altitude_columns = list(re.finditer(r"\d{4,5}", header_line))
    if not altitude_columns:
        return []

    altitudes = [int(match.group(0)) for match in altitude_columns]
    starts = [match.start() for match in altitude_columns]

    points: list[WindTempPoint] = []

    for line in lines[header_idx + 1 :]:
        station_match = re.match(r"^\s*([A-Z0-9]{3,4})\b", line)
        if not station_match:
            continue
        station = station_match.group(1)
        token_matches = list(WINDTEMP_GROUP_PATTERN.finditer(line))
        if not token_matches:
            continue

        # NOAA columns can be sparse, so map tokens to the nearest valid altitude header.
        token_starts = [match.start() for match in token_matches]
        assigned_columns = _best_column_assignment(token_starts, starts)
        for token_match, column_idx in zip(token_matches, assigned_columns):
            group = token_match.group(0).strip()
            if not group or group.startswith("/"):
                continue

            altitude_ft = altitudes[column_idx]
            direction_deg, speed_kt, temperature_c = _decode_windtemp_group(
                group,
                altitude_ft=altitude_ft,
            )
            points.append(
                WindTempPoint(
                    station=station,
                    altitude_ft=altitude_ft,
                    direction_deg=direction_deg,
                    speed_kt=speed_kt,
                    temperature_c=temperature_c,
                    raw_code=group,
                )
            )

    return points


def _best_column_assignment(token_starts: list[int], column_starts: list[int]) -> list[int]:
    """
    Match each windtemp token to a header altitude column while allowing missing columns.
    Returns column indexes in token order.
    """
    if not token_starts or not column_starts:
        return []

    n = min(len(token_starts), len(column_starts))
    token_starts = token_starts[:n]
    m = len(column_starts)

    inf = 10**9
    dp = [[inf for _ in range(m)] for _ in range(n)]
    prev = [[-1 for _ in range(m)] for _ in range(n)]

    max_first_col = m - n
    for col in range(0, max_first_col + 1):
        dp[0][col] = abs(token_starts[0] - column_starts[col])

    for token_idx in range(1, n):
        min_col = token_idx
        max_col = m - (n - token_idx)
        for col in range(min_col, max_col + 1):
            cost = abs(token_starts[token_idx] - column_starts[col])
            for prev_col in range(token_idx - 1, col):
                prev_cost = dp[token_idx - 1][prev_col]
                if prev_cost >= inf:
                    continue
                candidate = prev_cost + cost
                if candidate < dp[token_idx][col]:
                    dp[token_idx][col] = candidate
                    prev[token_idx][col] = prev_col

    best_cost = inf
    best_col = -1
    for col in range(n - 1, m):
        if dp[n - 1][col] < best_cost:
            best_cost = dp[n - 1][col]
            best_col = col

    if best_col < 0:
        return list(range(n))

    assignment = [0 for _ in range(n)]
    assignment[n - 1] = best_col
    for token_idx in range(n - 1, 0, -1):
        assignment[token_idx - 1] = prev[token_idx][assignment[token_idx]]
    return assignment


def _parse_epoch_utc(value: object) -> dt.datetime | None:
    """Parse a Unix epoch value as a timezone-aware UTC datetime."""

    epoch = _safe_int(value)
    if epoch is None:
        return None
    try:
        return dt.datetime.fromtimestamp(epoch, tz=dt.timezone.utc)
    except Exception:
        return None


def _parse_iso_utc(value: object) -> dt.datetime | None:
    """Parse an ISO timestamp as a timezone-aware UTC datetime."""

    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except Exception:
        return None


def _parse_tcf_valid_time_utc(value: object) -> dt.datetime | None:
    """Parse the TCF valid-time formats used by NOAA convective feeds."""

    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y%m%d_%H%M").replace(tzinfo=dt.timezone.utc)
    except Exception:
        return _parse_iso_utc(value)


def _parse_altitude_feet(value: object, *, assume_hundreds: bool = True) -> int | None:
    """Parse altitude text, flight levels, SFC/GND, or numeric hundreds into feet."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        raw = int(value)
        return raw * 100 if assume_hundreds and raw <= 700 else raw

    text = str(value).strip().upper()
    if not text:
        return None
    if text in {"SFC", "GND"}:
        return 0

    match = re.search(r"(\d+)", text)
    if not match:
        return None

    number = int(match.group(1))
    if text.startswith("FL") or (assume_hundreds and number <= 700):
        return number * 100
    return number


def _risk_score_from_severity(value: object, *, min_score: int = 1) -> int:
    """Normalize feed-specific severity values into the app's zero-to-three scale."""

    score = min_score
    if value is None or str(value).strip() == "":
        return min(max(score, 0), 3)

    if isinstance(value, (int, float)):
        if value >= 5:
            score = max(score, 3)
        elif value >= 3:
            score = max(score, 2)
        else:
            score = max(score, 1)
        return min(max(score, 0), 3)

    text = str(value).strip().upper()
    if any(token in text for token in ("SVR", "SEV", "EXTM", "HVY")):
        score = max(score, 3)
    elif "MOD" in text:
        score = max(score, 2)
    elif "LGT" in text:
        score = max(score, 1)
    return min(max(score, 0), 3)


def _risk_score_from_tcf(*, coverage: object, confidence: object) -> int:
    """Score TCF convective areas from coverage and confidence descriptors."""

    coverage_key = str(coverage or "").strip().lower()
    confidence_key = str(confidence or "").strip().lower()
    coverage_score = {
        "isolated": 1,
        "sparse": 1,
        "scattered": 2,
        "areas": 2,
        "numerous": 3,
        "widespread": 3,
    }.get(coverage_key, 2 if coverage_key else 1)
    confidence_boost = 1 if confidence_key in {"high", "likely"} else 0
    return min(3, coverage_score + confidence_boost)


def _hazard_band_from_gairmet(record: dict[str, object]) -> tuple[int, int]:
    """Extract a usable base/top altitude band from a G-AIRMET record."""

    base_raw = str(record.get("base") or "").upper()
    top_raw = str(record.get("top") or "").upper()

    base_ft = _parse_altitude_feet(record.get("fzlbase") if base_raw == "FZL" else base_raw)
    top_ft = _parse_altitude_feet(record.get("fzltop") if top_raw == "FZL" else top_raw)

    if base_ft is None:
        base_ft = _parse_altitude_feet(record.get("fzlbase"))
    if top_ft is None:
        top_ft = _parse_altitude_feet(record.get("fzltop"))

    if base_ft is None:
        base_ft = 0
    if top_ft is None:
        top_ft = 60000

    if top_ft < base_ft:
        base_ft, top_ft = top_ft, base_ft
    return base_ft, top_ft


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
        rate_fpm = max(int(row.rate_fpm if row else fallback_rate_fpm), 100)
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


def _parse_hazard_areas(
    *,
    gairmet_rows: list[dict[str, object]],
    airsigmet_rows: list[dict[str, object]],
    tcf_payload: dict[str, object],
    cwa_payload: dict[str, object],
    pirep_rows: list[dict[str, object]],
) -> list[HazardArea]:
    """Normalize all hazard feeds into one common polygon/time/altitude model."""

    areas: list[HazardArea] = []

    gairmet_hazard_types = {
        "ICE": "icing",
        "TURB-HI": "turbulence",
        "TURB-LO": "turbulence",
        "IFR": "ifr",
        "MT_OBSC": "mountain_obscuration",
        "SFC_WND": "surface_wind",
        "LLWS": "llws",
    }
    surface_hazard_bands = {
        "ifr": (0, 12000),
        "mountain_obscuration": (0, 18000),
        "surface_wind": (0, 3000),
        "llws": (0, 2000),
    }

    # G-AIRMET carries icing, turbulence, IFR, mountain obscuration, surface wind, and LLWS polygons.
    for row in gairmet_rows:
        hazard_code = str(row.get("hazard") or "").upper()
        hazard_type = gairmet_hazard_types.get(hazard_code)
        if hazard_type is None:
            continue
        polygon = _polygon_from_latlon_dicts(row.get("coords"))
        if len(polygon) < 3:
            continue

        base_ft, top_ft = _hazard_band_from_gairmet(row)
        if hazard_type in surface_hazard_bands and (base_ft, top_ft) == (0, 60000):
            base_ft, top_ft = surface_hazard_bands[hazard_type]
        issue_time = _parse_epoch_utc(row.get("issueTime")) or _parse_iso_utc(row.get("issueTime"))
        snapshot_time = _parse_epoch_utc(row.get("validTime")) or _parse_iso_utc(row.get("validTime"))
        forecast_hour = _safe_float(row.get("forecastHour"))
        if snapshot_time is None and issue_time is not None and forecast_hour is not None:
            snapshot_time = issue_time + dt.timedelta(hours=forecast_hour)
        if snapshot_time is not None:
            valid_from = snapshot_time - GAIRMET_SNAPSHOT_HALF_WINDOW
            valid_to = snapshot_time + GAIRMET_SNAPSHOT_HALF_WINDOW
        else:
            valid_from = issue_time
            valid_to = _parse_epoch_utc(row.get("expireTime")) or _parse_iso_utc(row.get("expireTime"))
        areas.append(
            HazardArea(
                hazard_type=hazard_type,
                severity_score=_risk_score_from_severity(row.get("severity"), min_score=1),
                base_ft=base_ft,
                top_ft=top_ft,
                polygons=[polygon],
                source=f"G-AIRMET {row.get('tag', '')} {hazard_code}".strip(),
                valid_from_utc=valid_from,
                valid_to_utc=valid_to,
            )
        )

    # AIRSIGMET uses a different schema and generally represents higher-severity hazards.
    for row in airsigmet_rows:
        hazard_code = str(row.get("hazard") or "").upper()
        if "CONVECTIVE" in hazard_code:
            hazard_type = "convective"
            min_score = 2
        elif "TURB" in hazard_code:
            hazard_type = "turbulence"
            min_score = 2
        elif "ICE" in hazard_code:
            hazard_type = "icing"
            min_score = 2
        elif "IFR" in hazard_code:
            hazard_type = "ifr"
            min_score = 2
        elif "MT" in hazard_code and "OBSC" in hazard_code:
            hazard_type = "mountain_obscuration"
            min_score = 2
        elif "SFC" in hazard_code and "WND" in hazard_code:
            hazard_type = "surface_wind"
            min_score = 2
        elif "LLWS" in hazard_code:
            hazard_type = "llws"
            min_score = 2
        else:
            continue

        polygon = _polygon_from_latlon_dicts(row.get("coords"))
        if len(polygon) < 3:
            continue

        # altitudeLow1 of 0 is a valid surface base; fall through only when a field is absent.
        low_ft = _parse_altitude_feet(row.get("altitudeLow1"), assume_hundreds=False)
        if low_ft is None:
            low_ft = _parse_altitude_feet(row.get("altitudeLow2"), assume_hundreds=False)
        if low_ft is None:
            low_ft = 0
        high_ft = _parse_altitude_feet(row.get("altitudeHi1"), assume_hundreds=False)
        if high_ft is None:
            high_ft = _parse_altitude_feet(row.get("altitudeHi2"), assume_hundreds=False)
        if high_ft is None:
            high_ft = 60000
        if high_ft < low_ft:
            low_ft, high_ft = high_ft, low_ft

        areas.append(
            HazardArea(
                hazard_type=hazard_type,
                severity_score=_risk_score_from_severity(row.get("severity"), min_score=min_score),
                base_ft=low_ft,
                top_ft=high_ft,
                polygons=[polygon],
                source=f"AIRSIGMET {row.get('seriesId', '')} {hazard_code}".strip(),
                valid_from_utc=_parse_epoch_utc(row.get("validTimeFrom")),
                valid_to_utc=_parse_epoch_utc(row.get("validTimeTo")),
            )
        )

    # TCF contributes convective polygons with tops and a single valid time.
    features = tcf_payload.get("features") if isinstance(tcf_payload, dict) else None
    if isinstance(features, list):
        for feature in features:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties")
            geometry = feature.get("geometry")
            if not isinstance(properties, dict):
                properties = {}
            polygons = _polygons_from_geojson_geometry(geometry)
            if not polygons:
                continue

            tops_ft = _parse_altitude_feet(properties.get("tops")) or 60000
            valid_time = _parse_tcf_valid_time_utc(properties.get("validTime"))
            areas.append(
                HazardArea(
                    hazard_type="convective",
                    severity_score=_risk_score_from_tcf(
                        coverage=properties.get("coverage"),
                        confidence=properties.get("confidence"),
                    ),
                    base_ft=0,
                    top_ft=tops_ft,
                    polygons=polygons,
                    source=f"TCF {properties.get('coverage', '')}/{properties.get('confidence', '')}".strip(),
                    valid_from_utc=valid_time,
                    valid_to_utc=valid_time,
                )
            )

    # CWAs are short-fuse warnings; when geometry is available, carry them as high-priority
    # advisory polygons without pretending they are long-range flight-planning products.
    cwa_features = cwa_payload.get("features") if isinstance(cwa_payload, dict) else None
    if isinstance(cwa_features, list):
        for feature in cwa_features:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties")
            if not isinstance(properties, dict):
                properties = {}
            polygons = _polygons_from_geojson_geometry(feature.get("geometry"))
            if not polygons:
                continue

            advisory_text = " ".join(
                str(value or "").upper()
                for value in (
                    properties.get("phenom") or properties.get("phenomenon"),
                    properties.get("qualifier"),
                )
                if value
            )
            if not advisory_text:
                # Unknown is safer and more honest than inventing a turbulence advisory.
                continue
            if "LLWS" in advisory_text:
                hazard_type = "llws"
            elif "TURB" in advisory_text:
                hazard_type = "turbulence"
            elif "ICE" in advisory_text or "ICING" in advisory_text:
                hazard_type = "icing"
            elif "IFR" in advisory_text or "CIG" in advisory_text or "VIS" in advisory_text:
                hazard_type = "ifr"
            elif "WND" in advisory_text or "WIND" in advisory_text:
                hazard_type = "surface_wind"
            elif any(token in advisory_text for token in ("TS", "CONV", "CB")):
                hazard_type = "convective"
            else:
                continue

            # A base of 0 is a valid surface anchor; fall through only when a field is absent.
            base_ft = _parse_altitude_feet(properties.get("base"), assume_hundreds=False)
            if base_ft is None:
                base_ft = _parse_altitude_feet(properties.get("bottom"), assume_hundreds=False)
            if base_ft is None:
                base_ft = 0
            top_ft = _parse_altitude_feet(properties.get("top"), assume_hundreds=False)
            if top_ft is None:
                top_ft = _parse_altitude_feet(properties.get("altitudeHi"), assume_hundreds=False)
            if top_ft is None:
                top_ft = DEFAULT_HAZARD_TOP_FT
            if top_ft < base_ft:
                base_ft, top_ft = top_ft, base_ft
            # Live CWA GeoJSON carries a numeric seriesId; urgent CWAs mark themselves
            # with a "UCWA" product header inside cwaText instead.
            is_urgent = (
                str(properties.get("seriesId") or properties.get("series") or "").upper().startswith("U")
                or str(properties.get("cwaText") or "").lstrip().upper().startswith("UCWA")
            )
            qualifier_score = _risk_score_from_severity(properties.get("qualifier"), min_score=2)
            areas.append(
                HazardArea(
                    hazard_type=hazard_type,
                    severity_score=3 if is_urgent else qualifier_score,
                    base_ft=base_ft,
                    top_ft=top_ft,
                    polygons=polygons,
                    source=f"CWA {properties.get('cwsu', '')} {properties.get('phenom', '')}".strip(),
                    valid_from_utc=_parse_iso_utc(properties.get("validTimeFrom")) or _parse_iso_utc(properties.get("issueTime")),
                    valid_to_utc=_parse_iso_utc(properties.get("validTimeTo")) or _parse_iso_utc(properties.get("expireTime")),
                )
            )

    # PIREPs/AIREPs are observations rather than forecasts, so they use a localized footprint
    # around the reported point and keep validity anchored to the report time.
    for row in pirep_rows:
        lat = _safe_float(row.get("lat") or row.get("latitude"))
        lon = _safe_float(row.get("lon") or row.get("longitude"))
        if lat is None or lon is None:
            continue
        report_text = " ".join(str(value) for value in row.values() if value is not None).upper()
        report_altitude_ft = (
            _parse_altitude_feet(row.get("fltLvl"))
            or _parse_altitude_feet(row.get("flightLevel"))
            or _parse_altitude_feet(row.get("altitude"))
            or 0
        )
        report_time = _parse_iso_utc(row.get("reportTime")) or _parse_epoch_utc(row.get("obsTime"))
        observations: list[tuple[str, str, object, object]] = []
        structured_hazard_types: set[str] = set()
        for hazard_type, prefix in (("icing", "icg"), ("turbulence", "tb")):
            for layer_number in (1, 2):
                intensity = str(row.get(f"{prefix}Int{layer_number}") or "").strip().upper()
                if intensity and not any(token in intensity for token in ("NEG", "NIL", "NONE")):
                    observations.append(
                        (
                            hazard_type,
                            intensity,
                            row.get(f"{prefix}Bas{layer_number}"),
                            row.get(f"{prefix}Top{layer_number}"),
                        )
                    )
                    structured_hazard_types.add(hazard_type)

        if "icing" not in structured_hazard_types:
            if re.search(r"\bIC(?:E|ING)?\b", report_text) and not re.search(
                r"\bIC(?:E|ING)?\s+(?:NEG|NIL|NONE)\b", report_text
            ):
                observations.append(("icing", report_text, None, None))
        if "turbulence" not in structured_hazard_types:
            if (re.search(r"\bTB\b|\bTURB(?:ULENCE)?\b", report_text)) and not re.search(
                r"(?:\bTB\b|\bTURB(?:ULENCE)?\b)\s+(?:NEG|NIL|NONE)\b", report_text
            ):
                observations.append(("turbulence", report_text, None, None))
        if "LLWS" in report_text:
            observations.append(("llws", report_text, None, None))
        if "IFR" in report_text:
            observations.append(("ifr", report_text, None, None))

        for hazard_type, intensity, structured_base, structured_top in observations:
            base_ft = _parse_altitude_feet(structured_base)
            top_ft = _parse_altitude_feet(structured_top)
            if base_ft is None:
                base_ft = max(report_altitude_ft - PIREP_ALTITUDE_HALF_BAND_FT, 0)
            if top_ft is None:
                top_ft = report_altitude_ft + PIREP_ALTITUDE_HALF_BAND_FT if report_altitude_ft else 60000
            areas.append(
                HazardArea(
                    hazard_type=hazard_type,
                    severity_score=_risk_score_from_severity(intensity, min_score=1),
                    base_ft=base_ft,
                    top_ft=max(top_ft, base_ft),
                    polygons=[_circle_polygon_nm(lat, lon)],
                    source=f"PIREP/AIREP {row.get('aircraftRef', '')} {row.get('rawOb', '')}".strip(),
                    valid_from_utc=report_time - PIREP_VALID_BEFORE if report_time else None,
                    valid_to_utc=report_time + PIREP_VALID_AFTER if report_time else None,
                )
            )

    return areas


def _feed_error_message(exc: Exception) -> str:
    """Return readable error text for a failed external feed request."""

    message = str(exc).strip()
    return message or exc.__class__.__name__


def _status_for_count(row_count: int) -> str:
    """Classify a successful feed response as populated or empty."""

    return "ok" if row_count > 0 else "empty"


def _build_feed_status(
    *,
    name: str,
    endpoint: str,
    params: dict[str, object],
    fetched_at_utc: dt.datetime,
    row_count: int,
    error: Exception | None = None,
    issue_time_utc: dt.datetime | None = None,
    valid_from_utc: dt.datetime | None = None,
    valid_to_utc: dt.datetime | None = None,
) -> FeedStatus:
    """Create one canonical feed-health record without hiding failed fetches."""

    return FeedStatus(
        name=name,
        endpoint=f"{NOAA_API_BASE_URL}/{endpoint}",
        status="failed" if error is not None else _status_for_count(row_count),
        fetched_at_utc=fetched_at_utc,
        row_count=row_count,
        error_message=_feed_error_message(error) if error is not None else None,
        params=dict(params),
        issue_time_utc=issue_time_utc,
        valid_from_utc=valid_from_utc,
        valid_to_utc=valid_to_utc,
    )


def _request_noaa_json_feed(
    endpoint: str,
    *,
    name: str,
    params: dict[str, object],
    session: requests.Session,
    timeout_seconds: int,
    fetched_at_utc: dt.datetime,
) -> tuple[list[dict[str, object]], FeedStatus]:
    """Request a NOAA JSON-list endpoint and return rows plus feed status."""

    try:
        response = session.get(
            f"{NOAA_API_BASE_URL}/{endpoint}",
            params=params,
            timeout=timeout_seconds,
        )
        if getattr(response, "status_code", None) == 204:
            return [], _build_feed_status(
                name=name,
                endpoint=endpoint,
                params=params,
                fetched_at_utc=fetched_at_utc,
                row_count=0,
            )
        response.raise_for_status()
        payload = response.json()
        rows = [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []
        return rows, _build_feed_status(
            name=name,
            endpoint=endpoint,
            params=params,
            fetched_at_utc=fetched_at_utc,
            row_count=len(rows),
        )
    except Exception as exc:
        return [], _build_feed_status(
            name=name,
            endpoint=endpoint,
            params=params,
            fetched_at_utc=fetched_at_utc,
            row_count=0,
            error=exc,
        )


def _request_noaa_text_feed(
    endpoint: str,
    *,
    name: str,
    params: dict[str, object],
    session: requests.Session,
    timeout_seconds: int,
    fetched_at_utc: dt.datetime,
) -> tuple[str, FeedStatus]:
    """Request a NOAA text endpoint and return text plus feed status."""

    try:
        response = session.get(
            f"{NOAA_API_BASE_URL}/{endpoint}",
            params=params,
            timeout=timeout_seconds,
        )
        if getattr(response, "status_code", None) == 204:
            return "", _build_feed_status(
                name=name,
                endpoint=endpoint,
                params=params,
                fetched_at_utc=fetched_at_utc,
                row_count=0,
            )
        response.raise_for_status()
        text = str(getattr(response, "text", "") or "")
        return text, _build_feed_status(
            name=name,
            endpoint=endpoint,
            params=params,
            fetched_at_utc=fetched_at_utc,
            row_count=1 if text.strip() else 0,
        )
    except Exception as exc:
        return "", _build_feed_status(
            name=name,
            endpoint=endpoint,
            params=params,
            fetched_at_utc=fetched_at_utc,
            row_count=0,
            error=exc,
        )


def _request_noaa_geojson_feed(
    endpoint: str,
    *,
    name: str,
    params: dict[str, object],
    session: requests.Session,
    timeout_seconds: int,
    fetched_at_utc: dt.datetime,
) -> tuple[dict[str, object], FeedStatus]:
    """Request a NOAA GeoJSON endpoint and return payload plus feed status."""

    try:
        response = session.get(
            f"{NOAA_API_BASE_URL}/{endpoint}",
            params=params,
            timeout=timeout_seconds,
        )
        if getattr(response, "status_code", None) == 204:
            return {}, _build_feed_status(
                name=name,
                endpoint=endpoint,
                params=params,
                fetched_at_utc=fetched_at_utc,
                row_count=0,
            )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            payload = {}
        features = payload.get("features") if isinstance(payload.get("features"), list) else []
        return payload, _build_feed_status(
            name=name,
            endpoint=endpoint,
            params=params,
            fetched_at_utc=fetched_at_utc,
            row_count=len(features),
        )
    except Exception as exc:
        return {}, _build_feed_status(
            name=name,
            endpoint=endpoint,
            params=params,
            fetched_at_utc=fetched_at_utc,
            row_count=0,
            error=exc,
        )


def _request_noaa_feed_job(
    kind: str,
    endpoint: str,
    name: str,
    params: dict[str, object],
    *,
    session: requests.Session | None,
    timeout_seconds: int,
    fetched_at_utc: dt.datetime,
) -> tuple[object, FeedStatus]:
    """Run one independent NOAA request, owning a live worker session when needed."""

    worker_session = session or requests.Session()
    try:
        if kind == "json":
            return _request_noaa_json_feed(
                endpoint,
                name=name,
                params=params,
                session=worker_session,
                timeout_seconds=timeout_seconds,
                fetched_at_utc=fetched_at_utc,
            )
        if kind == "text":
            return _request_noaa_text_feed(
                endpoint,
                name=name,
                params=params,
                session=worker_session,
                timeout_seconds=timeout_seconds,
                fetched_at_utc=fetched_at_utc,
            )
        if kind == "geojson":
            return _request_noaa_geojson_feed(
                endpoint,
                name=name,
                params=params,
                session=worker_session,
                timeout_seconds=timeout_seconds,
                fetched_at_utc=fetched_at_utc,
            )
        raise ValueError(f"Unsupported NOAA feed kind: {kind}")
    finally:
        # An injected session belongs to its caller. Live parallel workers each
        # own and close their session so requests never share mutable state.
        if session is None:
            worker_session.close()


def _derive_noaa_confidence(feed_statuses: dict[str, FeedStatus]) -> str:
    """Estimate data confidence from critical feed availability without penalizing missing PIREPs."""

    # PIREPs/AIREPs are opportunistic observations. Their absence should not lower overall feed
    # confidence because many valid routes and time windows simply have no recent reports.
    critical_names = ("metar", "taf", "windtemp", "gairmet", "airsigmet", "tcf", "cwa")
    critical = [feed_statuses[name] for name in critical_names if name in feed_statuses]
    if not critical:
        return "Unknown"
    failed_count = sum(1 for status in critical if status.status == "failed")
    if failed_count >= 3:
        return "Unknown"
    if failed_count:
        return "Low"
    if any(status.status == "partial" for status in critical):
        return "Medium"
    # Empty hazard/advisory feeds are often the expected clear-weather result.
    # Empty requested terminal or wind data, however, reduces planning confidence.
    if any(
        feed_statuses[name].status == "empty"
        for name in ("metar", "taf", "windtemp")
        if name in feed_statuses
    ):
        return "Medium"
    return "High"


def _pirep_query_params_for_airports(icaos: list[str]) -> dict[str, object]:
    """Build the spatially constrained PIREP/AIREP query AWC requires."""

    locations = []
    for code in icaos:
        airport_info = _lookup_airport_from_builtin_db(code)
        if airport_info is None:
            continue
        locations.append((airport_info.latitude, airport_info.longitude))

    if locations:
        latitudes = [latitude for latitude, _longitude in locations]
        longitudes = [longitude for _latitude, longitude in locations]
        # A modest bbox covers the terminal/route neighborhood without triggering an invalid
        # unconstrained request; route-specific filtering still happens later in hazard scoring.
        padding_deg = PIREP_BBOX_PADDING_SINGLE_DEG if len(locations) == 1 else PIREP_BBOX_PADDING_MULTI_DEG
        min_lon = max(min(longitudes) - padding_deg, -180.0)
        min_lat = max(min(latitudes) - padding_deg, -90.0)
        max_lon = min(max(longitudes) + padding_deg, 180.0)
        max_lat = min(max(latitudes) + padding_deg, 90.0)
        return {
            "format": "json",
            "hours": "3",
            "bbox": f"{min_lon:.3f},{min_lat:.3f},{max_lon:.3f},{max_lat:.3f}",
        }

    if icaos:
        return {"format": "json", "hours": "3", "id": icaos[0], "distance": "250"}
    return {"format": "json", "hours": "3"}


def select_windtemp_forecast_cycle(
    target_time_utc: dt.datetime | None,
    *,
    now_utc: dt.datetime | None = None,
) -> str:
    """Pick the nearest supported FD forecast horizon for the requested route time."""

    if target_time_utc is None:
        return "06"
    if target_time_utc.tzinfo is None:
        target_time_utc = target_time_utc.replace(tzinfo=dt.timezone.utc)
    reference_time = now_utc or dt.datetime.now(dt.timezone.utc)
    if reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=dt.timezone.utc)
    hours_from_now = (
        target_time_utc.astimezone(dt.timezone.utc) - reference_time.astimezone(dt.timezone.utc)
    ).total_seconds() / 3600.0
    if hours_from_now <= 9:
        return "06"
    if hours_from_now <= 18:
        return "12"
    return "24"


def _closest_utc_day_time(reference: dt.datetime, day: int, hour: int, minute: int) -> dt.datetime:
    """Resolve an aviation DDHHMMZ token to the nearest plausible UTC month."""

    reference = reference.astimezone(dt.timezone.utc)
    candidates: list[dt.datetime] = []
    for month_offset in (-1, 0, 1):
        month_index = (reference.year * 12 + reference.month - 1) + month_offset
        year, zero_month = divmod(month_index, 12)
        try:
            candidates.append(dt.datetime(year, zero_month + 1, day, hour, minute, tzinfo=dt.timezone.utc))
        except ValueError:
            continue
    if not candidates:
        raise ValueError("Invalid aviation date token")
    return min(candidates, key=lambda value: abs((value - reference).total_seconds()))


def parse_windtemp_product_times(
    product_text: str,
    *,
    fetched_at_utc: dt.datetime,
) -> tuple[dt.datetime | None, dt.datetime | None, dt.datetime | None]:
    """Parse FD issue and FOR-USE provenance from the textual product header."""

    issue_match = re.search(r"DATA\s+BASED\s+ON\s+(\d{2})(\d{2})(\d{2})Z", product_text, re.IGNORECASE)
    use_match = re.search(r"FOR\s+USE\s+(\d{2})(\d{2})-(\d{2})(\d{2})Z", product_text, re.IGNORECASE)
    issue_time = None
    valid_from = None
    valid_to = None
    if issue_match:
        issue_time = _closest_utc_day_time(
            fetched_at_utc,
            int(issue_match.group(1)),
            int(issue_match.group(2)),
            int(issue_match.group(3)),
        )
    if use_match:
        anchor = issue_time or fetched_at_utc.astimezone(dt.timezone.utc)
        valid_from = anchor.replace(hour=int(use_match.group(1)), minute=int(use_match.group(2)), second=0, microsecond=0)
        valid_to = anchor.replace(hour=int(use_match.group(3)), minute=int(use_match.group(4)), second=0, microsecond=0)
        if valid_from < anchor - dt.timedelta(hours=12):
            valid_from += dt.timedelta(days=1)
        if valid_from > anchor + dt.timedelta(hours=12):
            valid_from -= dt.timedelta(days=1)
        while valid_to <= valid_from:
            valid_to += dt.timedelta(days=1)
    return issue_time, valid_from, valid_to


def fetch_noaa_weather(
    icaos: list[str],
    *,
    windtemp_region: str = "us",
    windtemp_level: str = "low",
    windtemp_fcst: str = "06",
    session: requests.Session | None = None,
    timeout_seconds: int = 8,
) -> NoaaWeather:
    """Fetch and normalize the live NOAA inputs required by the mission brief."""

    codes = [normalize_icao(code) for code in icaos if normalize_icao(code)]
    normalized_codes = list(dict.fromkeys(codes))

    airports: dict[str, AirportWeather] = {
        code: AirportWeather(
            icao=code,
            metar_raw=None,
            metar_time_utc=None,
            flight_category=None,
            metar_summary=None,
            taf_raw=None,
            taf_issue_time_utc=None,
            taf_summary=None,
        )
        for code in normalized_codes
    }

    fetched_at_utc = dt.datetime.now(dt.timezone.utc)

    metar_rows: list[dict[str, object]] = []
    taf_rows: list[dict[str, object]] = []
    gairmet_rows: list[dict[str, object]] = []
    airsigmet_rows: list[dict[str, object]] = []
    tcf_payload: dict[str, object] = {}
    cwa_payload: dict[str, object] = {}
    pirep_rows: list[dict[str, object]] = []
    feed_statuses: dict[str, FeedStatus] = {}

    windtemp_regions = [region.strip() for region in windtemp_region.split(",") if region.strip()]
    windtemp_regions = list(dict.fromkeys(windtemp_regions or ["us"]))
    jobs: dict[str, tuple[str, str, str, dict[str, object]]] = {}
    if normalized_codes:
        joined = ",".join(normalized_codes)
        jobs["metar"] = ("json", "metar", "METAR", {"ids": joined, "format": "json", "hours": "3"})
        jobs["taf"] = ("json", "taf", "TAF", {"ids": joined, "format": "json"})
    for index, region in enumerate(windtemp_regions):
        jobs[f"windtemp:{index}"] = (
            "text",
            "windtemp",
            "FD winds/temps",
            {"region": region, "level": windtemp_level, "fcst": windtemp_fcst},
        )
    jobs.update(
        {
            "gairmet": ("json", "gairmet", "G-AIRMET", {"format": "json"}),
            "airsigmet": ("json", "airsigmet", "AIRSIGMET", {"format": "json"}),
            "tcf": ("geojson", "tcf", "TCF", {"format": "geojson"}),
            "cwa": ("geojson", "cwa", "CWA", {"format": "geojson"}),
            "pirep": (
                "json",
                "pirep",
                "PIREP/AIREP",
                _pirep_query_params_for_airports(normalized_codes),
            ),
        }
    )

    def run_job(job: tuple[str, str, str, dict[str, object]]) -> tuple[object, FeedStatus]:
        kind, endpoint, name, params = job
        return _request_noaa_feed_job(
            kind,
            endpoint,
            name,
            params,
            session=session,
            timeout_seconds=timeout_seconds,
            fetched_at_utc=fetched_at_utc,
        )

    if session is None:
        # Live feeds are independent. Fetch them concurrently with one Session
        # per worker; injected fake/custom sessions remain deterministic below.
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as executor:
            keys = list(jobs)
            values = executor.map(run_job, (jobs[key] for key in keys))
            results = dict(zip(keys, values))
    else:
        results = {key: run_job(job) for key, job in jobs.items()}

    def rows_result(key: str) -> tuple[list[dict[str, object]], FeedStatus]:
        payload, status = results[key]
        if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
            raise TypeError(f"NOAA {key} result was not a list of objects")
        return payload, status

    def object_result(key: str) -> tuple[dict[str, object], FeedStatus]:
        payload, status = results[key]
        if not isinstance(payload, dict):
            raise TypeError(f"NOAA {key} result was not an object")
        return payload, status

    def text_result(key: str) -> tuple[str, FeedStatus]:
        payload, status = results[key]
        if not isinstance(payload, str):
            raise TypeError(f"NOAA {key} result was not text")
        return payload, status

    if normalized_codes:
        metar_rows, feed_statuses["metar"] = rows_result("metar")
        taf_rows, feed_statuses["taf"] = rows_result("taf")
    else:
        feed_statuses["metar"] = _build_feed_status(
            name="METAR",
            endpoint="metar",
            params={"ids": "", "format": "json", "hours": "3"},
            fetched_at_utc=fetched_at_utc,
            row_count=0,
        )
        feed_statuses["taf"] = _build_feed_status(
            name="TAF",
            endpoint="taf",
            params={"ids": "", "format": "json"},
            fetched_at_utc=fetched_at_utc,
            row_count=0,
        )

    windtemp_points: list[WindTempPoint] = []
    windtemp_statuses: list[FeedStatus] = []
    for index, region in enumerate(windtemp_regions):
        region_text, region_status = text_result(f"windtemp:{index}")
        issue_time, valid_from, valid_to = parse_windtemp_product_times(
            region_text,
            fetched_at_utc=fetched_at_utc,
        )
        region_status = replace(
            region_status,
            issue_time_utc=issue_time,
            valid_from_utc=valid_from,
            valid_to_utc=valid_to,
        )
        windtemp_statuses.append(region_status)
        windtemp_points.extend(parse_windtemp_text(region_text))
    unique_windtemp_points = {
        (point.station, point.altitude_ft): point for point in windtemp_points
    }
    windtemp_points = list(unique_windtemp_points.values())
    failed_regions = [
        region
        for region, status in zip(windtemp_regions, windtemp_statuses)
        if status.status == "failed"
    ]
    aggregate_status = (
        ("partial" if failed_regions else "ok")
        if windtemp_points
        else ("failed" if len(failed_regions) == len(windtemp_regions) else "empty")
    )
    successful_statuses = [status for status in windtemp_statuses if status.status != "failed"]
    provenance_status = successful_statuses[0] if successful_statuses else windtemp_statuses[0]
    issue_times = [status.issue_time_utc for status in successful_statuses if status.issue_time_utc]
    valid_from_times = [status.valid_from_utc for status in successful_statuses if status.valid_from_utc]
    valid_to_times = [status.valid_to_utc for status in successful_statuses if status.valid_to_utc]
    feed_statuses["windtemp"] = replace(
        provenance_status,
        params={"regions": windtemp_regions, "level": windtemp_level, "fcst": windtemp_fcst},
        row_count=len(windtemp_points),
        status=aggregate_status,
        error_message=(f"Failed regions: {', '.join(failed_regions)}" if failed_regions else None),
        issue_time_utc=max(issue_times) if issue_times else None,
        valid_from_utc=max(valid_from_times) if valid_from_times else None,
        valid_to_utc=min(valid_to_times) if valid_to_times else None,
    )

    gairmet_rows, feed_statuses["gairmet"] = rows_result("gairmet")
    airsigmet_rows, feed_statuses["airsigmet"] = rows_result("airsigmet")
    tcf_payload, feed_statuses["tcf"] = object_result("tcf")
    cwa_payload, feed_statuses["cwa"] = object_result("cwa")
    pirep_rows, feed_statuses["pirep"] = rows_result("pirep")
    feed_statuses["gfa_fip_gtg"] = _build_feed_status(
        name="GFA/FIP/GTG",
        endpoint="gfa",
        params={"evaluation": "not listed as a public AWC Data API product"},
        fetched_at_utc=fetched_at_utc,
        row_count=0,
        error=RuntimeError("No public AWC Data API endpoint is listed for GFA/FIP/GTG gridded layers."),
    )

    metar_latest = _coerce_latest_rows(metar_rows, score_field="obsTime")
    taf_latest = _coerce_latest_rows(taf_rows, score_field="validTimeFrom")
    timezone_by_code: dict[str, str | None] = {}
    for code in normalized_codes:
        airport_info = _lookup_airport_from_builtin_db(code)
        timezone_by_code[code] = airport_info.timezone if airport_info else None

    for code in normalized_codes:
        metar = metar_latest.get(code, {})
        taf = taf_latest.get(code, {})
        timezone_name = timezone_by_code.get(code)
        airports[code] = AirportWeather(
            icao=code,
            metar_raw=str(metar.get("rawOb")) if metar.get("rawOb") else None,
            metar_time_utc=str(metar.get("reportTime")) if metar.get("reportTime") else None,
            flight_category=str(metar.get("fltCat")) if metar.get("fltCat") else None,
            metar_summary=_summarize_metar_row(metar, timezone_name=timezone_name) if metar else None,
            taf_raw=str(taf.get("rawTAF")) if taf.get("rawTAF") else None,
            taf_issue_time_utc=str(taf.get("issueTime")) if taf.get("issueTime") else None,
            taf_summary=_summarize_taf_row(taf, timezone_name=timezone_name) if taf else None,
            metar_risk=_terminal_risk_from_metar_row(metar),
            taf_risk=_terminal_risk_from_taf_row(taf),
            metar_observed_at_utc=_parse_epoch_utc(metar.get("obsTime")) or _parse_iso_utc(metar.get("reportTime")),
            metar_ceiling_ft=_lowest_ceiling_ft(
                cover=metar.get("cover"),
                clouds=metar.get("clouds"),
                vertical_visibility=metar.get("vertVis"),
            ),
            metar_visibility_sm=_parse_visibility_sm(metar.get("visib")),
            metar_wind_speed_kt=_safe_int(metar.get("wspd")),
            metar_wind_gust_kt=_safe_int(metar.get("wgst")),
            metar_weather=str(metar.get("wxString") or "").strip().upper() or None,
            taf_periods=_taf_periods_from_row(taf) if taf else (),
        )

    return NoaaWeather(
        airports=airports,
        windtemps=windtemp_points,
        windtemp_region=windtemp_region,
        windtemp_level=windtemp_level,
        windtemp_fcst=windtemp_fcst,
        hazard_areas=_parse_hazard_areas(
            gairmet_rows=gairmet_rows,
            airsigmet_rows=airsigmet_rows,
            tcf_payload=tcf_payload,
            cwa_payload=cwa_payload,
            pirep_rows=pirep_rows,
        ),
        feed_statuses=feed_statuses,
        data_confidence=_derive_noaa_confidence(feed_statuses),
    )


def great_circle_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute distance in nautical miles using the shared route geometry helper."""

    return _route_great_circle_distance_nm(lat1, lon1, lat2, lon2)


def _initial_track_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Compute initial true course using the shared route geometry helper."""

    return _route_initial_track_deg(lat1, lon1, lat2, lon2)


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
    wrapped_delta = ((destination.longitude - departure.longitude + 540.0) % 360.0) - 180.0
    is_westbound = wrapped_delta < 0

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
    wrapped_delta = ((destination.longitude - departure.longitude + 540.0) % 360.0) - 180.0
    is_westbound = wrapped_delta < 0
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

    low_ft = min(lower_altitude_ft, upper_altitude_ft)
    high_ft = max(lower_altitude_ft, upper_altitude_ft)
    if high_ft <= low_ft:
        return 0.0, 0.0, 0.0

    ordered_boundaries_ft = _phase_altitude_boundaries_ft(
        lower_altitude_ft=lower_altitude_ft,
        upper_altitude_ft=upper_altitude_ft,
        rows=rows,
    )
    total_hours = 0.0
    total_distance_nm = 0.0
    total_fuel_gal = 0.0

    for segment_low_ft, segment_high_ft in zip(ordered_boundaries_ft, ordered_boundaries_ft[1:]):
        if segment_high_ft <= segment_low_ft:
            continue

        midpoint_altitude_ft = (segment_low_ft + segment_high_ft) / 2.0
        row = _vertical_row_for_altitude(rows, midpoint_altitude_ft)
        ias_kts = float(row.ias_kts if row else fallback_ias_kts)
        rate_fpm = max(int(row.rate_fpm if row else fallback_rate_fpm), 100)
        fuel_gph = float(row.fuel_gph if row else fallback_fuel_gph)
        tas_kts = (
            _ias_to_tas(ias_kts, midpoint_altitude_ft)
            if prefer_nominal_ias_tas
            else (
                float(row.tas_kts)
                if row and row.tas_kts is not None
                else _ias_to_tas(ias_kts, midpoint_altitude_ft)
            )
        )

        segment_hours = (segment_high_ft - segment_low_ft) / rate_fpm / 60.0
        segment_ground_speed = _along_track_ground_speed(
            true_airspeed_kts=tas_kts,
            tailwind_kts=wind_kt,
            crosswind_kts=crosswind_kt,
            vertical_rate_fpm=rate_fpm,
        )
        total_hours += segment_hours
        total_distance_nm += segment_hours * segment_ground_speed
        total_fuel_gal += segment_hours * fuel_gph

    return total_hours, total_distance_nm, total_fuel_gal


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
        segment_winds=segment_winds,
        climb_wind=climb_wind,
        descent_wind=descent_wind,
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
    # Displayed planning quantities round conservatively so sub-unit fractions never look optimistic.
    fuel_burn = int(math.ceil(profile.total_fuel_gal))
    fuel_at_dest = int(start_fuel_gal - fuel_burn)
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
    calculated_required_landing_fuel_gal = alternate_fuel_gal + reserve_fuel_gal
    bounded_landing_minimum_gal = int(math.ceil(max(float(landing_minimum_gal), 0.0)))
    bounded_reserve_floor_gal = (
        int(math.ceil(max(float(reserve_floor_gal), 0.0)))
        if reserve_floor_gal is not None
        else 0
    )
    # Decision (Jack, 2026-07-20, FIX-07): the landing minimum is a floor protecting
    # arrival at the INTENDED destination; a diversion draws it down en route to the
    # alternate. The alternative reading (alt_fuel + max(reserve, landing_min), i.e. a
    # floor at final touchdown including a diversion) was considered and rejected.
    required_landing_fuel_gal = max(
        calculated_required_landing_fuel_gal,
        bounded_landing_minimum_gal,
        bounded_reserve_floor_gal,
    )
    reserve_margin_gal = fuel_at_dest - required_landing_fuel_gal
    if reserve_margin_gal < 0:
        fuel_status = "Below reserve"
    elif bounded_reserve_floor_gal > max(calculated_required_landing_fuel_gal, bounded_landing_minimum_gal):
        fuel_status = "Meets pilot floor"
    elif bounded_landing_minimum_gal > calculated_required_landing_fuel_gal:
        fuel_status = "Meets landing minimum"
    else:
        fuel_status = "Meets reserve"
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
        fuel_burn=fuel_burn,
        fuel_at_dest=fuel_at_dest,
        airborne_hours=profile.total_hours,
        alternate_fuel_gal=alternate_fuel_gal,
        reserve_fuel_gal=reserve_fuel_gal,
        calculated_required_landing_fuel_gal=calculated_required_landing_fuel_gal,
        reserve_floor_gal=bounded_reserve_floor_gal,
        required_landing_fuel_gal=required_landing_fuel_gal,
        reserve_margin_gal=reserve_margin_gal,
        fuel_status=fuel_status,
        performance_limit_notes=profile.performance_limit_notes,
    )
    return point, profile.avg_wind


def build_mission_brief(
    departure: AirportData,
    destination: AirportData,
    *,
    departure_date: dt.date,
    departure_time_local: dt.time,
    is_return_leg: bool,
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
) -> MissionBrief:
    """Assemble the mission brief table for the requested route and ETD."""

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
    baseline_wind_knots = 0
    active_flight_levels = list(flight_levels or FLIGHT_LEVELS)
    baseline_level = 280 if 280 in active_flight_levels else active_flight_levels[len(active_flight_levels) // 2]

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
        if fl == baseline_level:
            baseline_wind_knots = avg_wind

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
        baseline_wind_knots=baseline_wind_knots,
        points=points,
    )
