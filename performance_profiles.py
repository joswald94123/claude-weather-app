"""Performance-profile models and interpolation helpers built from Daher source tables."""

from __future__ import annotations

from dataclasses import dataclass, replace

from daher_pim_tables import (
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

DEFAULT_PERFORMANCE_PROFILE_ID = "tbm-960-pim"
DEFAULT_CRUISE_MODE_ID = "max"
DEFAULT_CLIMB_SCHEDULE_ID = "124_kias"
DEFAULT_DESCENT_PROFILE_ID = "230_kcas"
DEFAULT_DESCENT_RATE_FPM = 1500
DEFAULT_STARTUP_TAXI_FUEL_GAL = 8.0
DEFAULT_CRUISE_WEIGHT_LB = 7100
DEFAULT_CLIMB_WEIGHT_LB = 7394
# Keeps band_hours = altitude_delta / rate finite; real PIM band rates are always
# several hundred fpm, so this floor never binds on published data.
MIN_PLANNING_RATE_FPM = 100


# These data models preserve the official table families while keeping sampling call sites simple.
@dataclass(frozen=True)
class CruisePerformanceRow:
    """One cruise data point at a flight level for a specific mode/temperature slice."""

    flight_level: int
    tas_kts: float
    fuel_gph: float
    ias_kts: int | None = None
    torque_pct: int | None = None
    oat_c: int | None = None
    temperature_offset_c: float = 0.0
    source: str = "Built-in baseline"
    confidence: str = "medium"
    verified: bool = False
    notes: str = ""


@dataclass(frozen=True)
class VerticalPerformanceRow:
    """One climb or descent band derived from cumulative PIM performance tables."""

    start_altitude_ft: int
    end_altitude_ft: int
    ias_kts: int
    rate_fpm: int
    fuel_gph: float
    tas_kts: float | None = None
    distance_nm: float | None = None
    time_minutes: float | None = None
    temperature_offset_c: float | None = None
    source: str = "Built-in baseline"
    confidence: str = "medium"
    verified: bool = False
    notes: str = ""


@dataclass(frozen=True)
class CruiseModeProfile:
    """A named Daher cruise-mode table family, including temp and weight variants."""

    mode_id: str
    label: str
    description: str
    cruise_rows: tuple[CruisePerformanceRow, ...]
    cruise_rows_by_temp_offset_c: dict[int, tuple[CruisePerformanceRow, ...]]
    default_weight_lb: int
    available_weights_lb: tuple[int, ...]
    available_temperature_offsets_c: tuple[int, ...]
    table_reference: str = ""
    table_summary: str = ""
    table_references_by_temp_offset_c: dict[int, str] | None = None
    condition_summary: str = ""


@dataclass(frozen=True)
class ClimbScheduleProfile:
    """A named Daher climb schedule with altitude-band performance rows."""

    schedule_id: str
    label: str
    description: str
    climb_rows: tuple[VerticalPerformanceRow, ...]
    climb_rows_by_temp_offset_c: dict[int, tuple[VerticalPerformanceRow, ...]]
    nominal_ias_kts: int
    default_weight_lb: int
    available_weights_lb: tuple[int, ...]
    available_temperature_offsets_c: tuple[int, ...]
    table_reference: str = ""
    table_summary: str = ""
    table_references_by_temp_offset_c: dict[int, str] | None = None


@dataclass(frozen=True)
class DescentProfile:
    """A named Daher descent schedule keyed by available vertical-speed columns."""

    profile_id: str
    label: str
    description: str
    descent_rows: tuple[VerticalPerformanceRow, ...]
    descent_rows_by_rate_fpm: dict[int, tuple[VerticalPerformanceRow, ...]]
    nominal_ias_kts: int
    available_vertical_rates_fpm: tuple[int, ...]
    table_reference: str = ""
    table_summary: str = ""


@dataclass(frozen=True)
class AircraftPerformanceProfile:
    """Complete aircraft profile bundle consumed by mission calculations."""

    profile_id: str
    label: str
    summary: str
    aircraft: str
    default_cruise_mode_id: str
    default_climb_schedule_id: str
    default_descent_profile_id: str
    default_descent_rate_fpm: int
    cruise_modes: tuple[CruiseModeProfile, ...]
    climb_schedules: tuple[ClimbScheduleProfile, ...]
    descent_profiles: tuple[DescentProfile, ...]
    climb_rows: tuple[VerticalPerformanceRow, ...]
    descent_rows: tuple[VerticalPerformanceRow, ...]
    fixed_fuel_gal: float
    cruise_weight_lb: int
    climb_weight_lb: int
    climb_table_reference: str = ""
    climb_table_summary: str = ""
    descent_table_reference: str = ""
    descent_table_summary: str = ""
    source: str = "Built-in baseline"
    confidence: str = "medium"
    verified: bool = False
    notes: str = ""


@dataclass(frozen=True)
class CruisePerformanceSample:
    """Interpolated cruise result returned to the mission engine and UI."""

    flight_level: int
    tas_kts: float
    fuel_gph: float
    mode_id: str
    mode_label: str
    ias_kts: int | None = None
    torque_pct: int | None = None
    oat_c: int | None = None
    temperature_offset_c: float = 0.0
    table_reference: str = ""


# Interpolation is centralized so climb, descent, and cruise all follow the same rules.
def _interpolate_scalar(
    value_low: float,
    value_high: float,
    *,
    x_low: float,
    x_high: float,
    x_target: float,
) -> float:
    """Linearly interpolate one scalar and clamp degenerate intervals safely."""

    if x_high == x_low:
        return value_low
    ratio = (x_target - x_low) / (x_high - x_low)
    return value_low + ((value_high - value_low) * ratio)


def _nearest_bounds(values: tuple[int, ...], target: float) -> tuple[int, int, float]:
    """Return bracketing table keys and the clamped target used for interpolation."""

    if not values:
        raise ValueError(f"No table keys available to bracket interpolation target {target}.")

    ordered_values = tuple(sorted(values))
    for value in ordered_values:
        if float(value) == float(target):
            return value, value, float(value)
    if target <= ordered_values[0]:
        return ordered_values[0], ordered_values[0], float(ordered_values[0])
    if target >= ordered_values[-1]:
        return ordered_values[-1], ordered_values[-1], float(ordered_values[-1])

    for low_value, high_value in zip(ordered_values, ordered_values[1:]):
        if low_value <= target <= high_value:
            return low_value, high_value, float(target)

    raise RuntimeError(f"Unable to bracket interpolation target {target} from {ordered_values!r}.")


def _sample_cruise_row(
    rows: tuple[CruisePerformanceRow, ...],
    *,
    flight_level: int,
) -> CruisePerformanceRow:
    """Sample a cruise table by flight level, interpolating between adjacent rows."""

    ordered_rows = tuple(sorted(rows, key=lambda row: row.flight_level))
    if not ordered_rows:
        raise ValueError(f"No cruise rows available to sample FL{flight_level}.")

    if flight_level <= ordered_rows[0].flight_level:
        return ordered_rows[0]
    if flight_level >= ordered_rows[-1].flight_level:
        return ordered_rows[-1]

    for low_row, high_row in zip(ordered_rows, ordered_rows[1:]):
        if low_row.flight_level <= flight_level <= high_row.flight_level:
            return CruisePerformanceRow(
                flight_level=flight_level,
                tas_kts=_interpolate_scalar(
                    low_row.tas_kts,
                    high_row.tas_kts,
                    x_low=low_row.flight_level,
                    x_high=high_row.flight_level,
                    x_target=flight_level,
                ),
                fuel_gph=_interpolate_scalar(
                    low_row.fuel_gph,
                    high_row.fuel_gph,
                    x_low=low_row.flight_level,
                    x_high=high_row.flight_level,
                    x_target=flight_level,
                ),
                ias_kts=int(
                    round(
                        _interpolate_scalar(
                            float(low_row.ias_kts or 0),
                            float(high_row.ias_kts or 0),
                            x_low=low_row.flight_level,
                            x_high=high_row.flight_level,
                            x_target=flight_level,
                        )
                    )
                ),
                torque_pct=int(
                    round(
                        _interpolate_scalar(
                            float(low_row.torque_pct or 0),
                            float(high_row.torque_pct or 0),
                            x_low=low_row.flight_level,
                            x_high=high_row.flight_level,
                            x_target=flight_level,
                        )
                    )
                ),
                oat_c=int(
                    round(
                        _interpolate_scalar(
                            float(low_row.oat_c or 0),
                            float(high_row.oat_c or 0),
                            x_low=low_row.flight_level,
                            x_high=high_row.flight_level,
                            x_target=flight_level,
                        )
                    )
                ),
                temperature_offset_c=low_row.temperature_offset_c,
                source=low_row.source,
                confidence=low_row.confidence,
                verified=low_row.verified,
                notes=low_row.notes,
            )

    return ordered_rows[-1]


def _interpolate_cruise_row_across_temperature(
    low_row: CruisePerformanceRow,
    high_row: CruisePerformanceRow,
    *,
    temp_low: float,
    temp_high: float,
    target_temp: float,
) -> CruisePerformanceRow:
    """Interpolate a cruise sample between two ISA-deviation table slices."""

    if temp_low == temp_high:
        return low_row
    return CruisePerformanceRow(
        flight_level=low_row.flight_level,
        tas_kts=_interpolate_scalar(
            low_row.tas_kts,
            high_row.tas_kts,
            x_low=temp_low,
            x_high=temp_high,
            x_target=target_temp,
        ),
        fuel_gph=_interpolate_scalar(
            low_row.fuel_gph,
            high_row.fuel_gph,
            x_low=temp_low,
            x_high=temp_high,
            x_target=target_temp,
        ),
        ias_kts=int(
            round(
                _interpolate_scalar(
                    float(low_row.ias_kts or 0),
                    float(high_row.ias_kts or 0),
                    x_low=temp_low,
                    x_high=temp_high,
                    x_target=target_temp,
                )
            )
        ),
        torque_pct=int(
            round(
                _interpolate_scalar(
                    float(low_row.torque_pct or 0),
                    float(high_row.torque_pct or 0),
                    x_low=temp_low,
                    x_high=temp_high,
                    x_target=target_temp,
                )
            )
        ),
        oat_c=int(
            round(
                _interpolate_scalar(
                    float(low_row.oat_c or 0),
                    float(high_row.oat_c or 0),
                    x_low=temp_low,
                    x_high=temp_high,
                    x_target=target_temp,
                )
            )
        ),
        temperature_offset_c=target_temp,
        source=low_row.source,
        confidence=low_row.confidence,
        verified=low_row.verified,
        notes=low_row.notes,
    )


def _interpolate_cruise_row_across_weight(
    low_row: CruisePerformanceRow,
    high_row: CruisePerformanceRow,
    *,
    weight_low: float,
    weight_high: float,
    target_weight: float,
) -> CruisePerformanceRow:
    """Interpolate one sampled cruise row between two published source weights."""

    if weight_low == weight_high:
        return low_row
    return CruisePerformanceRow(
        flight_level=low_row.flight_level,
        tas_kts=_interpolate_scalar(
            low_row.tas_kts,
            high_row.tas_kts,
            x_low=weight_low,
            x_high=weight_high,
            x_target=target_weight,
        ),
        fuel_gph=_interpolate_scalar(
            low_row.fuel_gph,
            high_row.fuel_gph,
            x_low=weight_low,
            x_high=weight_high,
            x_target=target_weight,
        ),
        ias_kts=int(
            round(
                _interpolate_scalar(
                    float(low_row.ias_kts or 0),
                    float(high_row.ias_kts or 0),
                    x_low=weight_low,
                    x_high=weight_high,
                    x_target=target_weight,
                )
            )
        ),
        torque_pct=int(
            round(
                _interpolate_scalar(
                    float(low_row.torque_pct or 0),
                    float(high_row.torque_pct or 0),
                    x_low=weight_low,
                    x_high=weight_high,
                    x_target=target_weight,
                )
            )
        ),
        oat_c=int(
            round(
                _interpolate_scalar(
                    float(low_row.oat_c or 0),
                    float(high_row.oat_c or 0),
                    x_low=weight_low,
                    x_high=weight_high,
                    x_target=target_weight,
                )
            )
        ),
        temperature_offset_c=low_row.temperature_offset_c,
        source=low_row.source,
        confidence=low_row.confidence,
        verified=low_row.verified,
        notes=low_row.notes,
    )


def _assert_matching_vertical_bands(
    rows_low: tuple[VerticalPerformanceRow, ...],
    rows_high: tuple[VerticalPerformanceRow, ...],
    *,
    context: str,
) -> None:
    """Fail closed when two PIM slices do not describe identical altitude bands."""

    low_bands = [(row.start_altitude_ft, row.end_altitude_ft) for row in rows_low]
    high_bands = [(row.start_altitude_ft, row.end_altitude_ft) for row in rows_high]
    if low_bands != high_bands:
        raise ValueError(
            f"Cannot perform {context}: Daher PIM altitude bands differ "
            f"({low_bands!r} versus {high_bands!r})."
        )


def _interpolate_vertical_rows(
    rows_low: tuple[VerticalPerformanceRow, ...],
    rows_high: tuple[VerticalPerformanceRow, ...],
    *,
    x_low: float,
    x_high: float,
    x_target: float,
    context: str,
    temperature_offset_c: float | None = None,
) -> tuple[VerticalPerformanceRow, ...]:
    """Interpolate matching vertical bands along temperature, weight, or rate axes."""

    if x_low == x_high:
        return rows_low
    _assert_matching_vertical_bands(rows_low, rows_high, context=context)

    def optional_value(low_value: float | None, high_value: float | None) -> float | None:
        if low_value is not None and high_value is not None:
            return _interpolate_scalar(
                float(low_value), float(high_value),
                x_low=x_low, x_high=x_high, x_target=x_target,
            )
        return low_value if low_value is not None else high_value

    interpolated_rows: list[VerticalPerformanceRow] = []
    for low_row, high_row in zip(rows_low, rows_high):
        interpolated_rows.append(
            VerticalPerformanceRow(
                start_altitude_ft=low_row.start_altitude_ft,
                end_altitude_ft=low_row.end_altitude_ft,
                ias_kts=int(round(_interpolate_scalar(
                    float(low_row.ias_kts), float(high_row.ias_kts),
                    x_low=x_low, x_high=x_high, x_target=x_target,
                ))),
                rate_fpm=max(int(round(_interpolate_scalar(
                    float(low_row.rate_fpm), float(high_row.rate_fpm),
                    x_low=x_low, x_high=x_high, x_target=x_target,
                ))), 100),
                fuel_gph=_interpolate_scalar(
                    low_row.fuel_gph, high_row.fuel_gph,
                    x_low=x_low, x_high=x_high, x_target=x_target,
                ),
                tas_kts=optional_value(low_row.tas_kts, high_row.tas_kts),
                distance_nm=optional_value(low_row.distance_nm, high_row.distance_nm),
                time_minutes=optional_value(low_row.time_minutes, high_row.time_minutes),
                temperature_offset_c=(
                    temperature_offset_c if temperature_offset_c is not None
                    else low_row.temperature_offset_c
                ),
                source=low_row.source,
                confidence=low_row.confidence,
                verified=low_row.verified,
                notes=low_row.notes,
            )
        )
    return tuple(interpolated_rows)


def _interpolate_vertical_rows_across_temperature(
    rows_low: tuple[VerticalPerformanceRow, ...],
    rows_high: tuple[VerticalPerformanceRow, ...],
    *, temp_low: float, temp_high: float, target_temp: float,
) -> tuple[VerticalPerformanceRow, ...]:
    """Interpolate climb or descent rows between ISA-deviation table slices."""

    return _interpolate_vertical_rows(
        rows_low, rows_high,
        x_low=temp_low, x_high=temp_high, x_target=target_temp,
        context="temperature interpolation", temperature_offset_c=target_temp,
    )


def _interpolate_vertical_rows_across_weight(
    rows_low: tuple[VerticalPerformanceRow, ...],
    rows_high: tuple[VerticalPerformanceRow, ...],
    *, weight_low: float, weight_high: float, target_weight: float,
) -> tuple[VerticalPerformanceRow, ...]:
    """Interpolate climb bands between adjacent published Daher weight columns."""

    return _interpolate_vertical_rows(
        rows_low, rows_high,
        x_low=weight_low, x_high=weight_high, x_target=target_weight,
        context="weight interpolation",
    )


def _interpolate_vertical_rows_across_rate(
    rows_low: tuple[VerticalPerformanceRow, ...],
    rows_high: tuple[VerticalPerformanceRow, ...],
    *, rate_low: float, rate_high: float, target_rate: float,
) -> tuple[VerticalPerformanceRow, ...]:
    """Interpolate descent rows between published descent-rate columns."""

    return _interpolate_vertical_rows(
        rows_low, rows_high,
        x_low=rate_low, x_high=rate_high, x_target=target_rate,
        context="descent-rate interpolation",
    )


def _segment_speed_kts(*, distance_nm: float, time_minutes: float, rate_fpm: float) -> float:
    """Estimate true path speed from horizontal distance, elapsed time, and vertical rate."""

    if time_minutes <= 0.0:
        return 60.0
    horizontal_speed_kts = distance_nm / (time_minutes / 60.0)
    vertical_speed_kts = abs(rate_fpm) * 60.0 / 6076.12
    return max(((horizontal_speed_kts ** 2) + (vertical_speed_kts ** 2)) ** 0.5, 60.0)


def _build_vertical_rows_from_cumulative_table(
    cumulative_rows: tuple[dict[str, float | int], ...],
    *,
    nominal_ias_kts: int,
    source: str,
    notes: str,
    temperature_offset_c: float | None = None,
) -> tuple[VerticalPerformanceRow, ...]:
    """Convert cumulative PIM climb/descent rows into per-altitude-band rows."""

    if not cumulative_rows:
        return ()

    ordered_rows = sorted(cumulative_rows, key=lambda row: int(row["pressure_altitude_ft"]))
    band_rows: list[VerticalPerformanceRow] = []

    for low_row, high_row in zip(ordered_rows, ordered_rows[1:]):
        start_altitude_ft = int(low_row["pressure_altitude_ft"])
        end_altitude_ft = int(high_row["pressure_altitude_ft"])
        altitude_delta_ft = end_altitude_ft - start_altitude_ft
        if altitude_delta_ft <= 0:
            raise ValueError(
                f"Cumulative vertical table has a non-increasing altitude band: "
                f"{start_altitude_ft} to {end_altitude_ft} ft."
            )

        time_minutes = float(high_row["time_minutes"]) - float(low_row["time_minutes"])
        fuel_used_gal = float(high_row["fuel_used_gal"]) - float(low_row["fuel_used_gal"])
        distance_nm = float(high_row["distance_nm"]) - float(low_row["distance_nm"])
        if time_minutes <= 0.0:
            raise ValueError(
                f"Cumulative vertical table has non-increasing time in the "
                f"{start_altitude_ft}-{end_altitude_ft} ft band."
            )
        if fuel_used_gal < 0.0 or distance_nm < 0.0:
            raise ValueError(
                f"Cumulative vertical table decreases fuel or distance in the "
                f"{start_altitude_ft}-{end_altitude_ft} ft band."
            )

        rate_fpm = max(int(round(altitude_delta_ft / time_minutes)), MIN_PLANNING_RATE_FPM)
        fuel_gph = fuel_used_gal / (time_minutes / 60.0)
        tas_kts = _segment_speed_kts(
            distance_nm=distance_nm,
            time_minutes=time_minutes,
            rate_fpm=rate_fpm,
        )
        band_rows.append(
            VerticalPerformanceRow(
                start_altitude_ft=start_altitude_ft,
                end_altitude_ft=end_altitude_ft,
                ias_kts=nominal_ias_kts,
                rate_fpm=rate_fpm,
                fuel_gph=fuel_gph,
                tas_kts=tas_kts,
                distance_nm=distance_nm,
                time_minutes=time_minutes,
                temperature_offset_c=temperature_offset_c,
                source=source,
                confidence="high",
                verified=True,
                notes=notes,
            )
        )

    return tuple(band_rows)


def _build_cruise_mode_profile(mode_id: str) -> CruiseModeProfile:
    """Load the full published cruise family for one mode into app-friendly rows."""

    mode_metadata = CRUISE_MODE_METADATA[mode_id]
    temperature_offsets = tuple(list_available_cruise_temperature_offsets(mode_id))
    rows_by_temp_offset_c: dict[int, tuple[CruisePerformanceRow, ...]] = {}

    for temperature_offset_c in temperature_offsets:
        table_reference = mode_metadata["table_references_by_temp_delta_c"][temperature_offset_c]
        mode_rows = []
        for row in cruise_rows_for_weight(mode_id, temperature_offset_c, DEFAULT_CRUISE_WEIGHT_LB):
            flight_level = int(row["pressure_altitude_ft"]) // 100
            mode_rows.append(
                CruisePerformanceRow(
                    flight_level=flight_level,
                    tas_kts=float(row["tas_kts"]),
                    fuel_gph=float(row["fuel_flow_gph"]),
                    ias_kts=int(row["ias_kts"]),
                    torque_pct=int(row["torque_pct"]),
                    oat_c=int(row["oat_c"]),
                    temperature_offset_c=float(temperature_offset_c),
                    source=SOURCE_METADATA["source"],
                    confidence="high",
                    verified=True,
                    notes=f"{table_reference} | {DEFAULT_CRUISE_WEIGHT_LB:,} lb",
                )
            )
        rows_by_temp_offset_c[temperature_offset_c] = tuple(mode_rows)

    baseline_table_reference = mode_metadata["table_references_by_temp_delta_c"][0]
    return CruiseModeProfile(
        mode_id=mode_id,
        label=mode_metadata["label"],
        description=f"Official {mode_metadata['label']} cruise table family from the TBM 960 PIM.",
        cruise_rows=rows_by_temp_offset_c[0],
        cruise_rows_by_temp_offset_c=rows_by_temp_offset_c,
        default_weight_lb=DEFAULT_CRUISE_WEIGHT_LB,
        available_weights_lb=CRUISE_WEIGHTS_LB,
        available_temperature_offsets_c=temperature_offsets,
        table_reference=baseline_table_reference,
        table_summary=f"ISA | {DEFAULT_CRUISE_WEIGHT_LB:,} lb | {mode_id.upper()}",
        table_references_by_temp_offset_c=dict(mode_metadata["table_references_by_temp_delta_c"]),
        condition_summary=mode_metadata["condition_summary"],
    )


def _cruise_rows_for_source_weight(
    *,
    mode_id: str,
    temperature_offset_c: int,
    weight_lb: int,
) -> tuple[CruisePerformanceRow, ...]:
    """Project one published cruise table weight into the normal row model."""

    table_reference = CRUISE_MODE_METADATA[mode_id]["table_references_by_temp_delta_c"][temperature_offset_c]
    rows: list[CruisePerformanceRow] = []
    for row in cruise_rows_for_weight(mode_id, temperature_offset_c, weight_lb):
        flight_level = int(row["pressure_altitude_ft"]) // 100
        rows.append(
            CruisePerformanceRow(
                flight_level=flight_level,
                tas_kts=float(row["tas_kts"]),
                fuel_gph=float(row["fuel_flow_gph"]),
                ias_kts=int(row["ias_kts"]),
                torque_pct=int(row["torque_pct"]),
                oat_c=int(row["oat_c"]),
                temperature_offset_c=float(temperature_offset_c),
                source=SOURCE_METADATA["source"],
                confidence="high",
                verified=True,
                notes=f"{table_reference} | {weight_lb:,} lb",
            )
        )
    return tuple(rows)


def _build_climb_schedule_profile(schedule_id: str) -> ClimbScheduleProfile:
    """Convert one published climb schedule family into altitude-band rows."""

    schedule_metadata = CLIMB_SCHEDULE_METADATA[schedule_id]
    temperature_offsets = tuple(list_available_climb_temperature_offsets(schedule_id))
    rows_by_temp_offset_c: dict[int, tuple[VerticalPerformanceRow, ...]] = {}

    for temperature_offset_c in temperature_offsets:
        table_reference = schedule_metadata["table_references_by_temp_delta_c"][temperature_offset_c]
        cumulative_rows = climb_rows_for_weight(schedule_id, temperature_offset_c, DEFAULT_CLIMB_WEIGHT_LB)
        rows_by_temp_offset_c[temperature_offset_c] = _build_vertical_rows_from_cumulative_table(
            cumulative_rows,
            nominal_ias_kts=int(schedule_metadata["nominal_ias_kts"]),
            source=SOURCE_METADATA["source"],
            notes=f"{table_reference} | {DEFAULT_CLIMB_WEIGHT_LB:,} lb",
            temperature_offset_c=float(temperature_offset_c),
        )

    baseline_table_reference = schedule_metadata["table_references_by_temp_delta_c"][0]
    return ClimbScheduleProfile(
        schedule_id=schedule_id,
        label=schedule_metadata["label"],
        description=f"Official climb schedule {schedule_metadata['label']} from the TBM 960 PIM.",
        climb_rows=rows_by_temp_offset_c[0],
        climb_rows_by_temp_offset_c=rows_by_temp_offset_c,
        nominal_ias_kts=int(schedule_metadata["nominal_ias_kts"]),
        default_weight_lb=DEFAULT_CLIMB_WEIGHT_LB,
        available_weights_lb=CLIMB_WEIGHTS_LB,
        available_temperature_offsets_c=temperature_offsets,
        table_reference=baseline_table_reference,
        table_summary=f"ISA | {DEFAULT_CLIMB_WEIGHT_LB:,} lb | {schedule_metadata['label']}",
        table_references_by_temp_offset_c=dict(schedule_metadata["table_references_by_temp_delta_c"]),
    )


def _climb_rows_for_source_weight(
    *,
    schedule_id: str,
    temperature_offset_c: int,
    weight_lb: int,
) -> tuple[VerticalPerformanceRow, ...]:
    """Project one published climb table weight into climb bands."""

    schedule_metadata = CLIMB_SCHEDULE_METADATA[schedule_id]
    table_reference = schedule_metadata["table_references_by_temp_delta_c"][temperature_offset_c]
    return _build_vertical_rows_from_cumulative_table(
        climb_rows_for_weight(schedule_id, temperature_offset_c, weight_lb),
        nominal_ias_kts=int(schedule_metadata["nominal_ias_kts"]),
        source=SOURCE_METADATA["source"],
        notes=f"{table_reference} | {weight_lb:,} lb",
        temperature_offset_c=float(temperature_offset_c),
    )


def _build_descent_profile(profile_id: str) -> DescentProfile:
    """Convert the cumulative descent table into selectable vertical-rate bands."""

    profile_metadata = DESCENT_PROFILE_METADATA[profile_id]
    source_label = str(profile_metadata.get("source_label") or profile_metadata["label"])
    rows_by_rate_fpm: dict[int, tuple[VerticalPerformanceRow, ...]] = {}

    for vertical_rate_fpm in DESCENT_RATES_FPM:
        cumulative_rows = descent_rows_for_rate(profile_id, vertical_rate_fpm)
        rows_by_rate_fpm[vertical_rate_fpm] = _build_vertical_rows_from_cumulative_table(
            cumulative_rows,
            nominal_ias_kts=int(profile_metadata["nominal_ias_kts"]),
            source=SOURCE_METADATA["source"],
            notes=f"{profile_metadata['table_reference']} | {vertical_rate_fpm:,} fpm",
        )

    return DescentProfile(
        profile_id=profile_id,
        label=profile_metadata["label"],
        description=(
            f"Preferred descent schedule {profile_metadata['label']} backed by {source_label} cumulative rows "
            "from the TBM 960 PIM."
        ),
        descent_rows=rows_by_rate_fpm[DEFAULT_DESCENT_RATE_FPM],
        descent_rows_by_rate_fpm=rows_by_rate_fpm,
        nominal_ias_kts=int(profile_metadata["nominal_ias_kts"]),
        available_vertical_rates_fpm=tuple(profile_metadata["available_vertical_rates_fpm"]),
        table_reference=profile_metadata["table_reference"],
        table_summary=f"{profile_metadata['label']} | {DEFAULT_DESCENT_RATE_FPM:,} fpm | {source_label}",
    )


def _build_official_pim_profile() -> AircraftPerformanceProfile:
    """Assemble the immutable built-in TBM 960 profile from the official PIM source tables."""

    cruise_modes = tuple(_build_cruise_mode_profile(mode_id) for mode_id in CRUISE_MODE_METADATA)
    climb_schedules = tuple(
        _build_climb_schedule_profile(schedule_id) for schedule_id in CLIMB_SCHEDULE_METADATA
    )
    descent_profiles = tuple(_build_descent_profile(profile_id) for profile_id in DESCENT_PROFILE_METADATA)

    default_climb_schedule = next(
        schedule for schedule in climb_schedules if schedule.schedule_id == DEFAULT_CLIMB_SCHEDULE_ID
    )
    default_descent_profile = next(
        profile for profile in descent_profiles if profile.profile_id == DEFAULT_DESCENT_PROFILE_ID
    )

    return AircraftPerformanceProfile(
        profile_id=DEFAULT_PERFORMANCE_PROFILE_ID,
        label="TBM 960 Official PIM",
        summary=(
            "Official Daher TBM 960 PIM baseline using the published cruise, climb, and descent "
            "table families, including ISA-deviation slices and alternate climb/descent schedules."
        ),
        aircraft="TBM 960",
        default_cruise_mode_id=DEFAULT_CRUISE_MODE_ID,
        default_climb_schedule_id=DEFAULT_CLIMB_SCHEDULE_ID,
        default_descent_profile_id=DEFAULT_DESCENT_PROFILE_ID,
        default_descent_rate_fpm=DEFAULT_DESCENT_RATE_FPM,
        cruise_modes=cruise_modes,
        climb_schedules=climb_schedules,
        descent_profiles=descent_profiles,
        climb_rows=default_climb_schedule.climb_rows,
        descent_rows=default_descent_profile.descent_rows,
        # The PIM weight-and-balance example uses taxi fuel -50 lb; at typical Jet-A density this
        # rounds to 8 gallons for startup/taxi/run-up planning before user calibration exists.
        fixed_fuel_gal=DEFAULT_STARTUP_TAXI_FUEL_GAL,
        cruise_weight_lb=DEFAULT_CRUISE_WEIGHT_LB,
        climb_weight_lb=DEFAULT_CLIMB_WEIGHT_LB,
        climb_table_reference=default_climb_schedule.table_reference,
        climb_table_summary=default_climb_schedule.table_summary,
        descent_table_reference=default_descent_profile.table_reference,
        descent_table_summary=default_descent_profile.table_summary,
        source=SOURCE_METADATA["source"],
        confidence="high",
        verified=True,
        notes=(
            "Cruise keeps all published MXCR, RCR, and LRCR temperature slices for 5,500 / 6,300 / "
            "7,100 / 7,300 lb. Climb keeps both published schedules and all published ISA offset tables "
            "for 5,794 / 6,579 / 7,394 / 7,615 lb. Descent keeps all published 230 KCAS columns for "
            "1,500 / 2,000 / 2,500 fpm. Current mission calculations use the official 7,100 lb cruise "
            "and 7,394 lb climb source-weight baselines until a gross-weight control is added."
        ),
    )


OFFICIAL_PIM_PROFILE = _build_official_pim_profile()
PERFORMANCE_PROFILES = (OFFICIAL_PIM_PROFILE,)


def get_performance_profile(profile_id: str | None) -> AircraftPerformanceProfile:
    """Return a known profile, falling back to the official baseline when needed."""

    normalized_id = (profile_id or DEFAULT_PERFORMANCE_PROFILE_ID).strip().lower()
    for profile in PERFORMANCE_PROFILES:
        if profile.profile_id == normalized_id:
            return profile
    return OFFICIAL_PIM_PROFILE


def resolve_cruise_mode(
    profile: AircraftPerformanceProfile,
    cruise_mode_id: str | None,
) -> CruiseModeProfile:
    """Resolve a cruise mode ID with fallback to the profile default."""

    normalized_id = (cruise_mode_id or profile.default_cruise_mode_id).strip().lower()
    for mode in profile.cruise_modes:
        if mode.mode_id == normalized_id:
            return mode
    return next(
        (mode for mode in profile.cruise_modes if mode.mode_id == profile.default_cruise_mode_id),
        profile.cruise_modes[0],
    )


def resolve_climb_schedule(
    profile: AircraftPerformanceProfile,
    climb_schedule_id: str | None,
) -> ClimbScheduleProfile:
    """Resolve a climb schedule ID with fallback to the profile default."""

    normalized_id = (climb_schedule_id or profile.default_climb_schedule_id).strip().lower()
    for schedule in profile.climb_schedules:
        if schedule.schedule_id == normalized_id:
            return schedule
    return next(
        (
            schedule
            for schedule in profile.climb_schedules
            if schedule.schedule_id == profile.default_climb_schedule_id
        ),
        profile.climb_schedules[0],
    )


def resolve_descent_profile(
    profile: AircraftPerformanceProfile,
    descent_profile_id: str | None,
) -> DescentProfile:
    """Resolve a descent profile ID with fallback to the profile default."""

    normalized_id = (descent_profile_id or profile.default_descent_profile_id).strip().lower()
    for descent_profile in profile.descent_profiles:
        if descent_profile.profile_id == normalized_id:
            return descent_profile
    return next(
        (
            descent_profile
            for descent_profile in profile.descent_profiles
            if descent_profile.profile_id == profile.default_descent_profile_id
        ),
        profile.descent_profiles[0],
    )


def sample_cruise_performance(
    profile: AircraftPerformanceProfile,
    *,
    flight_level: int,
    cruise_mode_id: str | None = None,
    temperature_offset_c: float | None = None,
    weight_lb: float | None = None,
) -> CruisePerformanceSample:
    """Sample cruise performance across altitude, ISA-deviation, and weight slices."""

    mode = resolve_cruise_mode(profile, cruise_mode_id)
    requested_temp = float(temperature_offset_c if temperature_offset_c is not None else 0.0)
    temp_low, temp_high, bounded_temp = _nearest_bounds(mode.available_temperature_offsets_c, requested_temp)
    requested_weight = float(weight_lb if weight_lb is not None else mode.default_weight_lb)
    weight_low, weight_high, bounded_weight = _nearest_bounds(mode.available_weights_lb, requested_weight)

    def sample_at(temp_offset_c: int) -> CruisePerformanceRow:
        low_weight_rows = (
            mode.cruise_rows_by_temp_offset_c[temp_offset_c]
            if weight_low == mode.default_weight_lb
            else _cruise_rows_for_source_weight(
                mode_id=mode.mode_id,
                temperature_offset_c=temp_offset_c,
                weight_lb=weight_low,
            )
        )
        high_weight_rows = (
            mode.cruise_rows_by_temp_offset_c[temp_offset_c]
            if weight_high == mode.default_weight_lb
            else _cruise_rows_for_source_weight(
                mode_id=mode.mode_id,
                temperature_offset_c=temp_offset_c,
                weight_lb=weight_high,
            )
        )
        low_weight_sample = _sample_cruise_row(low_weight_rows, flight_level=flight_level)
        high_weight_sample = _sample_cruise_row(high_weight_rows, flight_level=flight_level)
        return _interpolate_cruise_row_across_weight(
            low_weight_sample,
            high_weight_sample,
            weight_low=float(weight_low),
            weight_high=float(weight_high),
            target_weight=bounded_weight,
        )

    low_sample = sample_at(temp_low)
    high_sample = sample_at(temp_high)
    interpolated_row = _interpolate_cruise_row_across_temperature(
        low_sample,
        high_sample,
        temp_low=float(temp_low),
        temp_high=float(temp_high),
        target_temp=bounded_temp,
    )
    if temp_low == temp_high:
        if mode.table_references_by_temp_offset_c:
            table_reference = mode.table_references_by_temp_offset_c.get(temp_low, mode.table_reference)
        else:
            table_reference = mode.table_reference
    else:
        if mode.table_references_by_temp_offset_c:
            low_reference = mode.table_references_by_temp_offset_c.get(temp_low, "")
            high_reference = mode.table_references_by_temp_offset_c.get(temp_high, "")
            table_reference = f"Interpolated {low_reference} to {high_reference}".strip()
        else:
            table_reference = mode.table_reference
    return CruisePerformanceSample(
        flight_level=flight_level,
        tas_kts=interpolated_row.tas_kts,
        fuel_gph=interpolated_row.fuel_gph,
        mode_id=mode.mode_id,
        mode_label=mode.label,
        ias_kts=interpolated_row.ias_kts,
        torque_pct=interpolated_row.torque_pct,
        oat_c=interpolated_row.oat_c,
        temperature_offset_c=bounded_temp,
        table_reference=table_reference,
    )


def sample_climb_rows(
    profile: AircraftPerformanceProfile,
    *,
    climb_schedule_id: str | None = None,
    temperature_offset_c: float | None = None,
    weight_lb: float | None = None,
) -> tuple[VerticalPerformanceRow, ...]:
    """Sample climb bands for the selected schedule, ISA deviation, and source weight."""

    schedule = resolve_climb_schedule(profile, climb_schedule_id)
    requested_temp = float(temperature_offset_c if temperature_offset_c is not None else 0.0)
    temp_low, temp_high, bounded_temp = _nearest_bounds(
        schedule.available_temperature_offsets_c,
        requested_temp,
    )
    requested_weight = float(weight_lb if weight_lb is not None else schedule.default_weight_lb)
    weight_low, weight_high, bounded_weight = _nearest_bounds(
        schedule.available_weights_lb,
        requested_weight,
    )

    def rows_at(temp_offset_c: int) -> tuple[VerticalPerformanceRow, ...]:
        low_weight_rows = (
            schedule.climb_rows_by_temp_offset_c[temp_offset_c]
            if weight_low == schedule.default_weight_lb
            else _climb_rows_for_source_weight(
                schedule_id=schedule.schedule_id,
                temperature_offset_c=temp_offset_c,
                weight_lb=weight_low,
            )
        )
        high_weight_rows = (
            schedule.climb_rows_by_temp_offset_c[temp_offset_c]
            if weight_high == schedule.default_weight_lb
            else _climb_rows_for_source_weight(
                schedule_id=schedule.schedule_id,
                temperature_offset_c=temp_offset_c,
                weight_lb=weight_high,
            )
        )
        return _interpolate_vertical_rows_across_weight(
            low_weight_rows,
            high_weight_rows,
            weight_low=float(weight_low),
            weight_high=float(weight_high),
            target_weight=bounded_weight,
        )

    return _interpolate_vertical_rows_across_temperature(
        rows_at(temp_low),
        rows_at(temp_high),
        temp_low=float(temp_low),
        temp_high=float(temp_high),
        target_temp=bounded_temp,
    )


def sample_composite_climb_rows(
    profile: AircraftPerformanceProfile,
    *,
    lower_schedule_id: str,
    upper_schedule_id: str,
    transition_altitude_ft: float,
    temperature_offset_c: float | None = None,
    weight_lb: float | None = None,
) -> tuple[VerticalPerformanceRow, ...]:
    """Splice two complete PIM climb schedules at a pilot-selected MSL altitude."""

    lower_rows = sample_climb_rows(
        profile,
        climb_schedule_id=lower_schedule_id,
        temperature_offset_c=temperature_offset_c,
        weight_lb=weight_lb,
    )
    upper_rows = sample_climb_rows(
        profile,
        climb_schedule_id=upper_schedule_id,
        temperature_offset_c=temperature_offset_c,
        weight_lb=weight_lb,
    )
    transition_ft = float(transition_altitude_ft)
    composite: list[VerticalPerformanceRow] = []

    # A clipped band's cumulative time/distance still describe the full parent band,
    # so they are nulled rather than left to double-count in any future consumer.
    for row in lower_rows:
        if row.start_altitude_ft >= transition_ft:
            continue
        clipped_end_ft = int(min(row.end_altitude_ft, transition_ft))
        composite.append(
            row
            if clipped_end_ft == row.end_altitude_ft
            else replace(row, end_altitude_ft=clipped_end_ft, time_minutes=None, distance_nm=None)
        )
    for row in upper_rows:
        if row.end_altitude_ft <= transition_ft:
            continue
        clipped_start_ft = int(max(row.start_altitude_ft, transition_ft))
        composite.append(
            row
            if clipped_start_ft == row.start_altitude_ft
            else replace(row, start_altitude_ft=clipped_start_ft, time_minutes=None, distance_nm=None)
        )

    usable_rows = [row for row in composite if row.end_altitude_ft > row.start_altitude_ft]
    return tuple(sorted(usable_rows, key=lambda row: (row.start_altitude_ft, row.end_altitude_ft)))


def sample_descent_rows(
    profile: AircraftPerformanceProfile,
    *,
    descent_profile_id: str | None = None,
    vertical_rate_fpm: int | None = None,
) -> tuple[VerticalPerformanceRow, ...]:
    """Sample descent bands for the selected schedule and target vertical rate."""

    descent_profile = resolve_descent_profile(profile, descent_profile_id)
    requested_rate = float(
        vertical_rate_fpm if vertical_rate_fpm is not None else profile.default_descent_rate_fpm
    )
    rate_low, rate_high, bounded_rate = _nearest_bounds(
        descent_profile.available_vertical_rates_fpm,
        requested_rate,
    )
    return _interpolate_vertical_rows_across_rate(
        descent_profile.descent_rows_by_rate_fpm[rate_low],
        descent_profile.descent_rows_by_rate_fpm[rate_high],
        rate_low=float(rate_low),
        rate_high=float(rate_high),
        target_rate=bounded_rate,
    )


