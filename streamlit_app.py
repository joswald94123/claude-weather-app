"""Streamlit UI for route setup, mission outputs, and NOAA evidence review."""

from __future__ import annotations

import datetime as dt
import html
import importlib
import math
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path

import deployment_bootstrap as _deployment_bootstrap

# Streamlit Community Cloud can execute this updated entrypoint inside the
# previous release's Python process. Reload the small bootstrap itself, then
# evict every cached repo module before any application imports are resolved.
importlib.reload(_deployment_bootstrap)
_APP_ROOT = Path(__file__).resolve().parent
# Same precedence as app_version.resolve_running_version: env override first, then
# the file, degrading to a sentinel instead of crashing every rerun at startup.
_APP_RELEASE = os.getenv("APP_RELEASE_VERSION", "").strip()
if not _APP_RELEASE:
    try:
        _APP_RELEASE = (_APP_ROOT / "RELEASE_VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        _APP_RELEASE = "unversioned"
_deployment_bootstrap.refresh_repo_modules_for_release(
    repo_root=_APP_ROOT,
    release=_APP_RELEASE,
    protected_paths=(Path(__file__),),
)

import pandas as pd  # noqa: E402 - deployment cache eviction must run first
import pytz  # noqa: E402 - deployment cache eviction must run first
import streamlit as st  # noqa: E402 - deployment cache eviction must run first
import streamlit.components.v1 as components  # noqa: E402 - deployment cache eviction must run first

from app_version import resolve_python_version, resolve_running_version  # noqa: E402
from faa_waypoints import FaaWaypoint, resolve_faa_waypoint  # noqa: E402
from daher_pim_tables import (  # noqa: E402
    CLIMB_SCHEDULE_METADATA,
    CLIMB_WEIGHTS_LB,
    CRUISE_MODE_METADATA,
    CRUISE_WEIGHTS_LB,
    DESCENT_PROFILE_METADATA,
    DESCENT_RATES_FPM,
    SOURCE_METADATA,
    climb_rows_for_weight,
    cruise_rows_for_weight,
    descent_rows_for_rate,
    list_available_climb_temperature_offsets,
    list_available_cruise_temperature_offsets,
)
from performance_profiles import (  # noqa: E402
    DEFAULT_STARTUP_TAXI_FUEL_GAL,
    DEFAULT_PERFORMANCE_PROFILE_ID,
    OFFICIAL_PIM_PROFILE,
    get_performance_profile,
    resolve_climb_schedule,
    resolve_cruise_mode,
    resolve_descent_profile,
    sample_cruise_performance,
)
from route_context_map import build_range_inset_svg, build_route_context_svg  # noqa: E402
import route_planning as _route_planning  # noqa: E402
import route_vertical_profile as _route_vertical_profile  # noqa: E402
from tail_profiles import (  # noqa: E402
    JET_A_POUNDS_PER_GALLON,
    MAX_USABLE_FUEL_GAL,
    TailProfile,
    calibration_deltas_pct,
    compute_planning_weights,
    deserialize_tail_profile,
    gallons_from_pounds,
    serialize_tail_profile,
)
from ui_presenters import (  # noqa: E402
    default_departure_time,
    is_departure_time_stale,
    landing_fuel_presentation,
)
from weather_core import (  # noqa: E402
    AirportData,
    FLIGHT_LEVELS,
    MissionRiskThresholds,
    build_alternate_range_rings,
    build_mission_brief_document,
    build_route_vertical_profile,
    cruise_flight_levels_for_direction,
    fetch_noaa_weather,
    get_airport_data,
    hazard_label,
    is_westbound_route,
    preferred_baseline_flight_level,
    windtemp_cycle_correction,
    infer_windtemp_region,
    normalize_icao,
    select_windtemp_forecast_cycle,
    summarize_segment_hazard,
)

# Streamlit Community Cloud may re-execute this entrypoint in the existing
# interpreter immediately after a Git pull. Refresh only an older cached route
# module that predates exports required by this release; normal reruns and clean
# starts leave the already-current module untouched.
_REQUIRED_ROUTE_PLANNING_EXPORTS = {
    "destination_arrival_fuel_gal",
    "resolve_mission_headline",
}
if not _REQUIRED_ROUTE_PLANNING_EXPORTS.issubset(vars(_route_planning)):
    importlib.reload(_route_planning)

_REQUIRED_ROUTE_VERTICAL_PROFILE_EXPORTS = {
    "build_interactive_route_vertical_profile_html",
    "build_route_vertical_profile_svg",
}
if not _REQUIRED_ROUTE_VERTICAL_PROFILE_EXPORTS.issubset(vars(_route_vertical_profile)):
    importlib.reload(_route_vertical_profile)

# Bind route-planning names only after the compatibility refresh so an in-process
# deployment cannot fail while resolving a newly introduced module export.
RoutePlan = _route_planning.RoutePlan
RouteWaypoint = _route_planning.RouteWaypoint
build_route_plan = _route_planning.build_route_plan
destination_arrival_fuel_gal = _route_planning.destination_arrival_fuel_gal
parse_airborne_ete = _route_planning.parse_airborne_ete
normalize_route_tokens = _route_planning.normalize_route_tokens
route_progress_warning = _route_planning.route_progress_warning
build_interactive_route_vertical_profile_html = (
    _route_vertical_profile.build_interactive_route_vertical_profile_html
)
build_route_vertical_profile_svg = _route_vertical_profile.build_route_vertical_profile_svg


@dataclass(frozen=True)
class UiAirportValidation:
    """Resolved airport metadata plus FAA validation state for the sidebar inputs."""

    airport: AirportData | None
    faa_waypoint: FaaWaypoint | None
    cycle_label: str | None
    lookup_error: str | None


@dataclass(frozen=True)
class UiRouteResolution:
    """Resolved route plan plus UI-facing warnings for custom waypoint entry."""

    route_plan: RoutePlan | None
    custom_route_applied: bool
    route_tokens: tuple[str, ...]
    unresolved_tokens: tuple[str, ...]
    ambiguity_notes: tuple[str, ...]
    cycle_label: str | None
    progress_warning: str | None
    lookup_error: str | None


@dataclass(frozen=True)
class UiRangeInset:
    """One waypoint-specific fuel-range inset and its calculation inputs."""

    role: str
    label: str
    airport: AirportData
    fuel_on_board_gal: float
    rings: tuple[object, ...]


@st.cache_data(ttl=300, show_spinner=False)
def _cached_faa_waypoint(
    identifier: str,
    reference_date_iso: str,
    app_release: str = "",
) -> tuple[FaaWaypoint | None, str]:
    """Resolve an FAA waypoint once per identifier, chart cycle, and app release.

    The release is part of the cache key so pickled results from a previous
    deploy cannot be unpickled against newer dataclass definitions.
    """

    reference_date = dt.date.fromisoformat(reference_date_iso)
    try:
        waypoint, cycle_urls = resolve_faa_waypoint(identifier, reference_date=reference_date)
        return waypoint, cycle_urls.effective_date.isoformat()
    except Exception as exc:
        # Cache short-lived failures so an offline Streamlit rerun does not stack
        # another 30-second FAA timeout for every route token.
        return None, f"ERROR:{str(exc).strip() or exc.__class__.__name__}"


def _uppercase_session_text(key: str, normalize_as_icao: bool = False) -> None:
    """Normalize any identifier or route-text widget through one callback path."""

    value = str(st.session_state.get(key, "") or "")
    st.session_state[key] = normalize_icao(value) if normalize_as_icao else value.upper()


def _set_departure_ksts() -> None:
    """Set the quick-pick departure airport used by the local mission workflow."""

    st.session_state.dep_icao = "KSTS"


def _reverse_route() -> None:
    """Swap departure and destination while reversing any custom route waypoints."""

    dep = normalize_icao(st.session_state.get("dep_icao", ""))
    arr = normalize_icao(st.session_state.get("arr_icao", ""))
    st.session_state.dep_icao = arr
    st.session_state.arr_icao = dep
    route_tokens = normalize_route_tokens(st.session_state.get("route_waypoints_text", ""))
    st.session_state.route_waypoints_text = " ".join(reversed(route_tokens))
    # These values describe destination-specific or directional legs and cannot
    # be safely inferred for the reversed mission.
    st.session_state.alternate_icao = ""
    st.session_state.alternate_route_waypoints_text = ""
    st.session_state.fuel_stop_alternate_text = ""
    st.session_state.reverse_route_notice = True


def _set_etd_now_plus_15() -> None:
    """Move the ETD controls to the next five-minute mark at least 15 minutes ahead."""

    timezone_name = st.session_state.get("etd_timezone_name", "UTC")
    try:
        timezone = pytz.timezone(timezone_name)
    except Exception:
        timezone = pytz.UTC
    selected = default_departure_time(dt.datetime.now(timezone))
    st.session_state.etd_date = selected.date()
    st.session_state.etd_hour = selected.hour % 12 or 12
    st.session_state.etd_minute = f"{selected.minute:02d}"
    st.session_state.etd_ampm = "AM" if selected.hour < 12 else "PM"


def _is_valid_icao(code: str) -> bool:
    """Return whether text has the basic shape of a four-character airport code."""

    return len(code) == 4 and code.isalnum()


def _resolve_if_valid_icao(code: str) -> AirportData | None:
    """Resolve an airport only after cheap syntax validation succeeds."""

    if not _is_valid_icao(code):
        return None

    try:
        airport = get_airport_data(code)
    except ValueError:
        return None
    return airport


def _apply_faa_airport_coordinates(
    airport: AirportData | None,
    faa_waypoint: FaaWaypoint | None,
) -> AirportData | None:
    """Keep local timezone/elevation data while letting FAA own route coordinates."""

    if airport is None or faa_waypoint is None or faa_waypoint.waypoint_type != "Airport":
        return airport

    return AirportData(
        icao=airport.icao,
        latitude=faa_waypoint.latitude,
        longitude=faa_waypoint.longitude,
        timezone=airport.timezone,
        source=f"{airport.source}+faa",
        elevation_ft=airport.elevation_ft,
    )


def _validate_airport_input(code: str, reference_date_iso: str) -> UiAirportValidation:
    """Resolve airport metadata and FAA coordinates for one sidebar airport field."""

    if not _is_valid_icao(code):
        return UiAirportValidation(None, None, None, None)

    airport = _resolve_if_valid_icao(code)
    faa_waypoint: FaaWaypoint | None = None
    cycle_label: str | None = None
    lookup_error: str | None = None
    faa_waypoint, cycle_label = _cached_faa_waypoint(code, reference_date_iso, _APP_RELEASE)
    if cycle_label.startswith("ERROR:"):
        lookup_error = cycle_label.removeprefix("ERROR:")
        cycle_label = None

    return UiAirportValidation(
        airport=_apply_faa_airport_coordinates(airport, faa_waypoint),
        faa_waypoint=faa_waypoint,
        cycle_label=cycle_label,
        lookup_error=lookup_error,
    )


def _faa_waypoint_to_route_waypoint(waypoint: FaaWaypoint) -> RouteWaypoint:
    """Convert FAA lookup results into the route-planning waypoint model."""

    return RouteWaypoint(
        identifier=waypoint.identifier,
        latitude=waypoint.latitude,
        longitude=waypoint.longitude,
        waypoint_type=waypoint.waypoint_type,
        source=waypoint.source,
        name=waypoint.name,
    )


def _resolve_route_plan_for_ui(
    departure_airport: AirportData | None,
    destination_airport: AirportData | None,
    raw_route_text: str,
    raw_fuel_stop_text: str,
    reference_date_iso: str,
) -> UiRouteResolution:
    """Resolve custom route and fuel-stop text into a route plan plus UI warnings."""

    route_tokens = tuple(normalize_route_tokens(raw_route_text))
    fuel_stop_tokens = set(normalize_route_tokens(raw_fuel_stop_text))
    if departure_airport is None or destination_airport is None:
        return UiRouteResolution(
            route_plan=None,
            custom_route_applied=False,
            route_tokens=route_tokens,
            unresolved_tokens=(),
            ambiguity_notes=(),
            cycle_label=None,
            progress_warning=None,
            lookup_error=None,
        )

    direct_route_plan = build_route_plan(departure_airport, destination_airport)
    if not route_tokens:
        return UiRouteResolution(
            route_plan=direct_route_plan,
            custom_route_applied=False,
            route_tokens=(),
            unresolved_tokens=(),
            ambiguity_notes=(),
            cycle_label=None,
            progress_warning=None,
            lookup_error=None,
        )

    resolved_waypoints: list[RouteWaypoint] = []
    unresolved_tokens: list[str] = []
    ambiguity_notes: list[str] = []
    cycle_label: str | None = None

    for token in route_tokens:
        faa_waypoint, token_cycle_label = _cached_faa_waypoint(token, reference_date_iso, _APP_RELEASE)
        if token_cycle_label.startswith("ERROR:"):
            return UiRouteResolution(
                route_plan=direct_route_plan,
                custom_route_applied=False,
                route_tokens=route_tokens,
                unresolved_tokens=(),
                ambiguity_notes=(),
                cycle_label=cycle_label,
                progress_warning=None,
                lookup_error=token_cycle_label.removeprefix("ERROR:"),
            )

        cycle_label = token_cycle_label
        if faa_waypoint is None:
            unresolved_tokens.append(token)
            continue
        route_waypoint = _faa_waypoint_to_route_waypoint(faa_waypoint)
        if token in fuel_stop_tokens and faa_waypoint.waypoint_type == "Airport":
            route_waypoint = replace(route_waypoint, is_fuel_stop=True)
        resolved_waypoints.append(route_waypoint)
        if faa_waypoint.ambiguity_note:
            ambiguity_notes.append(f"{token}: {faa_waypoint.ambiguity_note}")

    if unresolved_tokens:
        return UiRouteResolution(
            route_plan=direct_route_plan,
            custom_route_applied=False,
            route_tokens=route_tokens,
            unresolved_tokens=tuple(unresolved_tokens),
            ambiguity_notes=tuple(ambiguity_notes),
            cycle_label=cycle_label,
            progress_warning=None,
            lookup_error=None,
        )

    route_plan = build_route_plan(
        departure_airport,
        destination_airport,
        intermediate_waypoints=resolved_waypoints,
    )
    return UiRouteResolution(
        route_plan=route_plan,
        custom_route_applied=True,
        route_tokens=route_tokens,
        unresolved_tokens=(),
        ambiguity_notes=tuple(ambiguity_notes),
        cycle_label=cycle_label,
        progress_warning=route_progress_warning(route_plan),
        lookup_error=None,
    )


def _highlight_low_fuel(value: int, landing_minimum: int) -> str:
    """Return a pandas Styler CSS rule for fuel values below the pilot minimum."""

    if isinstance(value, int) and value < landing_minimum:
        return "color: red"
    return ""


def _highlight_negative_margin(value: int) -> str:
    """Return a pandas Styler CSS rule for negative reserve margins."""

    if isinstance(value, int) and value < 0:
        return "color: red; font-weight: 700"
    return ""


def _highlight_hazard_label(value: object) -> str:
    """Color mission-matrix hazard labels without changing their underlying text."""

    label = str(value or "").strip().lower()
    if label == "high":
        return "background-color: rgba(170, 70, 55, 0.22); color: #7a241d; font-weight: 700"
    if label == "caution":
        return "background-color: rgba(183, 121, 31, 0.20); color: #76500f; font-weight: 700"
    if label == "low":
        return "background-color: rgba(15, 118, 110, 0.15); color: #0b5c56"
    return ""


def _initialize_etd_defaults_once(timezone_name: str) -> None:
    """Seed ETD controls once with a practical lead time in the departure timezone."""

    if st.session_state.get("etd_initialized"):
        return

    try:
        local_tz = pytz.timezone(timezone_name)
    except Exception:
        local_tz = pytz.timezone("UTC")

    # A next-five-minute default can expire while weather and route data load.
    # Match the explicit Now +15 action so a new mission starts in the future.
    initial_etd = default_departure_time(dt.datetime.now(local_tz))
    hour_24 = initial_etd.hour
    hour_12 = hour_24 % 12
    if hour_12 == 0:
        hour_12 = 12

    st.session_state["etd_date"] = initial_etd.date()
    st.session_state["etd_hour"] = hour_12
    st.session_state["etd_minute"] = f"{initial_etd.minute:02d}"
    st.session_state["etd_ampm"] = "AM" if hour_24 < 12 else "PM"
    st.session_state["etd_initialized"] = True
    st.session_state["etd_timezone_name"] = timezone_name


def _sync_cruise_selection(
    departure_airport: AirportData | None,
    destination_airport: AirportData | None,
) -> list[int]:
    """Keep cruise-level choices synchronized with route direction."""

    if departure_airport is None or destination_airport is None:
        return FLIGHT_LEVELS

    is_westbound = is_westbound_route(departure_airport, destination_airport)
    available_flight_levels = cruise_flight_levels_for_direction(is_westbound=is_westbound)
    default_cruise = "FL300" if is_westbound else "FL310"
    # The placeholder is a deliberate pilot choice (preview mode); only correct
    # selections that became invalid for the current route direction.
    valid_options = {f"FL{fl}" for fl in available_flight_levels} | {"Select cruise altitude"}
    if st.session_state.get("cruise_flight_level") not in valid_options:
        st.session_state["cruise_flight_level"] = default_cruise
    return available_flight_levels


def _render_wrapped_raw(text: str | None, empty_message: str) -> None:
    """Render raw METAR, TAF, and feed text without horizontal page overflow."""

    if not text:
        st.caption(empty_message)
        return

    escaped = html.escape(text)
    st.markdown(
        (
            "<pre style='white-space: pre-wrap; word-break: break-word; "
            "font-family: monospace; margin: 0;'>"
            f"{escaped}</pre>"
        ),
        unsafe_allow_html=True,
    )


def _parse_wind_knots(wind_text: str) -> int | None:
    """Parse a UI wind string such as '12k' into integer knots."""

    try:
        return int(wind_text.replace("k", "").strip())
    except Exception:
        return None


def _display_wind_knots(wind_text: str) -> int | str:
    """Display parsed wind as a number while preserving unparseable source text."""

    parsed = _parse_wind_knots(wind_text)
    if parsed is None:
        return wind_text
    return parsed


def _parse_flight_level_number(flight_level_text: str) -> int | None:
    """Parse a label such as FL300 into hundreds-of-feet flight-level units."""

    if not isinstance(flight_level_text, str):
        return None
    text = flight_level_text.strip().upper()
    if not text.startswith("FL"):
        return None
    try:
        return int(text.replace("FL", ""))
    except Exception:
        return None


def _timezone_abbrev(value: dt.datetime, timezone_name: str) -> str:
    """Return a display timezone abbreviation with a safe fallback."""

    try:
        timezone = pytz.timezone(timezone_name)
        return value.astimezone(timezone).tzname() or timezone_name
    except Exception:
        return timezone_name


def _escape_html(value: object) -> str:
    """Escape arbitrary values before embedding them in Streamlit HTML snippets."""

    return html.escape(str(value))


def _tone_class_for_score(score: int) -> str:
    """Map mission or hazard risk scores to CSS tone classes."""

    if score >= 3:
        return "tone-high"
    if score == 2:
        return "tone-moderate"
    if score == 1:
        return "tone-low"
    return "tone-clear"


def _tone_class_for_margin(margin_gal: int, thresholds: MissionRiskThresholds) -> str:
    """Map reserve margin gallons to a CSS tone class."""

    if margin_gal < thresholds.fuel_high_margin_gal:
        return "tone-high"
    if margin_gal < thresholds.fuel_caution_margin_gal:
        return "tone-moderate"
    return "tone-clear"


def _render_insight_card(
    title: str,
    value: str,
    detail: str,
    *,
    tone_class: str = "tone-clear",
) -> None:
    """Render one compact metric card in the mission summary band."""

    st.markdown(
        (
            f"<div class='brief-card {tone_class}'>"
            f"<div class='brief-card-title'>{_escape_html(title)}</div>"
            f"<div class='brief-card-value'>{_escape_html(value)}</div>"
            f"<div class='brief-card-detail'>{_escape_html(detail)}</div>"
            "</div>"
        ),
        unsafe_allow_html=True,
    )


def _confidence_tone_class(confidence: str) -> str:
    """Map feed-confidence labels to CSS tone classes."""

    return {
        "High": "tone-clear",
        "Medium": "tone-low",
        "Low": "tone-high",
        "Unknown": "tone-high",
    }.get(confidence, "tone-low")


def _parse_stop_value_assignments(raw_text: str) -> dict[str, str]:
    """Parse compact stop assignments such as KBFL=60 or KBFL:KFUL."""

    assignments: dict[str, str] = {}
    for token in re.split(r"[\s,;]+", raw_text or ""):
        if not token:
            continue
        if "=" in token:
            key, value = token.split("=", 1)
        elif ":" in token:
            key, value = token.split(":", 1)
        else:
            continue
        key = normalize_icao(key)
        value = value.strip().upper()
        if key and value:
            assignments[key] = value
    return assignments


def _parse_fuel_stop_uplifts(raw_text: str) -> dict[str, float]:
    """Parse per-stop uplift gallons keyed by fuel-stop airport identifier."""

    uplifts: dict[str, float] = {}
    for key, value in _parse_stop_value_assignments(raw_text).items():
        try:
            uplifts[key] = max(float(value), 0.0)
        except ValueError:
            continue
    return uplifts


def _parse_fuel_stop_alternates(raw_text: str) -> dict[str, str]:
    """Parse per-leg alternate ICAOs keyed by the leg destination/fuel-stop airport."""

    alternates: dict[str, str] = {}
    for key, value in _parse_stop_value_assignments(raw_text).items():
        alternate = normalize_icao(value)
        if _is_valid_icao(alternate):
            alternates[key] = alternate
    return alternates


def _build_route_hero(
    *,
    title: str,
    subtitle: str,
    pills: list[str],
    kicker: str = "Mission Brief",
    tone_class: str = "",
    pill_tones: dict[str, str] | None = None,
) -> str:
    """Build the route hero HTML block shown at the top of the page."""

    pill_tones = pill_tones or {}
    pill_markup = "".join(
        f"<span class='route-pill {pill_tones.get(pill_text, '')}'>{_escape_html(pill_text)}</span>"
        for pill_text in pills
        if pill_text
    )
    return (
        f"<div class='route-hero {tone_class}'>"
        f"<div class='route-kicker'>{_escape_html(kicker)}</div>"
        f"<div class='route-title'>{_escape_html(title)}</div>"
        f"<div class='route-subtitle'>{_escape_html(subtitle)}</div>"
        f"<div class='route-pill-row'>{pill_markup}</div>"
        "</div>"
    )



def _build_waypoint_range_rings(
    *,
    airport: AirportData,
    fuel_on_board_gal: float,
    performance_profile: object | None,
    cruise_mode_id: str | None,
    climb_schedule_id: str | None,
    descent_profile_id: str | None,
    descent_profile_rate_fpm: int | None,
    cruise_weight_lb: float | None,
    climb_weight_lb: float | None,
    wind_model: object | None,
    alt_missed_approach_fuel_gal: float,
) -> tuple[object, ...]:
    """Build waypoint range rings with the currently selected performance assumptions."""

    try:
        return build_alternate_range_rings(
            destination=airport,
            fuel_at_destination_gal=float(fuel_on_board_gal),
            performance_profile=performance_profile,
            cruise_mode_id=cruise_mode_id,
            climb_schedule_id=climb_schedule_id,
            descent_profile_id=descent_profile_id,
            descent_profile_rate_fpm=descent_profile_rate_fpm,
            cruise_weight_lb=cruise_weight_lb,
            climb_weight_lb=climb_weight_lb,
            wind_model=wind_model,
            alt_missed_approach_fuel_gal=float(alt_missed_approach_fuel_gal),
        )
    except (ValueError, TypeError, KeyError) as exc:
        st.error(f"Range rings for {airport.icao} could not be calculated: {exc}")
        return ()


def _max_hazard_score(segment_hazards: list[object], score_field: str) -> int:
    """Return the maximum score for one hazard dimension across route segments."""

    return max((int(getattr(row, score_field, 0)) for row in segment_hazards), default=0)


def _count_impacted_segments(segment_hazards: list[object]) -> int:
    """Count route segments with any known hazard impact."""

    return sum(1 for row in segment_hazards if int(getattr(row, "overall_score", 0)) > 0)


def _highlight_focus_row(row: pd.Series, focus_flight_level_text: str) -> list[str]:
    """Return table styling that highlights the currently selected flight level."""

    if row.get("FL") == focus_flight_level_text:
        return [
            "background-color: rgba(15, 118, 110, 0.18); font-weight: 700; "
            "border-top: 2px solid #0f766e; border-bottom: 2px solid #0f766e;"
            for _ in row
        ]
    return ["" for _ in row]



class _UncacheableNoaaResult(RuntimeError):
    """Carry a degraded NOAA bundle out of Streamlit caching without losing diagnostics."""

    def __init__(self, weather_bundle):
        super().__init__("Critical NOAA feeds failed; result intentionally not cached.")
        self.weather_bundle = weather_bundle


# Degraded bundles are retried quickly instead of being served for the full 15 minutes,
# while a per-session stash prevents a refetch storm on every widget interaction.
_DEGRADED_WEATHER_TTL_SECONDS = 120.0
_DEGRADED_WEATHER_STASH_KEY = "_degraded_weather_stash"


@st.cache_data(ttl=900, show_spinner=False)
def _cached_successful_noaa_weather(
    dep_icao: str,
    arr_icao: str,
    alternate_icao: str,
    additional_airports_csv: str,
    windtemp_region: str,
    windtemp_level: str,
    windtemp_fcst: str,
    etd_date_iso: str,
    etd_time_hhmm: str,
    app_release: str = "",
):
    """Cache NOAA weather for the selected airports, wind region, ETD, and release."""

    # ETD values are part of the cache key to force refresh when date/time changes;
    # the release keeps pre-deploy pickles from surviving into a new build.
    airport_codes = [dep_icao, arr_icao]
    if alternate_icao:
        airport_codes.append(alternate_icao)
    airport_codes.extend(code for code in additional_airports_csv.split(",") if code)
    weather_bundle = fetch_noaa_weather(
        airport_codes,
        windtemp_region=windtemp_region,
        windtemp_level=windtemp_level,
        windtemp_fcst=windtemp_fcst,
    )
    critical_statuses = [weather_bundle.feed_statuses.get(name) for name in ("metar", "taf", "windtemp")]
    if any(status is not None and status.status == "failed" for status in critical_statuses):
        # Streamlit does not cache exceptions, so a degraded bundle is not pinned
        # for the full TTL; the wrapper stashes it briefly per session instead.
        raise _UncacheableNoaaResult(weather_bundle)
    return weather_bundle


def _cached_noaa_weather(
    dep_icao: str,
    arr_icao: str,
    alternate_icao: str,
    additional_airports_csv: str,
    windtemp_region: str,
    windtemp_level: str,
    windtemp_fcst: str,
    etd_date_iso: str,
    etd_time_hhmm: str,
    app_release: str = "",
):
    """Cache healthy NOAA bundles long-term and degraded bundles for two minutes."""

    cache_key = (
        dep_icao,
        arr_icao,
        alternate_icao,
        additional_airports_csv,
        windtemp_region,
        windtemp_level,
        windtemp_fcst,
        etd_date_iso,
        etd_time_hhmm,
        app_release,
    )
    stash: dict = st.session_state.get(_DEGRADED_WEATHER_STASH_KEY) or {}
    now_utc = dt.datetime.now(dt.timezone.utc)
    stashed = stash.get(cache_key)
    if stashed is not None and (now_utc - stashed[0]).total_seconds() < _DEGRADED_WEATHER_TTL_SECONDS:
        return stashed[1]
    try:
        weather_bundle = _cached_successful_noaa_weather(*cache_key)
    except _UncacheableNoaaResult as exc:
        # Keyed per request so two degraded fetches in one rerun (e.g. the windtemp
        # cycle correction) each get their own short retry window; expired entries
        # are pruned so the stash cannot grow unbounded.
        stash = {
            key: entry
            for key, entry in stash.items()
            if (now_utc - entry[0]).total_seconds() < _DEGRADED_WEATHER_TTL_SECONDS
        }
        stash[cache_key] = (now_utc, exc.weather_bundle)
        st.session_state[_DEGRADED_WEATHER_STASH_KEY] = stash
        return exc.weather_bundle
    if cache_key in stash:
        stash.pop(cache_key, None)
        st.session_state[_DEGRADED_WEATHER_STASH_KEY] = stash
    return weather_bundle


# Keep the visual shell near the top so layout and styling stay easy to tune together.
st.set_page_config(page_title="TBM 960 Mission Brief", layout="wide")
st.markdown(
    """
    <style>
    :root {
        --brief-ink: #15232a;
        --brief-muted: #5b6e72;
        --brief-line: rgba(21, 35, 42, 0.10);
        --brief-surface: rgba(255, 252, 247, 0.82);
        --brief-shadow: 0 18px 50px rgba(21, 35, 42, 0.08);
        --brief-deep: #153646;
        --brief-teal: #0f766e;
        --brief-gold: #b7791f;
        --brief-red: #aa4637;
    }

    html, body, [class*="css"] {
        font-family: "Aptos", "Trebuchet MS", sans-serif;
    }

    .stApp {
        background:
            radial-gradient(circle at top left, rgba(15, 118, 110, 0.16), transparent 32%),
            radial-gradient(circle at top right, rgba(183, 121, 31, 0.14), transparent 28%),
            linear-gradient(180deg, #f7f2e9 0%, #eef3ef 52%, #f8faf6 100%);
        color: var(--brief-ink);
    }

    .block-container {
        padding-top: 1.25rem;
        padding-bottom: 2rem;
        max-width: 1380px;
    }

    [data-testid="stSidebar"] {
        background: linear-gradient(180deg, rgba(251, 245, 236, 0.96), rgba(241, 247, 243, 0.96));
        border-right: 1px solid var(--brief-line);
    }

    [data-testid="stSidebar"] * {
        color: var(--brief-ink);
    }

    .route-hero {
        position: relative;
        overflow: hidden;
        padding: 1.5rem 1.75rem;
        border-radius: 30px;
        background: linear-gradient(135deg, rgba(15, 118, 110, 0.96), rgba(21, 54, 70, 0.92));
        color: #f7f7f2;
        box-shadow: 0 24px 60px rgba(21, 35, 42, 0.18);
        margin-bottom: 1rem;
    }

    .route-hero::after {
        content: "";
        position: absolute;
        top: -110px;
        right: -30px;
        width: 260px;
        height: 260px;
        border-radius: 50%;
        background: rgba(245, 196, 102, 0.17);
    }

    .route-hero.tone-caution,
    .route-hero.tone-moderate {
        background: linear-gradient(135deg, rgba(183, 121, 31, 0.96), rgba(104, 73, 29, 0.94));
    }

    .route-hero.tone-high {
        background: linear-gradient(135deg, rgba(170, 70, 55, 0.97), rgba(92, 36, 38, 0.95));
    }

    .route-pill.tone-caution,
    .route-pill.tone-moderate {
        background: rgba(255, 222, 156, 0.30);
        border-color: rgba(255, 234, 190, 0.72);
    }

    .route-pill.tone-high {
        background: rgba(255, 194, 181, 0.32);
        border-color: rgba(255, 218, 210, 0.76);
    }

    .route-kicker {
        position: relative;
        z-index: 1;
        text-transform: uppercase;
        letter-spacing: 0.22em;
        font-size: 0.74rem;
        font-weight: 800;
        color: rgba(247, 247, 242, 0.74);
    }

    .route-title {
        position: relative;
        z-index: 1;
        margin-top: 0.35rem;
        font-family: Georgia, "Book Antiqua", serif;
        font-size: clamp(2rem, 4vw, 3.15rem);
        line-height: 1.04;
        font-weight: 700;
    }

    .route-subtitle {
        position: relative;
        z-index: 1;
        margin-top: 0.55rem;
        max-width: 62rem;
        font-size: 1rem;
        line-height: 1.5;
        color: rgba(247, 247, 242, 0.9);
    }

    .route-pill-row {
        position: relative;
        z-index: 1;
        display: flex;
        flex-wrap: wrap;
        gap: 0.55rem;
        margin-top: 1rem;
    }

    .route-pill {
        display: inline-flex;
        align-items: center;
        border-radius: 999px;
        border: 1px solid rgba(247, 247, 242, 0.22);
        background: rgba(247, 247, 242, 0.14);
        padding: 0.4rem 0.8rem;
        font-size: 0.84rem;
        font-weight: 700;
    }

    .brief-card {
        min-height: 148px;
        padding: 1rem 1.05rem;
        border-radius: 24px;
        border: 1px solid var(--brief-line);
        background: var(--brief-surface);
        box-shadow: var(--brief-shadow);
        backdrop-filter: blur(8px);
    }

    .brief-card-title {
        text-transform: uppercase;
        letter-spacing: 0.14em;
        font-size: 0.74rem;
        font-weight: 800;
        color: var(--brief-muted);
    }

    .brief-card-value {
        margin-top: 0.55rem;
        font-size: 1.62rem;
        line-height: 1.08;
        font-weight: 800;
        color: var(--brief-ink);
    }

    .brief-card-detail {
        margin-top: 0.55rem;
        font-size: 0.95rem;
        line-height: 1.4;
        color: var(--brief-muted);
    }

    .brief-card.tone-low {
        border-color: rgba(183, 121, 31, 0.20);
        background: linear-gradient(180deg, rgba(255, 248, 235, 0.92), rgba(255, 252, 247, 0.84));
    }

    .brief-card.tone-moderate {
        border-color: rgba(183, 121, 31, 0.28);
        background: linear-gradient(180deg, rgba(255, 242, 220, 0.96), rgba(255, 251, 244, 0.86));
    }

    .brief-card.tone-high {
        border-color: rgba(170, 70, 55, 0.28);
        background: linear-gradient(180deg, rgba(253, 239, 235, 0.96), rgba(255, 249, 247, 0.86));
    }

    .section-copy {
        margin: 0.15rem 0 0.85rem;
        color: var(--brief-muted);
        font-size: 0.98rem;
    }

    .route-map-shell,
    .range-inset-shell {
        margin: 0.35rem 0 1rem;
        padding: 0.55rem;
        border-radius: 28px;
        border: 1px solid var(--brief-line);
        background: linear-gradient(180deg, rgba(255, 252, 247, 0.94), rgba(244, 248, 243, 0.90));
        box-shadow: var(--brief-shadow);
        overflow: hidden;
    }

    .range-inset-shell {
        border-radius: 20px;
        padding: 0.35rem;
    }

    .route-map-shell svg,
    .range-inset-shell svg {
        display: block;
        width: 100%;
        height: auto;
    }

    .vertical-profile-shell {
        margin: 0.35rem 0 1rem;
        padding: 0.55rem;
        border-radius: 28px;
        border: 1px solid var(--brief-line);
        background: linear-gradient(180deg, rgba(255, 252, 247, 0.96), rgba(242, 248, 244, 0.92));
        box-shadow: var(--brief-shadow);
        overflow: hidden;
    }

    .vertical-profile-shell svg {
        display: block;
        width: 100%;
        height: auto;
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 0.55rem;
    }

    .stTabs [data-baseweb="tab"] {
        height: auto;
        padding: 0.55rem 1rem;
        border-radius: 999px;
        border: 1px solid var(--brief-line);
        background: rgba(255, 252, 247, 0.72);
        color: var(--brief-ink);
        font-weight: 700;
    }

    .stTabs [aria-selected="true"] {
        background: var(--brief-deep);
        border-color: var(--brief-deep);
        color: #f7f7f2;
    }

    [data-testid="stDataFrame"] {
        border: 1px solid var(--brief-line);
        border-radius: 18px;
        overflow: hidden;
        background: rgba(255, 255, 255, 0.76);
    }

    div[data-testid="stExpander"] {
        border: 1px solid var(--brief-line);
        border-radius: 18px;
        background: rgba(255, 252, 247, 0.55);
    }

    .version-badge {
        display: inline-flex;
        align-items: center;
        gap: 0.4rem;
        margin: 0 0 0.8rem;
        padding: 0.32rem 0.62rem;
        border-radius: 999px;
        border: 1px solid var(--brief-line);
        background: rgba(255, 252, 247, 0.78);
        color: var(--brief-muted);
        font-size: 0.82rem;
        font-weight: 800;
        letter-spacing: 0;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

available_flight_levels = FLIGHT_LEVELS
# Resolve once so the sidebar badge and route hero pills report the same build identifier.
running_version = resolve_running_version()
running_python_version = resolve_python_version()

# The sidebar owns all mission inputs; the main pane is pure output and evidence review.
with st.sidebar:
    st.markdown(
        f"<div class='version-badge'>Running version {html.escape(running_version)} | "
        f"{html.escape(running_python_version)}</div>",
        unsafe_allow_html=True,
    )
    st.header("Mission Setup")
    st.caption("Route, aircraft, and departure timing for the current brief.")
    if st.button("Refresh Weather", help="Clear the 15-minute NOAA cache and fetch fresh weather on this rerun."):
        _cached_successful_noaa_weather.clear()
        st.session_state.pop(_DEGRADED_WEATHER_STASH_KEY, None)
    st.button("Reverse", on_click=_reverse_route, width="stretch")
    if st.session_state.pop("reverse_route_notice", False):
        st.caption(
            "Route reversed. Destination alternate, alternate-route fixes, and per-leg alternates were cleared for safety. "
            "Fuel-stop markers, uplifts, and approach confirmations were retained; review them against the reversed order."
        )
    dep_icao = st.text_input(
        "Departure ICAO",
        value="",
        key="dep_icao",
        on_change=_uppercase_session_text,
        args=("dep_icao", True),
    )
    st.button("Use KSTS for Departure", on_click=_set_departure_ksts, width="stretch")
    route_reference_date_value = st.session_state.get("etd_date", dt.date.today())
    if isinstance(route_reference_date_value, dt.datetime):
        route_reference_date_value = route_reference_date_value.date()
    if not isinstance(route_reference_date_value, dt.date):
        route_reference_date_value = dt.date.today()
    route_reference_date_iso = route_reference_date_value.isoformat()

    preview_dep_icao = normalize_icao(st.session_state.get("dep_icao", dep_icao))
    preview_arr_icao = normalize_icao(st.session_state.get("arr_icao", ""))
    preview_route_text = st.session_state.get("route_waypoints_text", "")
    preview_fuel_stop_text = st.session_state.get("fuel_stop_waypoints_text", "")
    # Resolve a preview route before the textarea renders so ordering warnings can appear above the box.
    preview_departure_validation = _validate_airport_input(preview_dep_icao, route_reference_date_iso)
    preview_destination_validation = _validate_airport_input(preview_arr_icao, route_reference_date_iso)
    preview_route_resolution = _resolve_route_plan_for_ui(
        preview_departure_validation.airport,
        preview_destination_validation.airport,
        preview_route_text,
        preview_fuel_stop_text,
        route_reference_date_iso,
    )
    if preview_route_resolution.progress_warning:
        st.warning(preview_route_resolution.progress_warning)
    elif preview_route_resolution.lookup_error and preview_route_resolution.route_tokens:
        st.warning("FAA live waypoint lookup is unavailable right now. Custom route points are not being applied.")

    route_waypoints_text = st.text_area(
        "Intermediate Waypoints",
        value="",
        key="route_waypoints_text",
        on_change=_uppercase_session_text,
        args=("route_waypoints_text",),
        help=(
            "Enter route points in order, separated by spaces or commas. "
            "Supported values include airports, VORs, and named fixes from the active FAA cycle."
        ),
    )
    fuel_stop_waypoints_text = st.text_input(
        "Fuel Stops",
        value="",
        key="fuel_stop_waypoints_text",
        on_change=_uppercase_session_text,
        args=("fuel_stop_waypoints_text",),
        help="Enter intermediate airport identifiers from the route that should split fuel, reserve, and timing calculations.",
    )
    fuel_stop_ground_minutes = st.number_input(
        "Fuel Stop Ground Time (min)",
        min_value=0.0,
        value=30.0,
        step=5.0,
        help="Added between fuel-stop legs when segment ETDs and ETAs are shown.",
    )
    fuel_stop_uplift_text = st.text_input(
        "Fuel Stop Uplift (gal)",
        value="",
        key="fuel_stop_uplift_text",
        help="Optional per-stop gallons added after landing, for example KBFL=80 KLAS=120. Blank keeps the older full-sidebar-fuel assumption for stops without an entry.",
    )
    fuel_stop_alternate_text = st.text_input(
        "Fuel Stop Alternates",
        value="",
        key="fuel_stop_alternate_text",
        help="Optional per-leg alternates keyed by leg destination, for example KBFL=KSMX KLAS=KVGT.",
    )
    fuel_stop_approach_text = st.text_input(
        "Fuel Stops With Confirmed Instrument Approach",
        value="",
        key="fuel_stop_approach_text",
        help=(
            "Enter fuel-stop destination ICAOs whose instrument-approach availability you confirmed, "
            "for example KBFL KLAS. Unlisted stops conservatively require an alternate."
        ),
    )
    fuel_stop_uplifts = _parse_fuel_stop_uplifts(fuel_stop_uplift_text)
    fuel_stop_alternates = _parse_fuel_stop_alternates(fuel_stop_alternate_text)
    _ignored_alternate_entries = {
        key: value
        for key, value in _parse_stop_value_assignments(fuel_stop_alternate_text).items()
        if key not in fuel_stop_alternates
    }
    if _ignored_alternate_entries:
        st.caption(
            "Ignored alternate entries (not valid ICAO idents): "
            + ", ".join(f"{key}={value}" for key, value in sorted(_ignored_alternate_entries.items()))
        )
    fuel_stop_approach_airports = set(normalize_route_tokens(fuel_stop_approach_text))
    arr_icao = st.text_input(
        "Destination ICAO",
        value="",
        key="arr_icao",
        on_change=_uppercase_session_text,
        args=("arr_icao", True),
    )

    dep_icao = normalize_icao(dep_icao)
    arr_icao = normalize_icao(arr_icao)

    # Departure, destination, and custom route points all share the same FAA live lookup path so
    # the flown geometry comes from one authoritative source while local airport metadata still
    # provides timezone and elevation fields the FAA bundle does not supply here.
    preview_matches_inputs = (
        dep_icao == preview_dep_icao
        and arr_icao == preview_arr_icao
        and route_waypoints_text == preview_route_text
        and fuel_stop_waypoints_text == preview_fuel_stop_text
    )
    departure_validation = (
        preview_departure_validation
        if preview_matches_inputs
        else _validate_airport_input(dep_icao, route_reference_date_iso)
    )
    destination_validation = (
        preview_destination_validation
        if preview_matches_inputs
        else _validate_airport_input(arr_icao, route_reference_date_iso)
    )
    departure_airport = departure_validation.airport
    destination_airport = destination_validation.airport
    route_resolution = (
        preview_route_resolution
        if preview_matches_inputs
        else _resolve_route_plan_for_ui(
            departure_airport,
            destination_airport,
            route_waypoints_text,
            fuel_stop_waypoints_text,
            route_reference_date_iso,
        )
    )
    route_plan = route_resolution.route_plan
    faa_cycle_label = (
        route_resolution.cycle_label
        or departure_validation.cycle_label
        or destination_validation.cycle_label
    )

    if _is_valid_icao(dep_icao):
        if departure_validation.lookup_error:
            st.caption(f"Departure FAA validation unavailable: {departure_validation.lookup_error}")
        if departure_airport is None:
            if departure_validation.faa_waypoint is not None and departure_validation.faa_waypoint.waypoint_type != "Airport":
                st.warning(
                    f"Departure must be an airport ICAO. FAA resolved {dep_icao} as "
                    f"{departure_validation.faa_waypoint.waypoint_type}."
                )
            else:
                st.warning(f"Could not resolve valid airport for departure: {dep_icao}")

    if _is_valid_icao(arr_icao):
        if destination_validation.lookup_error:
            st.caption(f"Destination FAA validation unavailable: {destination_validation.lookup_error}")
        if destination_airport is None:
            if destination_validation.faa_waypoint is not None and destination_validation.faa_waypoint.waypoint_type != "Airport":
                st.warning(
                    f"Destination must be an airport ICAO. FAA resolved {arr_icao} as "
                    f"{destination_validation.faa_waypoint.waypoint_type}."
                )
            else:
                st.warning(f"Could not resolve valid airport for destination: {arr_icao}")

    if route_resolution.lookup_error and route_resolution.route_tokens:
        st.warning("FAA live waypoint lookup is unavailable. Using the direct departure-to-destination route.")
    elif route_resolution.unresolved_tokens:
        st.warning(
            "Custom route is not applied until every waypoint resolves from FAA data: "
            f"{', '.join(route_resolution.unresolved_tokens)}"
        )
    elif route_resolution.custom_route_applied and route_resolution.route_plan is not None:
        st.caption(f"Applied route: {route_resolution.route_plan.route_text}")

    fuel_stop_tokens = tuple(normalize_route_tokens(fuel_stop_waypoints_text))
    if fuel_stop_tokens and route_resolution.route_plan is not None:
        fuel_stop_waypoints = {
            waypoint.identifier: waypoint
            for waypoint in route_resolution.route_plan.waypoints[1:-1]
            if waypoint.is_fuel_stop
        }
        ignored_fuel_stops = [
            token
            for token in fuel_stop_tokens
            if token not in fuel_stop_waypoints
            or fuel_stop_waypoints[token].waypoint_type != "Airport"
        ]
        if ignored_fuel_stops:
            st.warning(
                "Fuel stops must be intermediate airport waypoints already present in the route: "
                f"{', '.join(ignored_fuel_stops)}"
            )
        elif fuel_stop_waypoints:
            st.caption(f"Fuel-stop legs enabled at: {', '.join(fuel_stop_waypoints)}")

    for ambiguity_note in route_resolution.ambiguity_notes:
        st.caption(f"Route note: {ambiguity_note}")
    if faa_cycle_label and route_plan is not None:
        route_sources = [str(waypoint.source) for waypoint in route_plan.waypoints]
        faa_source_label = "offline snapshot" if any("offline snapshot" in source for source in route_sources) else "live"
        st.caption(f"FAA {faa_source_label} waypoint cycle: {faa_cycle_label}")

    available_flight_levels = _sync_cruise_selection(departure_airport, destination_airport)

    st.divider()
    st.header("Aircraft")
    active_performance_profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    mode_ids = [mode.mode_id for mode in active_performance_profile.cruise_modes]
    default_mode_id = st.session_state.get(
        "performance_cruise_mode_id",
        active_performance_profile.default_cruise_mode_id,
    )
    if default_mode_id not in mode_ids:
        default_mode_id = active_performance_profile.default_cruise_mode_id
    selected_cruise_mode_id = st.selectbox(
        "Cruise Mode",
        options=mode_ids,
        index=mode_ids.index(default_mode_id),
        format_func=lambda mode_id: resolve_cruise_mode(active_performance_profile, mode_id).label,
        key="performance_cruise_mode_id",
    )
    active_cruise_mode = resolve_cruise_mode(active_performance_profile, selected_cruise_mode_id)
    climb_schedule_ids = [schedule.schedule_id for schedule in active_performance_profile.climb_schedules]
    default_climb_schedule_id = st.session_state.get(
        "performance_climb_schedule_id",
        active_performance_profile.default_climb_schedule_id,
    )
    if default_climb_schedule_id not in climb_schedule_ids:
        default_climb_schedule_id = active_performance_profile.default_climb_schedule_id
    selected_climb_schedule_id = st.selectbox(
        "Initial Climb Schedule",
        options=climb_schedule_ids,
        index=climb_schedule_ids.index(default_climb_schedule_id),
        format_func=lambda schedule_id: resolve_climb_schedule(active_performance_profile, schedule_id).label,
        key="performance_climb_schedule_id",
    )
    active_climb_schedule = resolve_climb_schedule(active_performance_profile, selected_climb_schedule_id)
    default_upper_climb_schedule_id = st.session_state.get(
        "performance_upper_climb_schedule_id",
        "170_kias_m0_40",
    )
    if default_upper_climb_schedule_id not in climb_schedule_ids:
        default_upper_climb_schedule_id = climb_schedule_ids[-1]
    selected_upper_climb_schedule_id = st.selectbox(
        "Upper Climb Schedule",
        options=climb_schedule_ids,
        index=climb_schedule_ids.index(default_upper_climb_schedule_id),
        format_func=lambda schedule_id: resolve_climb_schedule(active_performance_profile, schedule_id).label,
        key="performance_upper_climb_schedule_id",
        help="The PIM climb schedule used at and above the transition altitude.",
    )
    climb_transition_altitude_ft = float(
        st.number_input(
            "Climb Transition Altitude (MSL)",
            min_value=0,
            max_value=30000,
            value=10000,
            step=1000,
            key="performance_climb_transition_altitude_ft",
            help=(
                "The initial schedule applies below this altitude; the upper schedule "
                "applies at and above it. Treated as pressure altitude against the PIM "
                "climb tables."
            ),
        )
    )

    descent_profile_ids = [profile.profile_id for profile in active_performance_profile.descent_profiles]
    default_descent_profile_id = st.session_state.get(
        "performance_descent_profile_id",
        active_performance_profile.default_descent_profile_id,
    )
    if default_descent_profile_id not in descent_profile_ids:
        default_descent_profile_id = active_performance_profile.default_descent_profile_id
    selected_descent_profile_id = st.selectbox(
        "Descent Profile",
        options=descent_profile_ids,
        index=descent_profile_ids.index(default_descent_profile_id),
        format_func=lambda profile_id: resolve_descent_profile(active_performance_profile, profile_id).label,
        key="performance_descent_profile_id",
    )
    active_descent_profile = resolve_descent_profile(active_performance_profile, selected_descent_profile_id)
    if selected_descent_profile_id == "230_kcas":
        st.caption("220 KIAS planning label; modeled from the Daher Table 5.12.1 230 KCAS source columns.")
    descent_rate_options = list(active_descent_profile.available_vertical_rates_fpm)
    default_descent_rate_fpm = int(
        st.session_state.get(
            "performance_descent_rate_fpm",
            active_performance_profile.default_descent_rate_fpm,
        )
    )
    if default_descent_rate_fpm not in descent_rate_options:
        default_descent_rate_fpm = descent_rate_options[0]
    selected_descent_rate_fpm = int(
        st.selectbox(
            "Descent Rate",
            options=descent_rate_options,
            index=descent_rate_options.index(default_descent_rate_fpm),
            format_func=lambda rate: f"{int(rate):,} fpm",
            key="performance_descent_rate_fpm",
        )
    )
    selected_descent_rows = active_descent_profile.descent_rows_by_rate_fpm[selected_descent_rate_fpm]
    start_fuel = st.number_input(
        "Fuel Load (gal)",
        min_value=0,
        max_value=int(MAX_USABLE_FUEL_GAL),
        value=292,
        key="mission_start_fuel_gal",
        help=f"TBM 960 usable fuel capacity is {int(MAX_USABLE_FUEL_GAL)} gal.",
    )
    startup_taxi_fuel = st.number_input(
        "Startup/Taxi Fuel (gal)",
        min_value=0.0,
        value=float(DEFAULT_STARTUP_TAXI_FUEL_GAL),
        step=0.5,
        key="mission_startup_taxi_fuel_gal",
        help="Default follows the PIM taxi fuel allowance rounded from 50 lb of Jet-A.",
    )
    use_tail_loading = st.checkbox(
        "Use tail-specific loading weights",
        value=False,
        key="use_tail_loading_weights",
        help="Computes climb weight at takeoff and cruise weight at the midpoint of the planned fuel curve.",
    )
    if use_tail_loading:
        uploaded_profile = st.file_uploader(
            "Import Tail Profile JSON",
            type=["json"],
            key="tail_profile_upload",
        )
        if uploaded_profile is not None:
            uploaded_bytes = uploaded_profile.getvalue()
            if st.session_state.get("tail_profile_import_marker") != uploaded_bytes:
                try:
                    imported_profile = deserialize_tail_profile(uploaded_bytes.decode("utf-8"))
                    st.session_state["tail_number"] = imported_profile.tail_number
                    st.session_state["tail_bow_lb"] = imported_profile.basic_operating_weight_lb
                    st.session_state["tail_payload_lb"] = imported_profile.payload_lb
                    st.session_state["tail_time_calibration_pct"] = imported_profile.time_calibration_pct
                    st.session_state["tail_fuel_calibration_pct"] = imported_profile.fuel_calibration_pct
                    st.session_state["tail_profile_import_marker"] = uploaded_bytes
                except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
                    st.error(f"Tail profile could not be imported: {exc}")
        tail_number = st.text_input("Tail Number", value="", key="tail_number").strip().upper()
        basic_operating_weight_lb = st.number_input(
            "Basic Operating Weight (lb)", min_value=0.0, value=4700.0, step=10.0, key="tail_bow_lb"
        )
        payload_lb = st.number_input("Payload (lb)", min_value=0.0, value=500.0, step=10.0, key="tail_payload_lb")
        tail_profile = TailProfile(
            tail_number=tail_number,
            basic_operating_weight_lb=float(basic_operating_weight_lb),
            payload_lb=float(payload_lb),
            time_calibration_pct=float(st.session_state.get("tail_time_calibration_pct", 0.0)),
            fuel_calibration_pct=float(st.session_state.get("tail_fuel_calibration_pct", 0.0)),
        )
        planning_weights = compute_planning_weights(
            tail_profile,
            fuel_load_gal=float(start_fuel),
            startup_taxi_fuel_gal=float(startup_taxi_fuel),
            landing_fuel_gal=float(st.session_state.get("landing_minimum", 60.0)),
        )
        selected_climb_weight_lb = int(round(planning_weights.climb_weight_lb))
        selected_cruise_weight_lb = int(round(planning_weights.cruise_weight_lb))
        st.caption(
            f"Computed takeoff/climb {selected_climb_weight_lb:,} lb; representative mid-cruise "
            f"{selected_cruise_weight_lb:,} lb. Performance tables clamp beyond published limits."
        )
        if tail_profile.time_calibration_pct or tail_profile.fuel_calibration_pct:
            st.caption("Saved calibration deltas are recorded for comparison and are not applied to predictions.")
        weight_limit_notes: list[str] = []
        if not min(CLIMB_WEIGHTS_LB) <= selected_climb_weight_lb <= max(CLIMB_WEIGHTS_LB):
            weight_limit_notes.append(
                f"climb weight clamps to {min(CLIMB_WEIGHTS_LB):,}-{max(CLIMB_WEIGHTS_LB):,} lb table limits"
            )
        if not min(CRUISE_WEIGHTS_LB) <= selected_cruise_weight_lb <= max(CRUISE_WEIGHTS_LB):
            weight_limit_notes.append(
                f"cruise weight clamps to {min(CRUISE_WEIGHTS_LB):,}-{max(CRUISE_WEIGHTS_LB):,} lb table limits"
            )
        if weight_limit_notes:
            st.warning("Performance limit: " + "; ".join(weight_limit_notes) + ".")
        st.download_button(
            "Export Tail Profile JSON",
            data=serialize_tail_profile(tail_profile),
            file_name=f"{tail_number or 'tail'}_profile.json",
            mime="application/json",
        )
    else:
        selected_cruise_weight_lb = int(
            st.selectbox(
                "Cruise Source Weight",
                options=list(CRUISE_WEIGHTS_LB),
                index=list(CRUISE_WEIGHTS_LB).index(active_performance_profile.cruise_weight_lb),
                format_func=lambda weight: f"{int(weight):,} lb",
                key="performance_cruise_weight_lb",
                help=(
                "Published Daher cruise-table weight used for IAS/TAS interpolation. "
                "The PIM shares one fuel-flow column across weights, so weight changes "
                "fuel per mile (via TAS), not gallons per hour."
            ),
            )
        )
        selected_climb_weight_lb = int(
            st.selectbox(
                "Climb Source Weight",
                options=list(CLIMB_WEIGHTS_LB),
                index=list(CLIMB_WEIGHTS_LB).index(active_performance_profile.climb_weight_lb),
                format_func=lambda weight: f"{int(weight):,} lb",
                key="performance_climb_weight_lb",
                help="Published Daher climb-table weight used for climb time, distance, and fuel interpolation.",
            )
        )
    preview_flight_level = preferred_baseline_flight_level(available_flight_levels)
    cruise_preview = sample_cruise_performance(
        active_performance_profile,
        flight_level=preview_flight_level,
        cruise_mode_id=selected_cruise_mode_id,
        weight_lb=selected_cruise_weight_lb,
    )
    climb_rate_values = [row.rate_fpm for row in active_climb_schedule.climb_rows]
    descent_rate_values = [row.rate_fpm for row in selected_descent_rows]
    climb_reference_row = active_climb_schedule.climb_rows[
        min(1, len(active_climb_schedule.climb_rows) - 1)
    ]
    st.caption(active_performance_profile.summary)
    st.caption(f"Aircraft profile: {active_performance_profile.label}")
    st.caption(
        f"{active_cruise_mode.label} ISA preview @ FL{preview_flight_level}: "
        f"{cruise_preview.tas_kts:.0f} KTAS, {cruise_preview.fuel_gph:.0f} GPH, "
        f"fixed trip fuel {active_performance_profile.fixed_fuel_gal:.0f} gal."
    )
    st.caption(
        f"Climb {active_climb_schedule.label} to {climb_transition_altitude_ft:,.0f} ft MSL, then "
        f"{resolve_climb_schedule(active_performance_profile, selected_upper_climb_schedule_id).label}; "
        f"initial bands {min(climb_rate_values)}-{max(climb_rate_values)} fpm. "
        f"Descent {active_descent_profile.label} @ {selected_descent_rate_fpm:,} fpm "
        f"bands {min(descent_rate_values)}-{max(descent_rate_values)} fpm."
    )
    with st.expander("Profile Workspace", expanded=False):
        st.caption(
            "Tail settings persist during this browser session. Export JSON for durable backup or transfer; "
            "Streamlit Community Cloud does not guarantee local-disk persistence while an app is dormant."
        )

    st.divider()
    st.header("Flight Plan")
    flight_level_dropdown_values = sorted(available_flight_levels, reverse=True)
    cruise_options = ["Select cruise altitude"] + [f"FL{fl}" for fl in flight_level_dropdown_values]
    cruise_selection = st.selectbox(
        "Cruise Flight Level",
        options=cruise_options,
        key="cruise_flight_level",
    )
    selected_cruise_fl: int | None = None
    if cruise_selection != "Select cruise altitude":
        selected_cruise_fl = int(cruise_selection.replace("FL", ""))

    cruise_tas_kts = int(round(cruise_preview.tas_kts))
    climb_ias_kts = int(active_climb_schedule.nominal_ias_kts)
    descent_ias_kts = int(active_descent_profile.nominal_ias_kts)
    climb_rate_fpm = int(climb_reference_row.rate_fpm)
    descent_rate_fpm = int(selected_descent_rate_fpm)
    active_performance_profile_for_calc = active_performance_profile
    active_cruise_mode_id_for_calc = selected_cruise_mode_id
    active_climb_schedule_id_for_calc = selected_climb_schedule_id
    active_upper_climb_schedule_id_for_calc = selected_upper_climb_schedule_id
    climb_transition_altitude_ft_for_calc = climb_transition_altitude_ft
    active_descent_profile_id_for_calc = selected_descent_profile_id
    active_descent_rate_fpm_for_calc = selected_descent_rate_fpm
    active_cruise_weight_lb_for_calc = selected_cruise_weight_lb
    active_climb_weight_lb_for_calc = selected_climb_weight_lb

    st.divider()
    st.header("ETD")
    departure_date: dt.date | None = None
    departure_time: dt.time | None = None
    if departure_airport:
        _initialize_etd_defaults_once(departure_airport.timezone)
        st.session_state["etd_timezone_name"] = departure_airport.timezone

        departure_today = dt.datetime.now(pytz.timezone(departure_airport.timezone)).date()
        if st.session_state.get("etd_date", departure_today) < departure_today:
            st.session_state.etd_date = departure_today
            st.caption("The saved ETD date was in the past and was moved to today.")
        st.button("Now +15 min", on_click=_set_etd_now_plus_15)
        departure_date = st.date_input("Date", key="etd_date", min_value=departure_today)
        st.caption(f"Departure Time ({departure_airport.timezone})")
        hour_options = list(range(1, 13))
        minute_options = [f"{m:02d}" for m in range(0, 60, 5)]
        ampm_options = ["AM", "PM"]

        # Session state is seeded before these widgets exist, so the widgets take
        # their values purely from state (no default args, no Streamlit warning).
        hour_col, minute_col, ampm_col = st.columns(3)
        with hour_col:
            etd_hour = st.selectbox(
                "Hour",
                options=hour_options,
                key="etd_hour",
            )
        with minute_col:
            etd_minute = st.selectbox(
                "Minute",
                options=minute_options,
                key="etd_minute",
            )
        with ampm_col:
            etd_ampm = st.selectbox(
                "AM/PM",
                options=ampm_options,
                key="etd_ampm",
            )

        departure_hour_24 = etd_hour % 12
        if etd_ampm == "PM":
            departure_hour_24 += 12
        departure_time = dt.time(departure_hour_24, int(etd_minute))
    else:
        st.caption("Timezone and ETD inputs appear after a valid departure ICAO is entered.")

    st.divider()
    landing_minimum = st.number_input(
        "Final Landing Minimum (gal)", min_value=0, value=60, key="landing_minimum"
    )
    alternate_icao = st.text_input(
        "Alternate ICAO",
        value="",
        key="alternate_icao",
        on_change=_uppercase_session_text,
        args=("alternate_icao", True),
        help="Optional destination alternate. When resolved, the app calculates the destination-to-alternate route distance.",
    )
    alternate_icao = normalize_icao(alternate_icao)
    alternate_validation = _validate_airport_input(alternate_icao, route_reference_date_iso) if alternate_icao else UiAirportValidation(None, None, None, None)
    alternate_airport = alternate_validation.airport
    if alternate_icao:
        if alternate_validation.lookup_error:
            st.caption(f"Alternate FAA validation unavailable: {alternate_validation.lookup_error}")
        if alternate_airport is None:
            st.warning(f"Could not resolve valid airport for alternate: {alternate_icao}")

    destination_has_approach = st.checkbox(
        "Destination has published/special instrument approach",
        value=True,
        help="Used only for Part 91 destination alternate-required logic; the app does not yet ingest Part 97 procedures directly.",
    )
    alternate_route_waypoints_text = st.text_area(
        "Alternate Route Fixes",
        value="",
        key="alternate_route_waypoints_text",
        on_change=_uppercase_session_text,
        args=("alternate_route_waypoints_text",),
        help="Optional fixes between destination and alternate. Leave blank for direct destination-to-alternate planning.",
    )
    manual_alternate_distance_nm = st.number_input(
        "Alternate Distance (NM)",
        min_value=0.0,
        value=0.0,
        step=5.0,
        help="Manual fallback used only when no alternate ICAO is resolved.",
    )
    alternate_route_resolution = (
        _resolve_route_plan_for_ui(
            destination_airport,
            alternate_airport,
            alternate_route_waypoints_text,
            "",
            route_reference_date_iso,
        )
        if destination_airport is not None and alternate_airport is not None
        else UiRouteResolution(None, False, (), (), (), None, None, None)
    )
    if alternate_route_resolution.lookup_error and normalize_route_tokens(alternate_route_waypoints_text):
        st.warning("FAA live waypoint lookup is unavailable. Using direct destination-to-alternate routing.")
    elif alternate_route_resolution.unresolved_tokens:
        st.warning(
            "Alternate route fixes are not applied until every waypoint resolves from FAA data: "
            f"{', '.join(alternate_route_resolution.unresolved_tokens)}"
        )
    elif alternate_route_resolution.custom_route_applied and alternate_route_resolution.route_plan is not None:
        st.caption(f"Applied alternate route: {alternate_route_resolution.route_plan.route_text}")
    alternate_route_plan = alternate_route_resolution.route_plan
    alternate_distance_nm = (
        float(alternate_route_plan.total_distance_nm)
        if alternate_route_plan is not None
        else float(manual_alternate_distance_nm)
    )
    reserve_minutes = st.number_input(
        "Final Reserve (min)",
        min_value=0.0,
        value=45.0,
        step=5.0,
        help="Default follows the fixed-wing IFR final reserve shape in 14 CFR 91.167.",
    )
    missed_approach_fuel = st.number_input(
        "Missed Approach Allowance (gal)",
        min_value=0.0,
        value=5.0,
        step=1.0,
        help="Subtracted from destination-arrival fuel before drawing post-missed alternate range rings.",
    )
    reserve_floor_unit = st.radio(
        "Pilot Reserve Floor Unit",
        options=["gal", "lb"],
        horizontal=True,
        help="Optional pilot preference that can exceed the calculated alternate/final-reserve requirement.",
    )
    reserve_floor_value = st.number_input(
        "Pilot Reserve Floor",
        min_value=0.0,
        value=0.0,
        step=5.0,
        help="Set 0 to use only calculated alternate, final reserve, and landing minimum fuel.",
    )
    reserve_floor_gal = (
        gallons_from_pounds(reserve_floor_value)
        if reserve_floor_unit == "lb"
        else reserve_floor_value
    )
    if reserve_floor_value > 0 and reserve_floor_unit == "lb":
        st.caption(
            f"Pilot reserve floor converts to {reserve_floor_gal:.0f} gal using "
            f"{JET_A_POUNDS_PER_GALLON:g} lb/gal."
        )

    with st.expander("Risk Preferences", expanded=False):
        fuel_high_margin_gal = st.number_input(
            "High fuel-risk margin below (gal)",
            min_value=-100,
            value=0,
            step=5,
            key="risk_fuel_high_margin_gal",
            help="Fuel margin below this value is treated as high known mission risk.",
        )
        fuel_caution_margin_gal = st.number_input(
            "Caution fuel-risk margin below (gal)",
            min_value=-100,
            value=15,
            step=5,
            key="risk_fuel_caution_margin_gal",
            help="Fuel margin below this value is treated as caution known mission risk.",
        )
        route_caution_fraction = st.slider(
            "Caution route exposure",
            min_value=0.05,
            max_value=1.0,
            value=0.25,
            step=0.05,
            key="risk_route_caution_fraction",
            help="Fraction of impacted route bins that can lift known route risk to caution.",
        )
        route_high_fraction = st.slider(
            "High route exposure",
            min_value=0.05,
            max_value=1.0,
            value=0.50,
            step=0.05,
            key="risk_route_high_fraction",
            help="Fraction of impacted route bins that can lift known route risk to high.",
        )
    if fuel_caution_margin_gal < fuel_high_margin_gal:
        st.warning("Caution fuel margin must be at or above the high-risk margin; using the high-risk value.")
        fuel_caution_margin_gal = fuel_high_margin_gal
    if route_high_fraction < route_caution_fraction:
        st.warning("High route exposure must be at or above caution exposure; using the caution value.")
        route_high_fraction = route_caution_fraction
    mission_risk_thresholds = MissionRiskThresholds(
        fuel_high_margin_gal=int(fuel_high_margin_gal),
        fuel_caution_margin_gal=int(fuel_caution_margin_gal),
        route_caution_fraction=float(route_caution_fraction),
        route_high_fraction=float(route_high_fraction),
    )
    if alternate_route_plan is not None:
        st.caption(f"Alternate route: {destination_airport.icao} -> {alternate_airport.icao}, {alternate_distance_nm:.0f} NM.")
    st.caption("Fuel status compares destination fuel against calculated reserves and any higher pilot reserve floor.")

if (
    departure_airport is None
    or destination_airport is None
    or departure_date is None
    or departure_time is None
):
    # Preserve a useful empty state so the app is readable before a route is fully configured.
    st.markdown(
        _build_route_hero(
            title="TBM 960 Mission Brief",
            subtitle=(
                "Build a route-aware weather brief from the sidebar, then review mission, hazard, "
                "and weather evidence in separate workspaces."
            ),
            pills=[
                f"Version {running_version}",
                running_python_version,
                "Live NOAA weather",
                "Segment hazard scoring",
                "TBM 960 performance model",
            ],
            kicker="Ready For Planning",
        ),
        unsafe_allow_html=True,
    )
    intro_cols = st.columns(3)
    with intro_cols[0]:
        _render_insight_card(
            "Route setup",
            "ICAO pair + ETD",
            "Enter valid departure and destination airports to unlock timezone-aware planning.",
        )
    with intro_cols[1]:
        _render_insight_card(
            "Mission matrix",
            "Flight levels",
            "Compare time, fuel, wind, and hazard posture across route-correct cruise altitudes.",
        )
    with intro_cols[2]:
        _render_insight_card(
            "Next profile work",
            "Validated calibration",
            "Profiles and import/export are live; the next step is pilot opt-in application of validated tail deltas.",
            tone_class="tone-low",
        )
    st.stop()

try:
    departure_tz = pytz.timezone(departure_airport.timezone)
    selected_etd = departure_tz.localize(
        dt.datetime.combine(departure_date, departure_time),
        is_dst=None,
    )
except Exception:
    st.error("Selected ETD is invalid for the departure timezone. Please adjust date/time.")
    st.stop()

if is_departure_time_stale(selected_etd, dt.datetime.now(departure_tz)):
    st.warning("Selected ETD is in the past. Use Now +15 min in the sidebar or choose a future time.")

departure_tz_abbrev = _timezone_abbrev(selected_etd, departure_airport.timezone)
destination_tz_abbrev = _timezone_abbrev(selected_etd, destination_airport.timezone)

with st.spinner("Recalculating..."):
    # One recalculation pass hydrates weather, mission math, hazards, and the route profile tabs.
    windtemp_region = infer_windtemp_region(departure_airport, destination_airport, route_plan=route_plan)
    windtemp_level = "low"
    windtemp_fcst = select_windtemp_forecast_cycle(selected_etd.astimezone(dt.timezone.utc))
    active_fuel_stop_airports = {
        waypoint.identifier
        for waypoint in (route_plan.waypoints if route_plan is not None else ())
        if getattr(waypoint, "is_fuel_stop", False)
    }
    mission_weather_airports = set(active_fuel_stop_airports)
    mission_weather_airports.update(
        alternate_code
        for stop_code, alternate_code in fuel_stop_alternates.items()
        if stop_code in active_fuel_stop_airports
    )
    mission_weather_airports.discard(departure_airport.icao)
    mission_weather_airports.discard(destination_airport.icao)
    if alternate_airport is not None:
        mission_weather_airports.discard(alternate_airport.icao)
    def _fetch_mission_weather(fcst_cycle: str):
        return _cached_noaa_weather(
            departure_airport.icao,
            destination_airport.icao,
            alternate_airport.icao if alternate_airport is not None else "",
            ",".join(sorted(mission_weather_airports)),
            windtemp_region,
            windtemp_level,
            fcst_cycle,
            departure_date.isoformat(),
            departure_time.strftime("%H:%M"),
            _APP_RELEASE,
        )

    weather = _fetch_mission_weather(windtemp_fcst)
    corrected_windtemp_fcst = windtemp_cycle_correction(weather, selected_etd.astimezone(dt.timezone.utc))
    if corrected_windtemp_fcst is not None and corrected_windtemp_fcst != windtemp_fcst:
        windtemp_fcst = corrected_windtemp_fcst
        weather = _fetch_mission_weather(windtemp_fcst)
    # The mission is computed as one immutable document; everything below renders
    # its fields and performs no mission arithmetic of its own.
    mission_performance_kwargs = dict(
        fixed_fuel_gal_override=float(startup_taxi_fuel),
        climb_rate_fpm=int(climb_rate_fpm),
        descent_rate_fpm=int(descent_rate_fpm),
        cruise_tas_kts=int(cruise_tas_kts),
        climb_ias_kts=int(climb_ias_kts),
        descent_ias_kts=int(descent_ias_kts),
        performance_profile=active_performance_profile_for_calc,
        cruise_mode_id=active_cruise_mode_id_for_calc,
        climb_schedule_id=active_climb_schedule_id_for_calc,
        upper_climb_schedule_id=active_upper_climb_schedule_id_for_calc,
        climb_transition_altitude_ft=climb_transition_altitude_ft_for_calc,
        descent_profile_id=active_descent_profile_id_for_calc,
        descent_profile_rate_fpm=active_descent_rate_fpm_for_calc,
        cruise_weight_lb=active_cruise_weight_lb_for_calc,
        climb_weight_lb=active_climb_weight_lb_for_calc,
        reserve_minutes=float(reserve_minutes),
        landing_minimum_gal=float(landing_minimum),
        reserve_floor_gal=float(reserve_floor_gal) if reserve_floor_gal > 0 else None,
    )
    try:
        mission_document = build_mission_brief_document(
            departure=departure_airport,
            destination=destination_airport,
            weather=weather,
            route_plan=route_plan,
            departure_dt=selected_etd,
            departure_date=departure_date,
            departure_time_local=departure_time,
            start_fuel_gal=float(start_fuel),
            flight_levels=available_flight_levels,
            selected_flight_level=selected_cruise_fl,
            preview_flight_level=preview_flight_level,
            ground_minutes=float(fuel_stop_ground_minutes),
            uplifts=fuel_stop_uplifts,
            alternates=fuel_stop_alternates,
            mission_alternate_code=alternate_airport.icao if alternate_airport is not None else None,
            mission_alternate_distance_nm=float(alternate_distance_nm),
            mission_alternate_route_label=(
                alternate_route_plan.route_text if alternate_route_plan is not None else ""
            ),
            approach_confirmed_icaos=fuel_stop_approach_airports,
            destination_has_approach=bool(destination_has_approach),
            forecast_phase_airports={
                "Departure": departure_airport.icao,
                "Arrival": destination_airport.icao,
                **({"Alternate": alternate_airport.icao} if alternate_airport is not None else {}),
            },
            usable_fuel_capacity_gal=MAX_USABLE_FUEL_GAL,
            thresholds=mission_risk_thresholds,
            mission_brief_kwargs=mission_performance_kwargs,
            stop_ring_kwargs=dict(
                performance_profile=active_performance_profile_for_calc,
                cruise_mode_id=active_cruise_mode_id_for_calc,
                climb_schedule_id=active_climb_schedule_id_for_calc,
                descent_profile_id=active_descent_profile_id_for_calc,
                descent_profile_rate_fpm=active_descent_rate_fpm_for_calc,
                cruise_weight_lb=active_cruise_weight_lb_for_calc,
                climb_weight_lb=active_climb_weight_lb_for_calc,
                # The builder injects its mission-wide wind model here.
                wind_model=None,
                alt_missed_approach_fuel_gal=float(missed_approach_fuel),
            ),
        )
    except (ValueError, TypeError, KeyError) as exc:
        st.error(f"Mission calculations failed: {exc}")
        st.stop()
    # Testability hook: the AppTest guardrail asserts that rendered text equals
    # these computed fields verbatim. The app never reads this key back.
    st.session_state["_mission_document_for_tests"] = mission_document
    route_wind_model = mission_document.wind_model
    brief = mission_document.brief
    route_hazards_by_fl = mission_document.route_hazards_by_fl

if route_wind_model is None:
    wind_source_status = "Heuristic fallback"
    wind_source_detail = (
        "Mission winds are using the heuristic fallback because NOAA FD windtemp data "
        "did not yield enough usable station coverage for this route."
    )
else:
    wind_source_status = route_wind_model.source
    wind_source_detail = (
        f"Mission winds are using NOAA interpolation from "
        f"{route_wind_model.station_count} stations and "
        f"{route_wind_model.usable_sample_count} usable altitude samples; "
        f"{route_wind_model.coverage_fraction:.0%} of route wind bins are within station coverage."
    )
    if route_wind_model.uncovered_segment_count:
        wind_source_status = "Partial NOAA coverage"

windtemp_feed_status = weather.feed_statuses.get("windtemp")
if windtemp_feed_status is not None:
    provenance_parts = []
    if windtemp_feed_status.issue_time_utc is not None:
        provenance_parts.append(f"data based on {windtemp_feed_status.issue_time_utc.strftime('%d%H%MZ')}")
    if windtemp_feed_status.valid_from_utc is not None and windtemp_feed_status.valid_to_utc is not None:
        provenance_parts.append(
            f"for use {windtemp_feed_status.valid_from_utc.strftime('%d%H%MZ')}–"
            f"{windtemp_feed_status.valid_to_utc.strftime('%d%H%MZ')}"
        )
    if provenance_parts:
        wind_source_detail += " Product " + "; ".join(provenance_parts) + "."
    if windtemp_feed_status.status == "partial":
        wind_source_detail += f" {windtemp_feed_status.error_message}."
    if (
        windtemp_feed_status.valid_from_utc is not None
        and windtemp_feed_status.valid_to_utc is not None
        and not (
            windtemp_feed_status.valid_from_utc
            <= selected_etd.astimezone(dt.timezone.utc)
            <= windtemp_feed_status.valid_to_utc
        )
    ):
        wind_source_status = "Outside FB validity"
        wind_source_detail += " The selected ETD is outside this FB product's FOR-USE window."

weather_fetch_times = [status.fetched_at_utc for status in weather.feed_statuses.values()]
weather_fetched_at_utc = max(weather_fetch_times) if weather_fetch_times else None
weather_age_minutes = (
    max(0, int((dt.datetime.now(dt.timezone.utc) - weather_fetched_at_utc).total_seconds() // 60))
    if weather_fetched_at_utc is not None
    else None
)

route_direction = "Westbound" if "Westbound" in brief.direction_label else "Eastbound"
focus_flight_level = mission_document.focus_flight_level
focus_point = mission_document.focus_point
focus_flight_level_text = f"FL{focus_flight_level}"
focus_segment_hazards = route_hazards_by_fl.get(focus_flight_level, [])
focus_overall_score = _max_hazard_score(focus_segment_hazards, "overall_score")
focus_wind_text = getattr(focus_point, "wind_knots", "Wind unavailable") if focus_point else "Wind unavailable"
focus_ete_text = getattr(focus_point, "ete", "Pending") if focus_point else "Pending"
focus_fuel_at_dest = int(getattr(focus_point, "fuel_at_dest", 0)) if focus_point else 0
focus_fuel_burn = int(getattr(focus_point, "fuel_burn", 0)) if focus_point else 0
focus_calculated_required_fuel = int(getattr(focus_point, "calculated_required_landing_fuel_gal", 0)) if focus_point else 0
focus_reserve_floor = int(getattr(focus_point, "reserve_floor_gal", 0)) if focus_point else 0
focus_required_landing_fuel = int(getattr(focus_point, "required_landing_fuel_gal", 0)) if focus_point else 0
focus_reserve_margin = int(getattr(focus_point, "reserve_margin_gal", 0)) if focus_point else 0
focus_fuel_status = str(getattr(focus_point, "fuel_status", "Unknown")) if focus_point else "Unknown"
focus_impacted_segments = _count_impacted_segments(focus_segment_hazards)
focus_eta_utc = mission_document.nonstop_focus_eta_utc
mission_arrival_eta_utc = mission_document.mission_arrival_eta_utc
legal_alternate_assessment = mission_document.legal_alternate
forecast_quality_checks = list(mission_document.forecast_quality_checks)
fuel_stop_segments = mission_document.fuel_stop_segments
fuel_stop_segment_rows: list[dict[str, object]] = []
segment_arrival_fuels_gal: list[float] = []
leg_reserve_margins_gal: list[tuple[str, int]] = []
range_insets: list[UiRangeInset] = []
if mission_document.multi_leg_plan is not None:
    multi_leg_plan = mission_document.multi_leg_plan
    for leg in multi_leg_plan.legs:
        for leg_warning in leg.warnings:
            st.warning(leg_warning)
        leg_departure_local = leg.departure_utc.astimezone(pytz.timezone(leg.departure_airport.timezone))
        leg_eta_local = leg.arrival_utc.astimezone(pytz.timezone(leg.destination_airport.timezone))
        if not leg.is_final_leg:
            range_insets.append(
                UiRangeInset(
                    role="Fuel stop",
                    label=leg.destination_airport.icao,
                    airport=leg.destination_airport,
                    fuel_on_board_gal=float(leg.point.fuel_at_dest),
                    rings=leg.fuel_stop_rings,
                )
            )
        fuel_stop_segment_rows.append(
            {
                "Leg": leg.leg_number,
                "Cruise FL": focus_flight_level_text,
                "Route": f"{leg.start_identifier} -> {leg.end_identifier}",
                "Start Fuel": int(round(leg.start_fuel_gal)),
                "Distance": leg.brief.distance_nm,
                "ETD": leg_departure_local.strftime("%a %I:%M %p %Z"),
                "ETA": leg_eta_local.strftime("%a %I:%M %p %Z"),
                "Airborne ETE": leg.point.ete,
                "Fuel Burn": leg.point.fuel_burn,
                "Fuel at Landing": leg.point.fuel_at_dest,
                "Uplift": (
                    int(round(leg.uplift_gal))
                    if leg.uplift_gal is not None and not leg.is_final_leg
                    else ("Full reset" if not leg.is_final_leg else "")
                ),
                "Next Start Fuel": int(round(leg.next_start_fuel_gal)) if not leg.is_final_leg else "",
                "Leg Alternate": leg.alternate_route_label,
                "Alt Dist": int(round(leg.alternate_distance_nm)) if leg.alternate_distance_nm else 0,
                "Alt + Reserve": leg.point.calculated_required_landing_fuel_gal,
                "Landing Min": int(math.ceil(float(landing_minimum))),
                "Pilot Floor": leg.point.reserve_floor_gal,
                "Effective Req": leg.point.required_landing_fuel_gal,
                "Reserve Margin": leg.point.reserve_margin_gal,
                "Fuel Status": leg.point.fuel_status,
                "Approach Confirmed": "Yes" if leg.has_approach_confirmed else "No",
                "Legal Alternate": leg.legal_alternate.label,
                "Forecast Quality": (
                    leg.forecast_quality.label if leg.forecast_quality is not None else "No material mismatch"
                ),
            }
        )
    segment_arrival_fuels_gal = list(multi_leg_plan.leg_arrival_fuels_gal)
    leg_reserve_margins_gal = list(multi_leg_plan.leg_reserve_margins_gal)
mission_headline = mission_document.mission_headline
mission_risk_summary = mission_document.risk_summary
if (
    windtemp_feed_status is not None
    and windtemp_feed_status.valid_to_utc is not None
    and mission_arrival_eta_utc is not None
    and mission_arrival_eta_utc > windtemp_feed_status.valid_to_utc
):
    wind_source_status = "Outside FB validity"
    wind_source_detail += " The mission ETA is outside this FB product's FOR-USE window."
if (
    windtemp_feed_status is not None
    and windtemp_feed_status.issue_time_utc is not None
    and mission_arrival_eta_utc is not None
    and mission_arrival_eta_utc - windtemp_feed_status.issue_time_utc > dt.timedelta(hours=30)
):
    wind_source_detail += " Mission ETA is more than 30 hours after the FB issue time; refresh closer to departure."
destination_range_fuel_gal = destination_arrival_fuel_gal(
    float(focus_fuel_at_dest),
    segment_arrival_fuels_gal,
)
with st.spinner("Computing destination range rings..."):
    destination_range_rings = _build_waypoint_range_rings(
        airport=destination_airport,
        fuel_on_board_gal=destination_range_fuel_gal,
        performance_profile=active_performance_profile_for_calc,
        cruise_mode_id=active_cruise_mode_id_for_calc,
        climb_schedule_id=active_climb_schedule_id_for_calc,
        descent_profile_id=active_descent_profile_id_for_calc,
        descent_profile_rate_fpm=active_descent_rate_fpm_for_calc,
        cruise_weight_lb=active_cruise_weight_lb_for_calc,
        climb_weight_lb=active_climb_weight_lb_for_calc,
        wind_model=route_wind_model,
        alt_missed_approach_fuel_gal=float(missed_approach_fuel),
    )
range_insets.append(
    UiRangeInset(
        role="Destination",
        label=destination_airport.icao,
        airport=destination_airport,
        fuel_on_board_gal=destination_range_fuel_gal,
        rings=destination_range_rings,
    )
)
range_calc_rows: list[dict[str, object]] = []
for inset in range_insets:
    for ring in inset.rings:
        range_calc_rows.append(
            {
                "Waypoint": inset.label,
                "Role": inset.role,
                "FOB": round(float(inset.fuel_on_board_gal), 1),
                "Ring": getattr(ring, "label", ""),
                "Altitude AGL": int(getattr(ring, "altitude_agl_ft", 0)),
                "Altitude MSL": int(getattr(ring, "altitude_msl_ft", 0)),
                "Alt Missed": round(float(getattr(ring, "alt_missed_approach_fuel_gal", 0.0)), 1),
                "Alt Climb Fuel": round(float(getattr(ring, "alt_climb_fuel_gal", 0.0)), 1),
                "Alt Descent Fuel": round(float(getattr(ring, "alt_descent_fuel_gal", 0.0)), 1),
                "Alt Cruise Fuel": round(float(getattr(ring, "alt_cruise_fuel_gal", 0.0)), 1),
                "Alt Climb Dist": round(float(getattr(ring, "alt_climb_distance_nm", 0.0)), 1),
                "Alt Cruise Dist": round(float(getattr(ring, "alt_cruise_distance_nm", 0.0)), 1),
                "Alt Descent Dist": round(float(getattr(ring, "alt_descent_distance_nm", 0.0)), 1),
                "Alt Avg Range": round(float(getattr(ring, "alt_average_range_nm", 0.0)), 1),
                "Alt Min Range": round(float(getattr(ring, "alt_min_range_nm", 0.0)), 1),
                "Alt Max Range": round(float(getattr(ring, "alt_max_range_nm", 0.0)), 1),
            }
        )
performance_model_label = (
    active_performance_profile_for_calc.label
    if active_performance_profile_for_calc is not None
    else "Manual override"
)
route_context_svg = build_route_context_svg(
    departure_label=departure_airport.icao,
    departure_latitude=departure_airport.latitude,
    departure_longitude=departure_airport.longitude,
    destination_label=destination_airport.icao,
    destination_latitude=destination_airport.latitude,
    destination_longitude=destination_airport.longitude,
    route_plan=route_plan,
)
# Keep the hero header scannable even when the planned route includes many fixes.
hero_route_title = f"{departure_airport.icao} -> {destination_airport.icao}"
reserve_margin_pill = (
    f"Worst leg reserve margin {mission_headline.reserve_margin_gal:+d} gal ({mission_headline.margin_leg_label})"
    if mission_headline.basis == "multi-leg"
    else f"Reserve margin {mission_headline.reserve_margin_gal:+d} gal"
)
reserve_margin_tone = _tone_class_for_margin(mission_headline.reserve_margin_gal, mission_risk_thresholds)

st.markdown(
    _build_route_hero(
        title=hero_route_title,
        subtitle=(
            f"{brief.direction_label} | ETD {brief.departure_zone_time} {departure_tz_abbrev} on "
            f"{departure_date.strftime('%a %b %d, %Y')} | Destination clock {destination_tz_abbrev}"
        ),
        pills=[
            f"Version {running_version}",
            running_python_version,
            f"Focus {focus_flight_level_text}",
            f"{performance_model_label} / {active_cruise_mode.label}",
            wind_source_status,
            f"Known risk {mission_risk_summary.label}",
            reserve_margin_pill,
        ],
        tone_class=reserve_margin_tone,
        pill_tones={reserve_margin_pill: reserve_margin_tone},
    ),
    unsafe_allow_html=True,
)
if weather_fetched_at_utc is not None:
    st.caption(
        f"Weather fetched {weather_fetched_at_utc.strftime('%H:%MZ')} "
        f"({weather_age_minutes} min ago). Use Refresh Weather in the sidebar to bypass the cache."
    )

if route_wind_model is None:
    st.warning(wind_source_detail)

performance_limit_notes = sorted(
    {note for point in brief.points for note in getattr(point, "performance_limit_notes", ())}
)
if performance_limit_notes:
    st.warning("Performance table limits applied: " + "; ".join(performance_limit_notes))

rows = []
for point in sorted(
    brief.points,
    key=lambda row: _parse_flight_level_number(row.flight_level) or -1,
    reverse=True,
):
    flight_level_number = _parse_flight_level_number(point.flight_level)
    segment_hazards = route_hazards_by_fl.get(flight_level_number or -1, [])
    rows.append(
        {
            "FL": point.flight_level,
            "Wind (kts)": _display_wind_knots(point.wind_knots),
            "Airborne ETE": point.ete,
            f"ETA Arrival ({destination_tz_abbrev})": point.eta_arrival_zone,
            f"ETA Departure ({departure_tz_abbrev})": point.eta_departure_zone,
            "Fuel Burn": point.fuel_burn,
            "FOB at Landing": point.fuel_at_dest,
            "Alt + Reserve": point.calculated_required_landing_fuel_gal,
            "Landing Min": int(math.ceil(float(landing_minimum))),
            "Pilot Floor": point.reserve_floor_gal,
            "Effective Req": point.required_landing_fuel_gal,
            "Reserve Margin": point.reserve_margin_gal,
            "Fuel Status": point.fuel_status,
            "Icing": summarize_segment_hazard(segment_hazards, score_field="icing_score"),
            "Turbulence": summarize_segment_hazard(segment_hazards, score_field="turbulence_score"),
            "Convective": summarize_segment_hazard(segment_hazards, score_field="convective_score"),
            "IFR": summarize_segment_hazard(segment_hazards, score_field="ifr_score"),
            "Mtn Obsc": summarize_segment_hazard(segment_hazards, score_field="mountain_obscuration_score"),
            "Sfc Wind": summarize_segment_hazard(segment_hazards, score_field="surface_wind_score"),
            "LLWS": summarize_segment_hazard(segment_hazards, score_field="llws_score"),
            "Hazard": summarize_segment_hazard(segment_hazards, score_field="overall_score"),
        }
    )
df = pd.DataFrame(rows)

hazard_fl_values = sorted(available_flight_levels, reverse=True)
hazard_fl_options = [f"FL{fl}" for fl in hazard_fl_values]
if selected_cruise_fl in available_flight_levels:
    hazard_default_idx = hazard_fl_values.index(selected_cruise_fl)
else:
    default_hazard_fl = preferred_baseline_flight_level(available_flight_levels)
    hazard_default_idx = hazard_fl_values.index(default_hazard_fl)

hazard_follow_value = hazard_fl_options[hazard_default_idx]
if st.session_state.get("hazard_detail_last_cruise") != focus_flight_level_text:
    st.session_state["hazard_detail_flight_level"] = hazard_follow_value
st.session_state["hazard_detail_last_cruise"] = focus_flight_level_text

selected_hazard_fl_text = st.selectbox(
    "Hazard detail flight level",
    options=hazard_fl_options,
    key="hazard_detail_flight_level",
)
selected_hazard_fl = _parse_flight_level_number(selected_hazard_fl_text) or available_flight_levels[0]
selected_segment_hazards = route_hazards_by_fl.get(selected_hazard_fl, [])

# Split the outputs by workflow so the mission matrix, hazards, source tables, and raw feeds
# can each stay dense without competing for one long scrolling page.
mission_tab, hazard_tab, performance_tab, weather_tab = st.tabs(
    ["Mission Plan", "Hazards", "Performance Tables", "Weather Inputs"]
)

with mission_tab:
    st.markdown(
        (
            "<p class='section-copy'>Compare the route by flight level. The highlighted row tracks "
            "the current focus altitude while the surrounding rows preserve the full comparison.</p>"
        ),
        unsafe_allow_html=True,
    )
    if fuel_stop_segments:
        st.caption(
            "Matrix rows are nonstop what-if comparisons across altitudes; "
            "the fuel-stop table below is the planned mission."
        )
    st.caption("Wind (kts) is the signed time-weighted along-track average; positive = tailwind.")
    mission_cards = st.columns(6)
    focus_fuel_ledger = getattr(focus_point, "fuel_ledger", None) if focus_point else None
    landing_fuel_summary = landing_fuel_presentation(
        fuel_on_board_gal=focus_fuel_at_dest,
        fuel_status=focus_fuel_status,
        effective_requirement_gal=focus_required_landing_fuel,
        alternate_and_reserve_gal=focus_calculated_required_fuel,
        landing_minimum_gal=int(math.ceil(float(landing_minimum))),
        pilot_floor_gal=focus_reserve_floor,
        reserve_margin_gal=focus_reserve_margin,
        taxi_fuel_gal=focus_fuel_ledger.taxi_fuel_gal if focus_fuel_ledger else None,
        climb_fuel_gal=focus_fuel_ledger.climb_fuel_gal if focus_fuel_ledger else None,
        cruise_fuel_gal=focus_fuel_ledger.cruise_fuel_gal if focus_fuel_ledger else None,
        descent_fuel_gal=focus_fuel_ledger.descent_fuel_gal if focus_fuel_ledger else None,
    )
    with mission_cards[0]:
        _render_insight_card(
            "Departure",
            brief.departure_zone_time,
            f"{departure_date.strftime('%a %b %d, %Y')} {departure_tz_abbrev}",
        )
    with mission_cards[1]:
        _render_insight_card(
            "Distance",
            f"{brief.distance_nm} NM",
            f"{route_direction} route | wind region {weather.windtemp_region}",
        )
    with mission_cards[2]:
        _render_insight_card(
            "Focus altitude",
            focus_flight_level_text,
            (
                f"{'Selected cruise' if selected_cruise_fl in available_flight_levels else 'Preview focus'} | "
                f"Wind {focus_wind_text} | Airborne ETE {focus_ete_text}"
            ),
        )
    with mission_cards[3]:
        _render_insight_card(
            "FOB at landing",
            f"{mission_headline.fob_at_landing_gal} gal",
            (
                "Gross touchdown FOB after planned fuel stops | worst leg margin "
                f"{mission_headline.reserve_margin_gal:+d} gal ({mission_headline.margin_leg_label})"
                if mission_headline.basis == "multi-leg"
                else landing_fuel_summary.card_detail
            ),
            tone_class=_tone_class_for_margin(mission_headline.reserve_margin_gal, mission_risk_thresholds),
        )
    with mission_cards[4]:
        _render_insight_card(
            "Hazard posture",
            hazard_label(focus_overall_score),
            f"{focus_impacted_segments}/{len(focus_segment_hazards)} route bins impacted at {focus_flight_level_text}",
            tone_class=_tone_class_for_score(focus_overall_score),
        )
    with mission_cards[5]:
        _render_insight_card(
            "Known mission risk",
            mission_risk_summary.label,
            "Observed weather, route hazards, and fuel margin only.",
            tone_class=_tone_class_for_score(mission_risk_summary.score),
        )
    # Keep the six-card band aligned while retaining the full fuel audit trail.
    if mission_headline.basis == "multi-leg":
        st.caption(
            "Nonstop what-if basis for the matrix rows: "
            + landing_fuel_summary.breakdown
            + " Headline FOB and margin track the planned legs; see the fuel-stop table for per-leg requirements."
        )
    else:
        st.caption(landing_fuel_summary.breakdown)
    if use_tail_loading and focus_point is not None:
        with st.expander("Actual vs. Modeled Calibration", expanded=False):
            # Same ceil convention the core uses for the displayed ETE.
            modeled_minutes = int(math.ceil((float(focus_point.airborne_hours) * 60.0) - 1e-9))
            calibration_cols = st.columns(2)
            with calibration_cols[0]:
                actual_minutes = st.number_input(
                    "Actual airborne time (min)",
                    min_value=1,
                    value=max(modeled_minutes, 1),
                    key="tail_actual_airborne_minutes",
                )
            with calibration_cols[1]:
                actual_fuel_burn = st.number_input(
                    "Actual fuel burn (gal)",
                    min_value=1,
                    value=max(focus_fuel_burn, 1),
                    key="tail_actual_fuel_burn_gal",
                )
            time_delta_pct, fuel_delta_pct = calibration_deltas_pct(
                actual_airborne_minutes=float(actual_minutes),
                modeled_airborne_minutes=float(modeled_minutes),
                actual_fuel_burn_gal=float(actual_fuel_burn),
                modeled_fuel_burn_gal=float(focus_fuel_burn),
            )
            st.caption(
                f"Observed delta vs book model: time {time_delta_pct:+.1f}%; fuel {fuel_delta_pct:+.1f}%. "
                "Save these to the tail profile after confirming the actual flight values."
            )
            if st.button("Save Calibration Deltas", key="save_tail_calibration"):
                st.session_state["tail_time_calibration_pct"] = time_delta_pct
                st.session_state["tail_fuel_calibration_pct"] = fuel_delta_pct
                st.success("Calibration deltas saved in this browser session; export the tail JSON for durable storage.")

    mission_diag_cols = st.columns(2)
    with mission_diag_cols[0]:
        _render_insight_card(
            "Performance model",
            performance_model_label,
            (
                f"Cruise {active_cruise_mode.label} | "
                f"Climb {active_climb_schedule.label} | "
                f"Weights {active_cruise_weight_lb_for_calc:,}/{active_climb_weight_lb_for_calc:,} lb"
            ),
        )
    with mission_diag_cols[1]:
        _render_insight_card(
            "Weather/data confidence",
            mission_risk_summary.confidence,
            "NOAA feed health; separate from known mission risk.",
            tone_class=_confidence_tone_class(mission_risk_summary.confidence),
        )
    legal_cols = st.columns(3)
    with legal_cols[0]:
        _render_insight_card(
            "Legal alternate",
            legal_alternate_assessment.label,
            "; ".join(legal_alternate_assessment.reasons[:2]),
            tone_class="tone-moderate" if legal_alternate_assessment.is_required else "tone-clear",
        )
    with legal_cols[1]:
        window_text = "ETA window unavailable"
        if legal_alternate_assessment.window_start_utc and legal_alternate_assessment.window_end_utc:
            window_text = (
                f"{legal_alternate_assessment.window_start_utc.strftime('%H%MZ')} - "
                f"{legal_alternate_assessment.window_end_utc.strftime('%H%MZ')}"
            )
        _render_insight_card(
            "1-2-3 window",
            window_text,
            (
                f"Worst ceiling {legal_alternate_assessment.worst_ceiling_ft or 'unknown'} ft | "
                f"visibility {legal_alternate_assessment.worst_visibility_sm or 'unknown'} SM"
            ),
            tone_class="tone-low" if legal_alternate_assessment.is_required else "tone-clear",
        )
    with legal_cols[2]:
        if forecast_quality_checks:
            strongest_quality = max(forecast_quality_checks, key=lambda check: check.score)
            quality_detail = "; ".join(strongest_quality.reasons[:2])
            _render_insight_card(
                "Forecast quality",
                f"{strongest_quality.phase}: {strongest_quality.label}",
                quality_detail,
                tone_class=_tone_class_for_score(strongest_quality.score),
            )
        else:
            _render_insight_card(
                "Forecast quality",
                "No material mismatch",
                "Recent METARs do not show a material downside from their applicable TAF periods.",
            )
    with st.expander("Known mission risk reasons", expanded=mission_risk_summary.score >= 2):
        for reason in mission_risk_summary.reasons:
            st.caption(reason)
    with st.expander("Legal alternate assessment", expanded=legal_alternate_assessment.is_required):
        st.caption("Fixed-wing Part 91 destination alternate logic: approach availability plus ETA +/- 1 hour TAF ceiling >= 2,000 ft and visibility >= 3 SM.")
        for reason in legal_alternate_assessment.reasons:
            st.caption(reason)
    with st.expander("Forecast-vs-actual quality checks", expanded=bool(forecast_quality_checks)):
        if forecast_quality_checks:
            for check in forecast_quality_checks:
                st.caption(f"{check.phase} {check.icao}: {check.label} - {'; '.join(check.reasons)}")
        else:
            st.caption("Missing PIREPs/AIREPs or missing observations are not treated as quality failures by themselves.")
    with st.expander(
        "Weather/data confidence reasons",
        expanded=mission_risk_summary.confidence in {"Low", "Unknown"},
    ):
        if mission_risk_summary.confidence_reasons:
            for reason in mission_risk_summary.confidence_reasons:
                st.caption(reason)
        else:
            st.caption("No failed or empty NOAA feeds are reducing confidence for this calculation.")

    st.markdown(
        (
            "<p class='section-copy'>Route context map cropped to the states crossed or nearby, "
            "with the planned course and any intermediate FAA waypoints overlaid.</p>"
        ),
        unsafe_allow_html=True,
    )
    st.markdown(f"<div class='route-map-shell'>{route_context_svg}</div>", unsafe_allow_html=True)

    range_map_tab, range_calc_tab = st.tabs(["Range Insets", "Range Ring Calcs"])
    with range_map_tab:
        if range_insets:
            st.markdown(
                (
                    "<p class='section-copy'><strong>Advisory reach estimate — not reserve or legal protection.</strong> "
                    "Fuel range insets show contextual post-missed reach from each "
                    "fuel stop and the destination. Rings subtract the missed-approach allowance plus "
                    "modeled climb and descent fuel; they do not protect legal reserve.</p>"
                ),
                unsafe_allow_html=True,
            )
            inset_cols = st.columns(min(len(range_insets), 3))
            for inset_index, inset in enumerate(range_insets):
                try:
                    inset_svg = build_range_inset_svg(
                        anchor_label=inset.label,
                        anchor_latitude=inset.airport.latitude,
                        anchor_longitude=inset.airport.longitude,
                        range_rings=inset.rings,
                        title=f"{inset.role} {inset.label} | FOB {inset.fuel_on_board_gal:.0f} gal",
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    st.error(f"Range inset {inset.label} could not be rendered: {exc}")
                    continue
                with inset_cols[inset_index % len(inset_cols)]:
                    st.markdown(f"<div class='range-inset-shell'>{inset_svg}</div>", unsafe_allow_html=True)
        else:
            st.caption("No range insets are available for the current FOB and performance assumptions.")
        st.caption(
            "Inset shapes use forecast winds by bearing and altitude during the post-missed climb, cruise, and descent."
        )
    with range_calc_tab:
        if range_calc_rows:
            st.dataframe(
                pd.DataFrame(range_calc_rows),
                hide_index=True,
                width="stretch",
                column_config={
                    "FOB": st.column_config.NumberColumn("FOB", format="%.1f gal"),
                    "Altitude AGL": st.column_config.NumberColumn("Altitude AGL", format="%d ft"),
                    "Altitude MSL": st.column_config.NumberColumn("Altitude MSL", format="%d ft"),
                    "Alt Missed": st.column_config.NumberColumn("Alt Missed", format="%.1f gal"),
                    "Alt Climb Fuel": st.column_config.NumberColumn("Alt Climb Fuel", format="%.1f gal"),
                    "Alt Descent Fuel": st.column_config.NumberColumn("Alt Descent Fuel", format="%.1f gal"),
                    "Alt Cruise Fuel": st.column_config.NumberColumn("Alt Cruise Fuel", format="%.1f gal"),
                    "Alt Climb Dist": st.column_config.NumberColumn("Alt Climb Dist", format="%.1f NM"),
                    "Alt Cruise Dist": st.column_config.NumberColumn("Alt Cruise Dist", format="%.1f NM"),
                    "Alt Descent Dist": st.column_config.NumberColumn("Alt Descent Dist", format="%.1f NM"),
                    "Alt Avg Range": st.column_config.NumberColumn("Alt Avg Range", format="%.1f NM"),
                    "Alt Min Range": st.column_config.NumberColumn("Alt Min Range", format="%.1f NM"),
                    "Alt Max Range": st.column_config.NumberColumn("Alt Max Range", format="%.1f NM"),
                },
            )
        else:
            st.caption("No range-ring calculation rows are available for the current FOB and performance assumptions.")

    if alternate_route_plan is not None:
        _render_insight_card(
            "Alternate route",
            alternate_route_plan.route_text,
            f"{alternate_distance_nm:.0f} NM destination-to-alternate route.",
        )

    if fuel_stop_segment_rows:
        st.markdown(
            (
                "<p class='section-copy'>Fuel-stop segmentation treats each fuel-stop leg as a fresh dispatch "
                "with the sidebar fuel load, reserve settings, and entered ground time between legs. "
                "Airborne ETE runs from takeoff to landing and excludes taxi, vectors, holds, and other operational delay.</p>"
            ),
            unsafe_allow_html=True,
        )
        st.dataframe(
            pd.DataFrame(fuel_stop_segment_rows),
            hide_index=True,
            width="stretch",
            column_config={
                "Start Fuel": st.column_config.NumberColumn("Start Fuel", format="%d gal"),
                "Distance": st.column_config.NumberColumn("Distance", format="%d NM"),
                "Fuel Burn": st.column_config.NumberColumn(
                    "Fuel Burn", format="%d gal", help="Block fuel including startup/taxi."
                ),
                "Fuel at Landing": st.column_config.NumberColumn("Fuel at Landing", format="%d gal"),
                "Next Start Fuel": st.column_config.NumberColumn("Next Start Fuel", format="%d gal"),
                "Alt Dist": st.column_config.NumberColumn("Alt Dist", format="%d NM"),
                "Alt + Reserve": st.column_config.NumberColumn("Alt + Reserve", format="%d gal"),
                "Landing Min": st.column_config.NumberColumn("Landing Min", format="%d gal"),
                "Pilot Floor": st.column_config.NumberColumn("Pilot Floor", format="%d gal"),
                "Effective Req": st.column_config.NumberColumn("Effective Req", format="%d gal"),
                "Reserve Margin": st.column_config.NumberColumn("Reserve Margin", format="%+d gal"),
            },
        )

    st.markdown(
        f"<p class='section-copy'>Mission matrix by flight level. The highlighted row follows {focus_flight_level_text}.</p>",
        unsafe_allow_html=True,
    )
    show_hazard_columns = st.toggle(
        "Show detailed hazard columns",
        value=False,
        help="The compact matrix retains the overall Hazard column; enable this to audit each hazard family.",
    )
    compact_columns = [
        "FL", "Wind (kts)", "Airborne ETE", f"ETA Arrival ({destination_tz_abbrev})",
        f"ETA Departure ({departure_tz_abbrev})", "Fuel Burn", "FOB at Landing", "Alt + Reserve",
        "Landing Min", "Pilot Floor", "Effective Req", "Reserve Margin", "Fuel Status", "Hazard",
    ]
    mission_df = df if show_hazard_columns else df[compact_columns]
    hazard_columns = [
        column
        for column in ("Icing", "Turbulence", "Convective", "IFR", "Mtn Obsc", "Sfc Wind", "LLWS", "Hazard")
        if column in mission_df.columns
    ]
    styled = mission_df.style.apply(
        lambda row: _highlight_focus_row(row, focus_flight_level_text),
        axis=1,
    ).map(
        lambda x: _highlight_low_fuel(x, int(landing_minimum)),
        subset=["FOB at Landing"],
    ).map(
        _highlight_negative_margin,
        subset=["Reserve Margin"],
    ).map(
        _highlight_hazard_label,
        subset=hazard_columns,
    )
    st.dataframe(
        styled,
        hide_index=True,
        width="stretch",
        column_config={
            "Fuel Burn": st.column_config.NumberColumn(
                "Fuel Burn", format="%d gal", help="Block fuel including startup/taxi."
            ),
            "FOB at Landing": st.column_config.NumberColumn("FOB at Landing", format="%d gal"),
            "Alt + Reserve": st.column_config.NumberColumn("Alt + Reserve", format="%d gal"),
            "Landing Min": st.column_config.NumberColumn("Landing Min", format="%d gal"),
            "Pilot Floor": st.column_config.NumberColumn("Pilot Floor", format="%d gal"),
            "Effective Req": st.column_config.NumberColumn("Effective Req", format="%d gal"),
            "Reserve Margin": st.column_config.NumberColumn("Reserve Margin", format="%+d gal"),
        },
    )

with hazard_tab:
    st.markdown(
        (
            "<p class='section-copy'>Hazard scoring uses route segments, interval geometry sampling, "
            "midpoint timing, altitude overlap, and live NOAA polygons.</p>"
        ),
        unsafe_allow_html=True,
    )

    selected_hazard_icing = _max_hazard_score(selected_segment_hazards, "icing_score")
    selected_hazard_turb = _max_hazard_score(selected_segment_hazards, "turbulence_score")
    selected_hazard_conv = _max_hazard_score(selected_segment_hazards, "convective_score")
    selected_hazard_ifr = _max_hazard_score(selected_segment_hazards, "ifr_score")
    selected_hazard_mountain = _max_hazard_score(selected_segment_hazards, "mountain_obscuration_score")
    selected_hazard_surface_wind = _max_hazard_score(selected_segment_hazards, "surface_wind_score")
    selected_hazard_llws = _max_hazard_score(selected_segment_hazards, "llws_score")
    selected_hazard_overall = _max_hazard_score(selected_segment_hazards, "overall_score")
    selected_hazard_impacted = _count_impacted_segments(selected_segment_hazards)

    hazard_top_cols = st.columns([1.05, 3.0])
    with hazard_top_cols[0]:
        _render_insight_card(
            "Impacted segments",
            f"{selected_hazard_impacted}/{len(selected_segment_hazards)}",
            f"Current detail view: {selected_hazard_fl_text}",
            tone_class=_tone_class_for_score(selected_hazard_overall),
        )
    with hazard_top_cols[1]:
        hazard_cards = st.columns(4)
        with hazard_cards[0]:
            _render_insight_card(
                "Icing",
                hazard_label(selected_hazard_icing),
                f"{selected_hazard_fl_text} segment exposure",
                tone_class=_tone_class_for_score(selected_hazard_icing),
            )
        with hazard_cards[1]:
            _render_insight_card(
                "Turbulence",
                hazard_label(selected_hazard_turb),
                f"{selected_hazard_fl_text} segment exposure",
                tone_class=_tone_class_for_score(selected_hazard_turb),
            )
        with hazard_cards[2]:
            _render_insight_card(
                "Convective",
                hazard_label(selected_hazard_conv),
                f"{selected_hazard_fl_text} segment exposure",
                tone_class=_tone_class_for_score(selected_hazard_conv),
            )
    with hazard_cards[3]:
        _render_insight_card(
            "Overall",
            hazard_label(selected_hazard_overall),
            f"{selected_hazard_fl_text} summary",
            tone_class=_tone_class_for_score(selected_hazard_overall),
        )
    surface_cards = st.columns(4)
    for card, title, score in (
        (surface_cards[0], "IFR", selected_hazard_ifr),
        (surface_cards[1], "Mtn Obsc", selected_hazard_mountain),
        (surface_cards[2], "Sfc Wind", selected_hazard_surface_wind),
        (surface_cards[3], "LLWS", selected_hazard_llws),
    ):
        with card:
            _render_insight_card(
                title,
                hazard_label(score),
                f"{selected_hazard_fl_text} route exposure",
                tone_class=_tone_class_for_score(score),
            )

    try:
        with st.spinner("Rendering vertical profile..."):
            vertical_profile = build_route_vertical_profile(
                departure_airport,
                destination_airport,
                hazard_areas=weather.hazard_areas,
                reference_time_utc=selected_etd.astimezone(dt.timezone.utc),
                flight_level=selected_hazard_fl,
                climb_rate_fpm=int(climb_rate_fpm),
                descent_rate_fpm=int(descent_rate_fpm),
                cruise_tas_kts=int(cruise_tas_kts),
                climb_ias_kts=int(climb_ias_kts),
                descent_ias_kts=int(descent_ias_kts),
                performance_profile=active_performance_profile_for_calc,
                cruise_mode_id=active_cruise_mode_id_for_calc,
                climb_schedule_id=active_climb_schedule_id_for_calc,
                upper_climb_schedule_id=active_upper_climb_schedule_id_for_calc,
                climb_transition_altitude_ft=climb_transition_altitude_ft_for_calc,
                descent_profile_id=active_descent_profile_id_for_calc,
                descent_profile_rate_fpm=active_descent_rate_fpm_for_calc,
                cruise_weight_lb=active_cruise_weight_lb_for_calc,
                climb_weight_lb=active_climb_weight_lb_for_calc,
                wind_model=route_wind_model,
                route_plan=route_plan,
            )
        vertical_profile_svg = build_route_vertical_profile_svg(
            vertical_profile,
            departure_label=departure_airport.icao,
            destination_label=destination_airport.icao,
            selected_flight_level_label=selected_hazard_fl_text,
        )
    except (ValueError, TypeError, KeyError) as exc:
        st.error(f"Vertical profile could not be calculated: {exc}")
        vertical_profile_svg = ""

    st.markdown(
        (
            "<p class='section-copy'>Side-profile view for the selected flight level. The magenta line shows "
            "the planned climb, cruise, and descent path; colored bands show route-relevant hazard layers with "
            "their forecast base/top altitudes so climb-above or descend-below options are easier to judge.</p>"
        ),
        unsafe_allow_html=True,
    )
    if vertical_profile_svg:
        st.caption("Click a legend tile to show or hide that hazard layer without rerunning the mission brief.")
        components.html(
            build_interactive_route_vertical_profile_html(vertical_profile_svg),
            height=560,
            scrolling=False,
        )

    if selected_segment_hazards:
        segment_rows = [
            {
                "Seg": row.segment_index,
                "Leg NM": row.segment_distance_nm,
                "Icing": hazard_label(row.icing_score),
                "Turbulence": hazard_label(row.turbulence_score),
                "Convective": hazard_label(row.convective_score),
                "IFR": hazard_label(row.ifr_score),
                "Mtn Obsc": hazard_label(row.mountain_obscuration_score),
                "Sfc Wind": hazard_label(row.surface_wind_score),
                "LLWS": hazard_label(row.llws_score),
                "Overall": hazard_label(row.overall_score),
                "Sources": row.sources,
            }
            for row in selected_segment_hazards
        ]
        st.dataframe(pd.DataFrame(segment_rows), hide_index=True, width="stretch")
    else:
        _render_insight_card(
            "Hazard detail",
            "Clear",
            f"No active polygon intersections at {selected_hazard_fl_text} for the selected ETD.",
        )

with performance_tab:
    official_pim_profile = OFFICIAL_PIM_PROFILE
    st.markdown(
        (
            "<p class='section-copy'>Browse the extracted official Daher PIM source tables that now drive "
            "the TBM 960 baseline. These are the published table families, not just the older ISA-only subset.</p>"
        ),
        unsafe_allow_html=True,
    )

    performance_cards = st.columns(4)
    with performance_cards[0]:
        _render_insight_card(
            "Profile",
            official_pim_profile.label,
            official_pim_profile.aircraft,
        )
    with performance_cards[1]:
        _render_insight_card(
            "Source",
            "Official PIM",
            official_pim_profile.source,
        )
    with performance_cards[2]:
        _render_insight_card(
            "Source weights",
            f"{official_pim_profile.cruise_weight_lb:,}/{official_pim_profile.climb_weight_lb:,} lb",
            "Cruise calculations use the official 7,100 lb rows and climb uses the official 7,394 lb rows.",
            tone_class="tone-clear" if official_pim_profile.verified else "tone-low",
        )
    with performance_cards[3]:
        _render_insight_card(
            "PDF asset",
            "Vendored",
            "The Daher PIM source PDF is stored in the repo and parsed locally.",
        )

    cruise_table_tab, climb_table_tab, descent_table_tab, notes_table_tab = st.tabs(
        ["Cruise", "Climb", "Descent", "Notes"]
    )

    with cruise_table_tab:
        cruise_mode_id = st.selectbox(
            "Cruise table family",
            options=list(CRUISE_MODE_METADATA.keys()),
            index=list(CRUISE_MODE_METADATA.keys()).index(selected_cruise_mode_id),
            format_func=lambda mode_id: CRUISE_MODE_METADATA[mode_id]["label"],
            key="perf_tables_cruise_mode_id",
        )
        cruise_temp_offsets = list_available_cruise_temperature_offsets(cruise_mode_id)
        cruise_temp_offset_c = st.selectbox(
            "ISA deviation table",
            options=list(cruise_temp_offsets),
            index=list(cruise_temp_offsets).index(0),
            format_func=lambda value: "ISA" if value == 0 else f"ISA {value:+d} C",
            key="perf_tables_cruise_temp_offset_c",
        )
        cruise_weight_lb = st.selectbox(
            "Cruise weight",
            options=list(CRUISE_WEIGHTS_LB),
            index=list(CRUISE_WEIGHTS_LB).index(official_pim_profile.cruise_weight_lb),
            format_func=lambda weight: f"{weight:,} lb",
            key="perf_tables_cruise_weight_lb",
        )
        cruise_rows = cruise_rows_for_weight(cruise_mode_id, int(cruise_temp_offset_c), int(cruise_weight_lb))
        cruise_reference = CRUISE_MODE_METADATA[cruise_mode_id]["table_references_by_temp_delta_c"][int(cruise_temp_offset_c)]
        st.caption(
            f"{cruise_reference} | {CRUISE_MODE_METADATA[cruise_mode_id]['label']} | "
            f"{CRUISE_MODE_METADATA[cruise_mode_id]['condition_summary']}"
        )
        cruise_df = pd.DataFrame(
            [
                {
                    "Altitude": f"{int(row['pressure_altitude_ft']):,} ft",
                    "FL": (f"FL{int(row['pressure_altitude_ft']) // 100}" if int(row["pressure_altitude_ft"]) >= 18000 else ""),
                    "OAT": int(row["oat_c"]),
                    "Torque": int(row["torque_pct"]),
                    "Fuel Flow": float(row["fuel_flow_gph"]),
                    "IAS": int(row["ias_kts"]),
                    "TAS": int(row["tas_kts"]),
                }
                for row in reversed(cruise_rows)
            ]
        )
        st.dataframe(
            cruise_df,
            hide_index=True,
            width="stretch",
            column_config={
                "OAT": st.column_config.NumberColumn("OAT", format="%d C"),
                "Torque": st.column_config.NumberColumn("Torque", format="%d %%"),
                "Fuel Flow": st.column_config.NumberColumn("Fuel Flow", format="%.1f GPH"),
                "IAS": st.column_config.NumberColumn("IAS", format="%d kts"),
                "TAS": st.column_config.NumberColumn("TAS", format="%d KTAS"),
            },
        )

    with climb_table_tab:
        climb_schedule_id = st.selectbox(
            "Climb schedule family",
            options=list(CLIMB_SCHEDULE_METADATA.keys()),
            index=list(CLIMB_SCHEDULE_METADATA.keys()).index(selected_climb_schedule_id),
            format_func=lambda schedule_id: CLIMB_SCHEDULE_METADATA[schedule_id]["label"],
            key="perf_tables_climb_schedule_id",
        )
        climb_temp_offsets = list_available_climb_temperature_offsets(climb_schedule_id)
        climb_temp_offset_c = st.selectbox(
            "ISA deviation table",
            options=list(climb_temp_offsets),
            index=list(climb_temp_offsets).index(0),
            format_func=lambda value: "ISA" if value == 0 else f"ISA {value:+d} C",
            key="perf_tables_climb_temp_offset_c",
        )
        climb_weight_lb = st.selectbox(
            "Climb weight",
            options=list(CLIMB_WEIGHTS_LB),
            index=list(CLIMB_WEIGHTS_LB).index(official_pim_profile.climb_weight_lb),
            format_func=lambda weight: f"{weight:,} lb",
            key="perf_tables_climb_weight_lb",
        )
        climb_rows = climb_rows_for_weight(climb_schedule_id, int(climb_temp_offset_c), int(climb_weight_lb))
        climb_reference = CLIMB_SCHEDULE_METADATA[climb_schedule_id]["table_references_by_temp_delta_c"][int(climb_temp_offset_c)]
        st.caption(
            f"{climb_reference} | {CLIMB_SCHEDULE_METADATA[climb_schedule_id]['label']} | cumulative climb source table"
        )
        climb_df = pd.DataFrame(
            [
                {
                    "Altitude": f"{int(row['pressure_altitude_ft']):,} ft",
                    "Time": f"{float(row['time_minutes']):.2f} min",
                    "Fuel Used": float(row["fuel_used_gal"]),
                    "Distance": float(row["distance_nm"]),
                }
                for row in reversed(climb_rows)
            ]
        )
        st.dataframe(
            climb_df,
            hide_index=True,
            width="stretch",
            column_config={
                "Fuel Used": st.column_config.NumberColumn("Fuel Used", format="%.1f gal"),
                "Distance": st.column_config.NumberColumn("Distance", format="%.0f NM"),
            },
        )

    with descent_table_tab:
        descent_rate_for_display = st.selectbox(
            "Descent source column",
            options=list(DESCENT_RATES_FPM),
            index=list(DESCENT_RATES_FPM).index(active_descent_rate_fpm_for_calc),
            format_func=lambda rate: f"{int(rate):,} fpm",
            key="perf_tables_descent_rate_fpm",
        )
        descent_rows = descent_rows_for_rate(official_pim_profile.default_descent_profile_id, int(descent_rate_for_display))
        st.caption(
            f"{DESCENT_PROFILE_METADATA[official_pim_profile.default_descent_profile_id]['table_reference']} | "
            f"{DESCENT_PROFILE_METADATA[official_pim_profile.default_descent_profile_id].get('source_label', DESCENT_PROFILE_METADATA[official_pim_profile.default_descent_profile_id]['label'])} | "
            "cumulative descent source table"
        )
        descent_df = pd.DataFrame(
            [
                {
                    "Altitude": f"{int(row['pressure_altitude_ft']):,} ft",
                    "Time": f"{float(row['time_minutes']):.2f} min",
                    "Fuel Used": float(row["fuel_used_gal"]),
                    "Distance": float(row["distance_nm"]),
                }
                for row in descent_rows
            ]
        )
        st.dataframe(
            descent_df,
            hide_index=True,
            width="stretch",
            column_config={
                "Fuel Used": st.column_config.NumberColumn("Fuel Used", format="%.1f gal"),
                "Distance": st.column_config.NumberColumn("Distance", format="%.0f NM"),
            },
        )

    with notes_table_tab:
        st.caption(official_pim_profile.summary)
        st.caption(official_pim_profile.notes)
        st.caption(f"PDF path: {SOURCE_METADATA['pdf_path']}")
        st.markdown(
            (
                "<p class='section-copy'>Mission calculations now interpolate across the official cruise and "
                "climb ISA-deviation tables when forecast temperatures are available from NOAA FD windtemps. "
                "The full published weight and schedule variants are preserved in the source layer for later "
                "gross-weight and profile-management work.</p>"
            ),
            unsafe_allow_html=True,
        )

with weather_tab:
    st.markdown(
        (
            "<p class='section-copy'>Audit the raw NOAA feeds behind the brief. This view is for trust, "
            "troubleshooting, and future calibration work.</p>"
        ),
        unsafe_allow_html=True,
    )
    weather_cards = st.columns(4)
    with weather_cards[0]:
        _render_insight_card(
            "Windtemp feed",
            f"{len(weather.windtemps)} decoded points",
            f"Region {weather.windtemp_region} | Level {weather.windtemp_level} | Cycle {weather.windtemp_fcst}",
        )
    with weather_cards[1]:
        _render_insight_card(
            "Hazard polygons",
            str(len(weather.hazard_areas)),
            "Combined G-AIRMET, AIRSIGMET, TCF, CWA, and PIREP/AIREP records.",
        )
    with weather_cards[2]:
        _render_insight_card(
            "Wind source",
            wind_source_status,
            wind_source_detail,
            tone_class="tone-low" if route_wind_model is None else "tone-clear",
        )
    with weather_cards[3]:
        _render_insight_card(
            "Weather/data confidence",
            weather.data_confidence,
            "Based on NOAA feed health, not observed mission risk.",
            tone_class=_confidence_tone_class(weather.data_confidence),
        )

    if weather.feed_statuses:
        feed_rows = [
            {
                "Feed": status.name,
                "Status": status.status.title(),
                "Records": status.row_count,
                "Parameters": ", ".join(
                    f"{key}={value}" for key, value in status.params.items() if value not in (None, "")
                ),
                "Error": status.error_message or "",
                "Issue Time": status.issue_time_utc.strftime("%Y-%m-%d %H:%MZ") if status.issue_time_utc else "",
                "Valid Window": (
                    f"{status.valid_from_utc.strftime('%Y-%m-%d %H:%MZ')} – "
                    f"{status.valid_to_utc.strftime('%Y-%m-%d %H:%MZ')}"
                    if status.valid_from_utc and status.valid_to_utc
                    else ""
                ),
            }
            for status in weather.feed_statuses.values()
        ]
        with st.expander("NOAA feed health", expanded=weather.data_confidence in {"Low", "Unknown"}):
            st.dataframe(pd.DataFrame(feed_rows), hide_index=True, width="stretch")

    airport_entries: list[tuple[str, str]] = [(departure_airport.icao, "Departure")]
    if fuel_stop_segments:
        for leg_number, fuel_segment in enumerate(fuel_stop_segments, start=1):
            leg_destination = fuel_segment.end_identifier
            role = "Final destination" if leg_number == len(fuel_stop_segments) else f"Leg {leg_number} fuel stop"
            airport_entries.append((leg_destination, role))
            leg_alternate = fuel_stop_alternates.get(leg_destination)
            if leg_alternate:
                airport_entries.append((leg_alternate, f"Leg {leg_number} alternate"))
    else:
        airport_entries.append((destination_airport.icao, "Destination"))
    if alternate_airport is not None:
        airport_entries.append((alternate_airport.icao, "Mission alternate"))
    known_airports = {icao for icao, _role in airport_entries}
    airport_entries.extend(
        (icao, "Additional mission weather")
        for icao in sorted(mission_weather_airports)
        if icao not in known_airports
    )
    airport_roles: dict[str, list[str]] = {}
    airport_icaos = []
    for icao, role in airport_entries:
        if icao not in airport_roles:
            airport_icaos.append(icao)
            airport_roles[icao] = []
        if role not in airport_roles[icao]:
            airport_roles[icao].append(role)
    airport_cols = []
    for row_start in range(0, len(airport_icaos), 3):
        airport_cols.extend(st.columns(min(3, len(airport_icaos) - row_start)))
    for idx, icao in enumerate(airport_icaos):
        wx = weather.airports.get(icao)
        with airport_cols[idx]:
            with st.container(border=True):
                st.markdown(f"### {icao}")
                st.caption(" · ".join(airport_roles[icao]))
                risk_cols = st.columns(2)
                for risk_col, risk_title, risk in (
                    (risk_cols[0], "METAR risk", wx.metar_risk if wx else None),
                    (risk_cols[1], "TAF risk (worst of full TAF)", wx.taf_risk if wx else None),
                ):
                    with risk_col:
                        if risk is None:
                            _render_insight_card(risk_title, "Unknown", "No terminal product returned.", tone_class="tone-high")
                        else:
                            detail = "; ".join(risk.reasons[:3]) if risk.reasons else "No scored terminal trigger."
                            _render_insight_card(
                                risk_title,
                                risk.label,
                                detail,
                                tone_class=_tone_class_for_score(int(risk.score)),
                            )

                st.markdown("**METAR**")
                st.caption(
                    wx.metar_summary if wx and wx.metar_summary else "No METAR time translation available."
                )
                _render_wrapped_raw(
                    wx.metar_raw if wx else None,
                    "No METAR returned.",
                )

                st.markdown("**TAF**")
                st.caption(
                    wx.taf_summary if wx and wx.taf_summary else "No TAF time translation available."
                )
                _render_wrapped_raw(
                    wx.taf_raw if wx else None,
                    "No TAF returned.",
                )

    if weather.windtemps:
        sample_rows = [
            {
                "Station": p.station,
                "Altitude (ft)": p.altitude_ft,
                "Dir (deg)": p.direction_deg,
                "Speed (kt)": p.speed_kt,
                "Temp (C)": p.temperature_c,
                "Code": p.raw_code,
            }
            for p in weather.windtemps[:20]
        ]
        with st.expander("Windtemp sample points", expanded=False):
            st.dataframe(pd.DataFrame(sample_rows), hide_index=True, width="stretch")
    else:
        _render_insight_card(
            "Windtemp samples",
            "Unavailable",
            "No decoded winds aloft points were available for the current cycle.",
            tone_class="tone-low",
        )
