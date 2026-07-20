"""Parse and expose official Daher TBM 960 PIM performance tables from the vendored PDF."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader

CRUISE_WEIGHTS_LB = (5500, 6300, 7100, 7300)
CLIMB_WEIGHTS_LB = (5794, 6579, 7394, 7615)
DESCENT_RATES_FPM = (1500, 2000, 2500)
EXPECTED_CRUISE_ALTITUDES_FT = (
    0, 5000, 10000, 15000, 18000, 20000, 21000, 22000, 23000,
    24000, 25000, 26000, 27000, 28000, 29000, 30000, 31000,
)
EXPECTED_VERTICAL_ALTITUDES_FT = (
    0, 2000, 4000, 6000, 8000, 10000, 12000, 14000, 16000,
    18000, 20000, 22000, 24000, 26000, 28000, 30000, 31000,
)

PIM_PDF_PATH = Path(__file__).resolve().parent / "assets" / "manuals" / "PIM_TBM960E0R1_DRAFT.pdf"
PIM_SNAPSHOT_PATH = Path(__file__).resolve().parent / "assets" / "pim_tables_snapshot.json"
PIM_SNAPSHOT_SCHEMA_VERSION = 1

CRUISE_MODE_METADATA = {
    "max": {
        "label": "Max Cruise",
        "condition_summary": "INERT SEP OFF, BLEED AUTO, P2.5 HI and P3 OFF",
        "table_references_by_temp_delta_c": {
            -20: "Table 5.11.2",
            -10: "Table 5.11.3",
            -5: "Table 5.11.4",
            0: "Table 5.11.5",
            5: "Table 5.11.6",
            10: "Table 5.11.7",
            20: "Table 5.11.8",
        },
    },
    "normal": {
        "label": "Recommended Cruise",
        "condition_summary": "INERT SEP OFF, BLEED AUTO, P2.5 HI and P3 OFF",
        "table_references_by_temp_delta_c": {
            -20: "Table 5.11.31",
            -10: "Table 5.11.32",
            -5: "Table 5.11.33",
            0: "Table 5.11.34",
            5: "Table 5.11.35",
            10: "Table 5.11.36",
            20: "Table 5.11.37",
        },
    },
    "economy": {
        "label": "Long Range Cruise",
        "condition_summary": "INERT SEP OFF, BLEED AUTO, P2.5 HI and P3 OFF",
        "table_references_by_temp_delta_c": {
            -20: "Table 5.11.46",
            -10: "Table 5.11.47",
            -5: "Table 5.11.48",
            0: "Table 5.11.49",
            5: "Table 5.11.50",
            10: "Table 5.11.51",
            20: "Table 5.11.52",
        },
    },
}

CLIMB_SCHEDULE_METADATA = {
    "124_kias": {
        "label": "124 KIAS",
        "nominal_ias_kts": 124,
        "table_references_by_temp_delta_c": {
            -20: "Table 5.10.4",
            0: "Table 5.10.5",
            20: "Table 5.10.6",
        },
    },
    "170_kias_m0_40": {
        "label": "170 KIAS / M 0.40",
        "nominal_ias_kts": 170,
        "table_references_by_temp_delta_c": {
            -20: "Table 5.10.7",
            0: "Table 5.10.8",
            20: "Table 5.10.9",
        },
    },
}

DESCENT_PROFILE_METADATA = {
    "230_kcas": {
        # Keep the original key so the underlying PIM source table mapping stays stable even
        # though the app presents this as the user's preferred default descent profile.
        "label": "220 KIAS",
        "nominal_ias_kts": 220,
        "table_reference": "Table 5.12.1",
        "source_label": "230 KCAS source table",
        "available_vertical_rates_fpm": DESCENT_RATES_FPM,
    }
}

SOURCE_METADATA = {
    "source": "Daher TBM 960 PIM TBM960E0R1 (DRAFT)",
    "pdf_path": str(PIM_PDF_PATH),
    "notes": (
        "Repo-local extracted climb, cruise, and descent source tables from the official Daher "
        "TBM 960 PIM performance chapter for future reuse."
    ),
}

_CRUISE_PAGE_MAP = {
    "max": {-20: 499, -10: 500, -5: 501, 0: 502, 5: 503, 10: 504, 20: 505},
    "normal": {-20: 529, -10: 530, -5: 531, 0: 532, 5: 533, 10: 534, 20: 535},
    "economy": {-20: 545, -10: 546, -5: 547, 0: 548, 5: 549, 10: 550, 20: 551},
}
_CLIMB_PAGE_MAP = {
    "124_kias": {-20: 487, 0: 488, 20: 489},
    "170_kias_m0_40": {-20: 490, 0: 491, 20: 492},
}
_DESCENT_PAGE_MAP = {"230_kcas": 559}

_CRUISE_LINE_RE = re.compile(
    r"^(?P<alt>SL|\d{1,2},\d{3})\s+(?P<oat>-?\d+)\s+(?P<trq>\d+)\s+(?P<fuel>[\d.]+)"
    r"\s+(?P<w1_ias>\d+)\s+(?P<w1_tas>\d+)\s+(?P<w2_ias>\d+)\s+(?P<w2_tas>\d+)"
    r"\s+(?P<w3_ias>\d+)\s+(?P<w3_tas>\d+)\s+(?P<w4_ias>\d+)\s+(?P<w4_tas>\d+)$"
)
_CLIMB_LINE_RE = re.compile(
    r"^(?P<alt>SL|\d{1,2},\d{3})"
    r"\s+(?P<w1_time>\d{2}:\d{2})\s+(?P<w1_fuel>[\d.]+)\s+(?P<w1_dist>\d+)"
    r"\s+(?P<w2_time>\d{2}:\d{2})\s+(?P<w2_fuel>[\d.]+)\s+(?P<w2_dist>\d+)"
    r"\s+(?P<w3_time>\d{2}:\d{2})\s+(?P<w3_fuel>[\d.]+)\s+(?P<w3_dist>\d+)"
    r"\s+(?P<w4_time>\d{2}:\d{2})\s+(?P<w4_fuel>[\d.]+)\s+(?P<w4_dist>\d+)$"
)
_DESCENT_LINE_RE = re.compile(
    r"^(?P<alt>SL|\d{1,2},\d{3})"
    r"\s+(?P<r1_time>\d{2}:\d{2})\s+(?P<r1_fuel>[\d.]+)\s+(?P<r1_dist>\d+)"
    r"\s+(?P<r2_time>\d{2}:\d{2})\s+(?P<r2_fuel>[\d.]+)\s+(?P<r2_dist>\d+)"
    r"\s+(?P<r3_time>\d{2}:\d{2})\s+(?P<r3_fuel>[\d.]+)\s+(?P<r3_dist>\d+)$"
)


# Parsed row models mirror the structure printed in the source manual.
@dataclass(frozen=True)
class CruiseTableRow:
    """One source cruise row with every published weight column preserved."""

    pressure_altitude_ft: int
    oat_c: int
    torque_pct: int
    fuel_flow_gph: float
    weights_lb: dict[int, dict[str, int]]


@dataclass(frozen=True)
class CumulativeVerticalRow:
    """One climb row where time, fuel, and distance are cumulative from takeoff."""

    pressure_altitude_ft: int
    weights_lb: dict[int, dict[str, float]]


@dataclass(frozen=True)
class DescentCumulativeRow:
    """One descent row keyed by the published descent-rate columns."""

    pressure_altitude_ft: int
    profiles_by_rate_fpm: dict[int, dict[str, float]]


def _source_pdf_sha256() -> str:
    """Return the source-manual fingerprint used to accept or invalidate a snapshot."""

    digest = hashlib.sha256()
    with PIM_PDF_PATH.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@lru_cache(maxsize=1)
def _pim_snapshot() -> dict | None:
    """Load the checked-in parsed tables when they match the current source PDF exactly."""

    if os.environ.get("CODEX_REBUILD_PIM_SNAPSHOT") == "1" or not PIM_SNAPSHOT_PATH.exists():
        return None
    payload = json.loads(PIM_SNAPSHOT_PATH.read_text(encoding="utf-8"))
    if payload.get("schema_version") != PIM_SNAPSHOT_SCHEMA_VERSION:
        return None
    # Public deployments do not vendor the copyrighted PDF; the snapshot then stands on
    # its stored provenance hash plus the row-level validators applied at parse time.
    if PIM_PDF_PATH.exists() and payload.get("source_pdf_sha256") != _source_pdf_sha256():
        return None
    return payload


def _parse_altitude_ft(value: str) -> int:
    """Convert a PIM altitude cell such as `SL` or `10,000` into feet."""

    if value == "SL":
        return 0
    return int(value.replace(",", ""))


def parse_mmss_to_minutes(value: str) -> float:
    """Convert a `MM:SS` table value into decimal minutes."""

    minutes_text, seconds_text = value.split(":")
    return int(minutes_text) + (int(seconds_text) / 60.0)


def _validate_altitude_rows(rows: list, *, expected_altitudes_ft: tuple[int, ...], table_label: str) -> None:
    """Reject incomplete or shifted PDF parses before they can enter interpolation."""

    actual_altitudes_ft = tuple(sorted(row.pressure_altitude_ft for row in rows))
    if actual_altitudes_ft != expected_altitudes_ft:
        raise ValueError(
            f"Incomplete Daher PIM parse for {table_label}: expected altitude rows "
            f"{expected_altitudes_ft!r}, received {actual_altitudes_ft!r}."
        )


def _validate_cruise_values(rows: list[CruiseTableRow], *, table_label: str) -> None:
    """Reject nonpositive cruise values regardless of whether rows came from JSON or PDF."""

    if any(
        row.fuel_flow_gph <= 0
        or any(values["tas_kts"] <= 0 for values in row.weights_lb.values())
        for row in rows
    ):
        raise ValueError(f"Invalid zero performance value in {table_label}.")


def _validate_cumulative_values(
    rows: list[CumulativeVerticalRow] | list[DescentCumulativeRow],
    *,
    profile_attribute: str,
    table_label: str,
) -> None:
    """Reject negative or decreasing cumulative time, fuel, and distance values."""

    metrics = ("time_minutes", "fuel_used_gal", "distance_nm")
    ordered_rows = sorted(rows, key=lambda row: row.pressure_altitude_ft)
    for profile_key in getattr(ordered_rows[0], profile_attribute):
        previous = {metric: -1.0 for metric in metrics}
        for row in ordered_rows:
            values = getattr(row, profile_attribute)[profile_key]
            for metric in metrics:
                value = float(values[metric])
                if value < 0.0 or value < previous[metric]:
                    raise ValueError(
                        f"Invalid cumulative {metric} in {table_label} profile {profile_key}: {value}."
                    )
                previous[metric] = value


@lru_cache(maxsize=1)
def _pdf_reader() -> PdfReader:
    """Open the vendored Daher PIM PDF through pypdf with process-local caching."""

    if not PIM_PDF_PATH.exists():
        raise FileNotFoundError(f"Missing Daher PIM PDF: {PIM_PDF_PATH}")
    return PdfReader(str(PIM_PDF_PATH))


@lru_cache(maxsize=None)
def _page_lines(page_number: int) -> tuple[str, ...]:
    """Extract normalized text lines from a one-based PIM PDF page number."""

    # The PDF extractor can emit uneven whitespace, so normalize lines before regex parsing.
    text = (_pdf_reader().pages[page_number - 1].extract_text() or "").encode("ascii", "replace").decode("ascii")
    return tuple(" ".join(line.split()) for line in text.splitlines() if line.strip())


@lru_cache(maxsize=None)
def list_available_cruise_modes() -> tuple[str, ...]:
    """Return cruise-mode IDs that have source pages in the PIM parser."""

    return tuple(_CRUISE_PAGE_MAP.keys())


@lru_cache(maxsize=None)
def list_available_cruise_temperature_offsets(mode_id: str) -> tuple[int, ...]:
    """Return ISA-offset slices available for one cruise mode."""

    return tuple(sorted(_CRUISE_PAGE_MAP.get(mode_id, {}).keys()))


@lru_cache(maxsize=None)
def list_available_climb_schedules() -> tuple[str, ...]:
    """Return climb-schedule IDs that have source pages in the PIM parser."""

    return tuple(_CLIMB_PAGE_MAP.keys())


@lru_cache(maxsize=None)
def list_available_climb_temperature_offsets(schedule_id: str) -> tuple[int, ...]:
    """Return ISA-offset slices available for one climb schedule."""

    return tuple(sorted(_CLIMB_PAGE_MAP.get(schedule_id, {}).keys()))


@lru_cache(maxsize=None)
def list_available_descent_profiles() -> tuple[str, ...]:
    """Return descent-profile IDs that have source pages in the PIM parser."""

    return tuple(_DESCENT_PAGE_MAP.keys())


@lru_cache(maxsize=None)
def parse_cruise_table(mode_id: str, temperature_offset_c: int) -> tuple[CruiseTableRow, ...]:
    """Parse one published cruise table page, preserving every supported weight column."""

    snapshot = _pim_snapshot()
    snapshot_rows = (snapshot or {}).get("cruise", {}).get(f"{mode_id}|{temperature_offset_c}")
    if snapshot_rows is not None:
        rows = [
            CruiseTableRow(
                pressure_altitude_ft=int(row["pressure_altitude_ft"]),
                oat_c=int(row["oat_c"]),
                torque_pct=int(row["torque_pct"]),
                fuel_flow_gph=float(row["fuel_flow_gph"]),
                weights_lb={int(weight): values for weight, values in row["weights_lb"].items()},
            )
            for row in snapshot_rows
        ]
        table_label = f"cruise {mode_id} ISA {temperature_offset_c:+d}"
        _validate_altitude_rows(
            rows,
            expected_altitudes_ft=EXPECTED_CRUISE_ALTITUDES_FT,
            table_label=table_label,
        )
        _validate_cruise_values(rows, table_label=table_label)
        return tuple(rows)

    page_number = _CRUISE_PAGE_MAP[mode_id][temperature_offset_c]
    rows: list[CruiseTableRow] = []
    for line in _page_lines(page_number):
        match = _CRUISE_LINE_RE.match(line)
        if match is None:
            continue
        weights_lb = {}
        for index, weight_lb in enumerate(CRUISE_WEIGHTS_LB, start=1):
            weights_lb[weight_lb] = {
                "ias_kts": int(match.group(f"w{index}_ias")),
                "tas_kts": int(match.group(f"w{index}_tas")),
            }
        rows.append(
            CruiseTableRow(
                pressure_altitude_ft=_parse_altitude_ft(match.group("alt")),
                oat_c=int(match.group("oat")),
                torque_pct=int(match.group("trq")),
                fuel_flow_gph=float(match.group("fuel")),
                weights_lb=weights_lb,
            )
        )
    _validate_altitude_rows(
        rows,
        expected_altitudes_ft=EXPECTED_CRUISE_ALTITUDES_FT,
        table_label=f"cruise {mode_id} ISA {temperature_offset_c:+d}",
    )
    _validate_cruise_values(rows, table_label=f"cruise {mode_id} ISA {temperature_offset_c:+d}")
    return tuple(rows)


@lru_cache(maxsize=None)
def parse_climb_table(schedule_id: str, temperature_offset_c: int) -> tuple[CumulativeVerticalRow, ...]:
    """Parse one published climb table page as cumulative rows by weight."""

    snapshot = _pim_snapshot()
    snapshot_rows = (snapshot or {}).get("climb", {}).get(f"{schedule_id}|{temperature_offset_c}")
    if snapshot_rows is not None:
        rows = [
            CumulativeVerticalRow(
                pressure_altitude_ft=int(row["pressure_altitude_ft"]),
                weights_lb={int(weight): values for weight, values in row["weights_lb"].items()},
            )
            for row in snapshot_rows
        ]
        _validate_altitude_rows(
            rows,
            expected_altitudes_ft=EXPECTED_VERTICAL_ALTITUDES_FT,
            table_label=f"climb {schedule_id} ISA {temperature_offset_c:+d}",
        )
        _validate_cumulative_values(
            rows,
            profile_attribute="weights_lb",
            table_label=f"climb {schedule_id} ISA {temperature_offset_c:+d}",
        )
        return tuple(rows)

    page_number = _CLIMB_PAGE_MAP[schedule_id][temperature_offset_c]
    rows: list[CumulativeVerticalRow] = []
    for line in _page_lines(page_number):
        match = _CLIMB_LINE_RE.match(line)
        if match is None:
            continue
        weights_lb = {}
        for index, weight_lb in enumerate(CLIMB_WEIGHTS_LB, start=1):
            weights_lb[weight_lb] = {
                "time_minutes": parse_mmss_to_minutes(match.group(f"w{index}_time")),
                "fuel_used_gal": float(match.group(f"w{index}_fuel")),
                "distance_nm": float(match.group(f"w{index}_dist")),
            }
        rows.append(
            CumulativeVerticalRow(
                pressure_altitude_ft=_parse_altitude_ft(match.group("alt")),
                weights_lb=weights_lb,
            )
        )
    _validate_altitude_rows(
        rows,
        expected_altitudes_ft=EXPECTED_VERTICAL_ALTITUDES_FT,
        table_label=f"climb {schedule_id} ISA {temperature_offset_c:+d}",
    )
    _validate_cumulative_values(
        rows,
        profile_attribute="weights_lb",
        table_label=f"climb {schedule_id} ISA {temperature_offset_c:+d}",
    )
    return tuple(rows)


@lru_cache(maxsize=None)
def parse_descent_table(profile_id: str) -> tuple[DescentCumulativeRow, ...]:
    """Parse the published descent table and keep each supported rate column."""

    snapshot = _pim_snapshot()
    snapshot_rows = (snapshot or {}).get("descent", {}).get(profile_id)
    if snapshot_rows is not None:
        rows = [
            DescentCumulativeRow(
                pressure_altitude_ft=int(row["pressure_altitude_ft"]),
                profiles_by_rate_fpm={
                    int(rate): values for rate, values in row["profiles_by_rate_fpm"].items()
                },
            )
            for row in snapshot_rows
        ]
        _validate_altitude_rows(
            rows,
            expected_altitudes_ft=EXPECTED_VERTICAL_ALTITUDES_FT,
            table_label=f"descent {profile_id}",
        )
        _validate_cumulative_values(
            rows,
            profile_attribute="profiles_by_rate_fpm",
            table_label=f"descent {profile_id}",
        )
        return tuple(rows)

    page_number = _DESCENT_PAGE_MAP[profile_id]
    rows: list[DescentCumulativeRow] = []
    for line in _page_lines(page_number):
        match = _DESCENT_LINE_RE.match(line)
        if match is None:
            continue
        rows.append(
            DescentCumulativeRow(
                pressure_altitude_ft=_parse_altitude_ft(match.group("alt")),
                profiles_by_rate_fpm={
                    1500: {
                        "time_minutes": parse_mmss_to_minutes(match.group("r1_time")),
                        "fuel_used_gal": float(match.group("r1_fuel")),
                        "distance_nm": float(match.group("r1_dist")),
                    },
                    2000: {
                        "time_minutes": parse_mmss_to_minutes(match.group("r2_time")),
                        "fuel_used_gal": float(match.group("r2_fuel")),
                        "distance_nm": float(match.group("r2_dist")),
                    },
                    2500: {
                        "time_minutes": parse_mmss_to_minutes(match.group("r3_time")),
                        "fuel_used_gal": float(match.group("r3_fuel")),
                        "distance_nm": float(match.group("r3_dist")),
                    },
                },
            )
        )
    _validate_altitude_rows(
        rows,
        expected_altitudes_ft=EXPECTED_VERTICAL_ALTITUDES_FT,
        table_label=f"descent {profile_id}",
    )
    _validate_cumulative_values(
        rows,
        profile_attribute="profiles_by_rate_fpm",
        table_label=f"descent {profile_id}",
    )
    return tuple(rows)


@lru_cache(maxsize=None)
def cruise_rows_for_weight(
    mode_id: str,
    temperature_offset_c: int,
    weight_lb: int,
) -> tuple[dict[str, float | int], ...]:
    """Project parsed cruise rows down to the one weight used by the caller."""

    return tuple(
        {
            "pressure_altitude_ft": row.pressure_altitude_ft,
            "oat_c": row.oat_c,
            "torque_pct": row.torque_pct,
            "fuel_flow_gph": row.fuel_flow_gph,
            "ias_kts": row.weights_lb[weight_lb]["ias_kts"],
            "tas_kts": row.weights_lb[weight_lb]["tas_kts"],
        }
        for row in parse_cruise_table(mode_id, temperature_offset_c)
    )


@lru_cache(maxsize=None)
def climb_rows_for_weight(
    schedule_id: str,
    temperature_offset_c: int,
    weight_lb: int,
) -> tuple[dict[str, float | int], ...]:
    """Project parsed climb rows down to the one weight used by the caller."""

    return tuple(
        {
            "pressure_altitude_ft": row.pressure_altitude_ft,
            "time_minutes": row.weights_lb[weight_lb]["time_minutes"],
            "fuel_used_gal": row.weights_lb[weight_lb]["fuel_used_gal"],
            "distance_nm": row.weights_lb[weight_lb]["distance_nm"],
        }
        for row in parse_climb_table(schedule_id, temperature_offset_c)
    )


@lru_cache(maxsize=None)
def descent_rows_for_rate(profile_id: str, vertical_rate_fpm: int) -> tuple[dict[str, float | int], ...]:
    """Project parsed descent rows down to the requested vertical-rate column."""

    return tuple(
        {
            "pressure_altitude_ft": row.pressure_altitude_ft,
            "time_minutes": row.profiles_by_rate_fpm[vertical_rate_fpm]["time_minutes"],
            "fuel_used_gal": row.profiles_by_rate_fpm[vertical_rate_fpm]["fuel_used_gal"],
            "distance_nm": row.profiles_by_rate_fpm[vertical_rate_fpm]["distance_nm"],
        }
        for row in parse_descent_table(profile_id)
    )
