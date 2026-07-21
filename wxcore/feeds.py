"""NOAA/AWC feed fetching plus METAR/TAF/windtemp/hazard normalization."""

from __future__ import annotations

import datetime as dt
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import lru_cache

import pytz
import requests

from route_planning import (
    RoutePlan,
    route_midpoint_lat_lon,
)

from .models import (
    DEFAULT_TZ,
    MULTI_REGION_ROUTE_DISTANCE_NM,
    _safe_float,
    _safe_int,
    GAIRMET_SNAPSHOT_HALF_WINDOW,
    PIREP_ALTITUDE_HALF_BAND_FT,
    PIREP_VALID_BEFORE,
    PIREP_VALID_AFTER,
    PIREP_BBOX_PADDING_SINGLE_DEG,
    PIREP_BBOX_PADDING_MULTI_DEG,
    DEFAULT_HAZARD_TOP_FT,
    NOAA_API_BASE_URL,
    WINDTEMP_TOKEN_PATTERN,
    WINDTEMP_GROUP_PATTERN,
    AirportData,
    TerminalRisk,
    TerminalForecastPeriod,
    AirportWeather,
    WindTempPoint,
    FeedStatus,
    NoaaWeather,
    HazardArea,
    normalize_icao,
)

from .geo import (
    great_circle_distance_nm,
    _polygon_from_latlon_dicts,
    _polygons_from_geojson_geometry,
    _circle_polygon_nm,
    hazard_label,
)


def _format_time_12h(value: dt.datetime) -> str:
    """Format a local datetime as compact 12-hour clock text."""

    return value.strftime("%I:%M %p").lstrip("0")


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
    return TerminalRisk(source=source, score=score, label=hazard_label(score), reasons=tuple(reasons))


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
    return TerminalRisk(source="TAF", score=max_score, label=hazard_label(max_score), reasons=tuple(reasons))


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
        # AWC numeric coding observed live 2026-07-20: convective SIGMETs carry
        # severity=5 (High). A 4 has not been observed on the wire; SIGMET-class
        # products are inherently significant, so >=4 is treated as High rather
        # than risking a severe advisory scoring Moderate.
        if value >= 4:
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
        top_ft = DEFAULT_HAZARD_TOP_FT

    if top_ft < base_ft:
        base_ft, top_ft = top_ft, base_ft
    return base_ft, top_ft


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
        if hazard_type in surface_hazard_bands and (base_ft, top_ft) == (0, DEFAULT_HAZARD_TOP_FT):
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
            high_ft = DEFAULT_HAZARD_TOP_FT
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

            tops_ft = _parse_altitude_feet(properties.get("tops")) or DEFAULT_HAZARD_TOP_FT
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
                top_ft = report_altitude_ft + PIREP_ALTITUDE_HALF_BAND_FT if report_altitude_ft else DEFAULT_HAZARD_TOP_FT
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


def windtemp_cycle_correction(
    weather: NoaaWeather,
    departure_dt_utc: dt.datetime,
) -> str | None:
    """Return the FB cycle implied by the fetched product's own issue time.

    The first fetch selects a cycle relative to wall-clock now; once the product's
    DATA-BASED-ON time is known, the ETD may map to a different issued cycle. None
    means no correction basis is available.
    """

    status = weather.feed_statuses.get("windtemp")
    if status is None or status.issue_time_utc is None:
        return None
    return select_windtemp_forecast_cycle(departure_dt_utc, now_utc=status.issue_time_utc)


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
