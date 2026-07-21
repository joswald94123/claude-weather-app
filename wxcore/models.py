"""Shared constants, planning thresholds, and frozen dataclasses for the mission core."""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass, field




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
FUEL_BURN_GPH = 57.0
FIXED_FUEL_GAL = 8
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
class FuelLedger:
    """The complete landing-fuel derivation for one planned flight level.

    Every displayed fuel quantity is a projection of this ledger, so the numbers
    can never disagree with one another: total_burn_gal = ceil(taxi + climb +
    cruise + descent); fob_at_landing_gal = start - total burn;
    effective_requirement_gal = max(alternate + reserve, landing minimum, pilot
    floor); reserve_margin_gal = FOB - effective requirement.
    """

    start_fuel_gal: int
    taxi_fuel_gal: float
    climb_fuel_gal: float
    cruise_fuel_gal: float
    descent_fuel_gal: float
    total_burn_gal: int
    fob_at_landing_gal: int
    alternate_fuel_gal: int
    reserve_fuel_gal: int
    alternate_plus_reserve_gal: int
    landing_minimum_gal: int
    pilot_floor_gal: int
    effective_requirement_gal: int
    reserve_margin_gal: int
    fuel_status: str


def build_fuel_ledger(
    *,
    start_fuel_gal: int,
    taxi_fuel_gal: float,
    climb_fuel_gal: float,
    cruise_fuel_gal: float,
    descent_fuel_gal: float,
    alternate_fuel_gal: int,
    reserve_fuel_gal: int,
    landing_minimum_gal: int,
    pilot_floor_gal: int,
) -> FuelLedger:
    """Derive burn, FOB, requirement, margin, and status from the raw components."""

    total_burn_gal = int(
        math.ceil(taxi_fuel_gal + climb_fuel_gal + cruise_fuel_gal + descent_fuel_gal)
    )
    fob_at_landing_gal = int(start_fuel_gal - total_burn_gal)
    alternate_plus_reserve_gal = alternate_fuel_gal + reserve_fuel_gal
    # Decision (Jack, 2026-07-20, FIX-07): the landing minimum is a floor protecting
    # arrival at the INTENDED destination; a diversion draws it down en route to the
    # alternate. The alternative reading (alt_fuel + max(reserve, landing_min), i.e. a
    # floor at final touchdown including a diversion) was considered and rejected.
    effective_requirement_gal = max(
        alternate_plus_reserve_gal,
        landing_minimum_gal,
        pilot_floor_gal,
    )
    reserve_margin_gal = fob_at_landing_gal - effective_requirement_gal
    if reserve_margin_gal < 0:
        fuel_status = "Below reserve"
    elif pilot_floor_gal > max(alternate_plus_reserve_gal, landing_minimum_gal):
        fuel_status = "Meets pilot floor"
    elif landing_minimum_gal > alternate_plus_reserve_gal:
        fuel_status = "Meets landing minimum"
    else:
        fuel_status = "Meets reserve"
    return FuelLedger(
        start_fuel_gal=start_fuel_gal,
        taxi_fuel_gal=taxi_fuel_gal,
        climb_fuel_gal=climb_fuel_gal,
        cruise_fuel_gal=cruise_fuel_gal,
        descent_fuel_gal=descent_fuel_gal,
        total_burn_gal=total_burn_gal,
        fob_at_landing_gal=fob_at_landing_gal,
        alternate_fuel_gal=alternate_fuel_gal,
        reserve_fuel_gal=reserve_fuel_gal,
        alternate_plus_reserve_gal=alternate_plus_reserve_gal,
        landing_minimum_gal=landing_minimum_gal,
        pilot_floor_gal=pilot_floor_gal,
        effective_requirement_gal=effective_requirement_gal,
        reserve_margin_gal=reserve_margin_gal,
        fuel_status=fuel_status,
    )


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
    fuel_ledger: FuelLedger | None = None


@dataclass(frozen=True)
class MissionBrief:
    """User-facing mission summary for a route and a set of flight levels."""

    route_label: str
    departure_zone_time: str
    distance_nm: int
    direction_label: str
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


def is_westbound_route(departure: AirportData, destination: AirportData) -> bool:
    """Classify route direction by the wrapped longitude delta."""

    wrapped_delta = ((destination.longitude - departure.longitude + 540.0) % 360.0) - 180.0
    return wrapped_delta < 0


def preferred_baseline_flight_level(levels: list[int]) -> int:
    """Pick the comparison/preview altitude: FL280 when available, else the middle."""

    return 280 if 280 in levels else levels[len(levels) // 2]


def cruise_flight_levels_for_direction(*, is_westbound: bool) -> list[int]:
    """Return the standard TBM cruise levels for the route direction."""

    return WESTBOUND_CRUISE_LEVELS if is_westbound else EASTBOUND_CRUISE_LEVELS


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
