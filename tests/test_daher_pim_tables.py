"""Regression coverage for parsing the vendored Daher performance tables."""

import copy

import daher_pim_tables
import pytest

from daher_pim_tables import (
    CLIMB_SCHEDULE_METADATA,
    CLIMB_WEIGHTS_LB,
    CRUISE_MODE_METADATA,
    CRUISE_WEIGHTS_LB,
    DESCENT_RATES_FPM,
    PIM_PDF_PATH,
    climb_rows_for_weight,
    cruise_rows_for_weight,
    descent_rows_for_rate,
    parse_climb_table,
    parse_cruise_table,
    parse_descent_table,
)


def test_every_mapped_pim_page_has_the_complete_altitude_sequence():
    """Verify all mapped PDF pages pass the parser's fail-closed row validation."""

    for mode_id, metadata in CRUISE_MODE_METADATA.items():
        for temperature_offset_c in metadata["table_references_by_temp_delta_c"]:
            assert len(parse_cruise_table(mode_id, temperature_offset_c)) == 17
    for schedule_id, metadata in CLIMB_SCHEDULE_METADATA.items():
        for temperature_offset_c in metadata["table_references_by_temp_delta_c"]:
            assert len(parse_climb_table(schedule_id, temperature_offset_c)) == 17
    assert len(parse_descent_table("230_kcas")) == 17


def test_vendored_daher_pdf_is_present():
    """Verify that vendored daher pdf is present."""

    assert PIM_PDF_PATH.exists()


def test_matching_snapshot_loads_without_opening_the_pdf(monkeypatch):
    """Verify normal cold starts use the checked-in hash-matched snapshot."""

    daher_pim_tables._pim_snapshot.cache_clear()
    daher_pim_tables.parse_cruise_table.cache_clear()

    def fail_if_pdf_is_opened():
        raise AssertionError("The PDF should not be parsed when its validated snapshot matches.")

    monkeypatch.setattr(daher_pim_tables, "_pdf_reader", fail_if_pdf_is_opened)
    assert len(daher_pim_tables.parse_cruise_table("max", 0)) == 17


def test_corrupt_snapshot_rows_fail_closed(monkeypatch):
    """Verify snapshot-loaded rows receive the same altitude validation as PDF parses."""

    payload = copy.deepcopy(daher_pim_tables._pim_snapshot())
    payload["cruise"]["max|0"].pop()
    monkeypatch.setattr(daher_pim_tables, "_pim_snapshot", lambda: payload)
    daher_pim_tables.parse_cruise_table.cache_clear()

    with pytest.raises(ValueError, match="Incomplete Daher PIM parse"):
        daher_pim_tables.parse_cruise_table("max", 0)


def test_cruise_parser_preserves_all_published_weights():
    """Verify that cruise parser preserves all published weights."""

    rows = parse_cruise_table("max", 0)

    assert len(rows) == 17
    assert tuple(sorted(rows[0].weights_lb)) == CRUISE_WEIGHTS_LB
    assert cruise_rows_for_weight("max", 0, 5500)[-1]["tas_kts"] == 329
    assert cruise_rows_for_weight("max", 0, 7100)[-1]["tas_kts"] == 323


def test_climb_parser_preserves_all_published_weights():
    """Verify that climb parser preserves all published weights."""

    rows = parse_climb_table("124_kias", 0)

    assert len(rows) == 17
    assert tuple(sorted(rows[0].weights_lb)) == CLIMB_WEIGHTS_LB
    assert climb_rows_for_weight("124_kias", 0, 5794)[-1]["time_minutes"] == 12.75
    assert climb_rows_for_weight("124_kias", 0, 7615)[-1]["distance_nm"] == 52.0


def test_descent_parser_preserves_all_published_rate_columns():
    """Verify that descent parser preserves all published rate columns."""

    rows = parse_descent_table("230_kcas")

    assert len(rows) == 17
    assert tuple(sorted(rows[0].profiles_by_rate_fpm)) == DESCENT_RATES_FPM
    assert descent_rows_for_rate("230_kcas", 1500)[0]["pressure_altitude_ft"] == 31000
    assert descent_rows_for_rate("230_kcas", 2500)[0]["distance_nm"] == 60.0
