"""Regression coverage for the built-in performance profile and interpolation helpers."""

import pytest

from performance_profiles import (
    DEFAULT_CRUISE_MODE_ID,
    DEFAULT_DESCENT_RATE_FPM,
    DEFAULT_PERFORMANCE_PROFILE_ID,
    DEFAULT_STARTUP_TAXI_FUEL_GAL,
    get_performance_profile,
    list_cruise_rows_for_display,
    list_vertical_rows_for_display,
    resolve_climb_schedule,
    resolve_cruise_mode,
    resolve_descent_profile,
    sample_climb_rows,
    sample_composite_climb_rows,
    sample_cruise_performance,
    sample_descent_rows,
)


def test_composite_climb_splices_pim_schedules_at_selected_altitude():
    """Verify that the pilot's transition altitude selects the correct PIM schedule on each side."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)
    rows = sample_composite_climb_rows(
        profile,
        lower_schedule_id="124_kias",
        upper_schedule_id="170_kias_m0_40",
        transition_altitude_ft=10000,
    )

    assert rows
    assert all(row.ias_kts == 124 for row in rows if row.end_altitude_ft <= 10000)
    assert all(row.ias_kts == 170 for row in rows if row.start_altitude_ft >= 10000)
    assert any(row.end_altitude_ft == 10000 for row in rows)
    assert any(row.start_altitude_ft == 10000 for row in rows)


def test_official_pim_profile_exposes_source_table_families():
    """Verify that official pim profile exposes source table families."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)
    max_mode = resolve_cruise_mode(profile, "max")
    climb_schedule = resolve_climb_schedule(profile, "124_kias")
    descent_profile = resolve_descent_profile(profile, "230_kcas")

    assert DEFAULT_CRUISE_MODE_ID == "max"
    assert profile.default_cruise_mode_id == "max"
    assert profile.default_climb_schedule_id == "124_kias"
    assert profile.default_descent_profile_id == "230_kcas"
    assert profile.default_descent_rate_fpm == DEFAULT_DESCENT_RATE_FPM
    assert profile.fixed_fuel_gal == DEFAULT_STARTUP_TAXI_FUEL_GAL
    assert max_mode.table_reference == "Table 5.11.5"
    assert max_mode.available_temperature_offsets_c == (-20, -10, -5, 0, 5, 10, 20)
    assert max_mode.available_weights_lb == (5500, 6300, 7100, 7300)
    assert climb_schedule.table_reference == "Table 5.10.5"
    assert climb_schedule.available_temperature_offsets_c == (-20, 0, 20)
    assert climb_schedule.available_weights_lb == (5794, 6579, 7394, 7615)
    assert descent_profile.table_reference == "Table 5.12.1"
    assert descent_profile.label == "220 KIAS"
    assert descent_profile.nominal_ias_kts == 220
    assert descent_profile.available_vertical_rates_fpm == (1500, 2000, 2500)


# Display-oriented helpers must keep rows in the order pilots expect to scan them.
def test_cruise_rows_for_display_sort_highest_flight_level_first():
    """Verify that cruise rows for display sort highest flight level first."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    rows = list_cruise_rows_for_display(profile, cruise_mode_id="max")

    assert rows[0].flight_level == 310
    assert rows[-1].flight_level == 180


def test_vertical_rows_for_display_sort_highest_altitude_band_first():
    """Verify that vertical rows for display sort highest altitude band first."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    climb_rows = list_vertical_rows_for_display(profile.climb_rows)
    descent_rows = list_vertical_rows_for_display(profile.descent_rows)

    assert climb_rows[0].start_altitude_ft == 30000
    assert climb_rows[-1].start_altitude_ft == 0
    assert descent_rows[0].start_altitude_ft == 30000
    assert descent_rows[-1].start_altitude_ft == 0


def test_sample_cruise_performance_interpolates_between_defined_rows_and_temps():
    """Verify that sample cruise performance interpolates between defined rows and temps."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    sample = sample_cruise_performance(
        profile,
        flight_level=190,
        cruise_mode_id="normal",
        temperature_offset_c=5,
    )

    assert sample.mode_id == "normal"
    assert sample.mode_label == "Recommended Cruise"
    assert sample.temperature_offset_c == 5.0
    assert sample.table_reference == "Table 5.11.35"
    assert sample.tas_kts == pytest.approx(279.5)
    assert sample.fuel_gph == pytest.approx(61.15)


def test_sample_cruise_performance_retains_low_altitude_pim_rows():
    """Verify range calculations can sample actual low-altitude PIM performance."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)
    low = sample_cruise_performance(profile, flight_level=50, cruise_mode_id="max")
    fl190 = sample_cruise_performance(profile, flight_level=190, cruise_mode_id="max")

    assert low.flight_level == 50
    assert low.tas_kts == pytest.approx(253.0)
    assert low.fuel_gph == pytest.approx(81.4)
    assert low.tas_kts < fl190.tas_kts
    assert low.fuel_gph > fl190.fuel_gph


def test_sample_cruise_performance_interpolates_between_temp_slices():
    """Verify that sample cruise performance interpolates between temp slices."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    sample = sample_cruise_performance(
        profile,
        flight_level=300,
        cruise_mode_id="max",
        temperature_offset_c=7.5,
    )

    assert sample.temperature_offset_c == 7.5
    assert sample.tas_kts == pytest.approx(319.0)
    assert sample.fuel_gph == pytest.approx(57.75)
    assert sample.table_reference == "Interpolated Table 5.11.6 to Table 5.11.7"


def test_sample_climb_rows_supports_alternate_schedule_and_temp_slice():
    """Verify that sample climb rows supports alternate schedule and temp slice."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    rows = sample_climb_rows(
        profile,
        climb_schedule_id="170_kias_m0_40",
        temperature_offset_c=20,
    )

    assert rows[0].ias_kts == 170
    assert rows[0].temperature_offset_c == 20.0
    assert rows[0].notes.startswith("Table 5.10.9")


def test_sample_descent_rows_supports_exact_rate_selection():
    """Verify that sample descent rows supports exact rate selection."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)

    rows = sample_descent_rows(profile, vertical_rate_fpm=2000)

    assert rows[0].ias_kts == 220
    assert rows[0].rate_fpm == 2000
    assert rows[0].notes == "Table 5.12.1 | 2,000 fpm"


def test_empty_inputs_raise_instead_of_returning_sentinels():
    """Verify empty tables fail loudly rather than yielding zero performance."""

    import performance_profiles

    with pytest.raises(ValueError, match="No cruise rows"):
        performance_profiles._sample_cruise_row((), flight_level=300)
    with pytest.raises(ValueError, match="No table keys"):
        performance_profiles._nearest_bounds((), 1500.0)


def test_composite_climb_clipped_bands_null_stale_cumulative_fields():
    """Verify a clipped transition band cannot leak its parent band's time/distance."""

    profile = get_performance_profile(DEFAULT_PERFORMANCE_PROFILE_ID)
    rows = sample_composite_climb_rows(
        profile,
        lower_schedule_id="124_kias",
        upper_schedule_id="170_kias_m0_40",
        transition_altitude_ft=9000,
    )

    clipped = [
        row
        for row in rows
        if row.end_altitude_ft == 9000 or row.start_altitude_ft == 9000
    ]
    assert clipped
    assert all(row.time_minutes is None and row.distance_nm is None for row in clipped)
