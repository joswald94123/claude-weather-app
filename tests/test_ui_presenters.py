"""Regression coverage for compact mission-summary presentation helpers."""

import datetime as dt

from ui_presenters import (
    default_departure_time,
    is_departure_time_stale,
    landing_fuel_presentation,
)


def test_default_departure_time_keeps_fifteen_minute_startup_lead():
    """Prevent the initial ETD from expiring while the first brief loads."""

    now_local = dt.datetime(
        2026,
        7,
        20,
        11,
        3,
        42,
        tzinfo=dt.timezone(dt.timedelta(hours=-7)),
    )

    selected = default_departure_time(now_local)

    assert selected == dt.datetime(
        2026,
        7,
        20,
        11,
        20,
        tzinfo=now_local.tzinfo,
    )
    assert selected - now_local >= dt.timedelta(minutes=15)


def test_departure_time_staleness_allows_current_five_minute_window():
    """Avoid a false warning merely because a selected minute has begun."""

    timezone = dt.timezone(dt.timedelta(hours=-7))
    selected = dt.datetime(2026, 7, 20, 11, 5, tzinfo=timezone)

    assert not is_departure_time_stale(
        selected,
        dt.datetime(2026, 7, 20, 11, 5, 59, tzinfo=timezone),
    )
    assert is_departure_time_stale(
        selected,
        dt.datetime(2026, 7, 20, 11, 10, 1, tzinfo=timezone),
    )


def test_landing_fuel_card_stays_compact_and_breakdown_remains_auditable():
    """Keep the six-card row aligned without dropping protected-fuel inputs."""

    presentation = landing_fuel_presentation(
        fuel_on_board_gal=29,
        fuel_status="Below reserve",
        effective_requirement_gal=60,
        alternate_and_reserve_gal=40,
        landing_minimum_gal=60,
        pilot_floor_gal=0,
        reserve_margin_gal=-31,
    )

    assert presentation.card_detail == "Gross touchdown FOB | 31 gal below 60 gal required"
    assert "alt + reserve 40" not in presentation.card_detail
    assert "gross FOB at touchdown is 29 gal before alternate or reserve use" in presentation.breakdown
    assert "max(alt + reserve 40, landing minimum 60, pilot floor 0)" in presentation.breakdown


def test_landing_fuel_card_reports_positive_and_exact_margins():
    """Use unambiguous comparison text for surplus and exact-requirement cases."""

    above = landing_fuel_presentation(
        fuel_on_board_gal=75,
        fuel_status="Meets reserve",
        effective_requirement_gal=60,
        alternate_and_reserve_gal=60,
        landing_minimum_gal=40,
        pilot_floor_gal=0,
        reserve_margin_gal=15,
    )
    exact = landing_fuel_presentation(
        fuel_on_board_gal=60,
        fuel_status="Meets landing minimum",
        effective_requirement_gal=60,
        alternate_and_reserve_gal=40,
        landing_minimum_gal=60,
        pilot_floor_gal=0,
        reserve_margin_gal=0,
    )

    assert above.card_detail.endswith("15 gal above 60 gal required")
    assert exact.card_detail.endswith("Meets 60 gal required")


def test_landing_fuel_breakdown_includes_ledger_phase_composition():
    """Verify the audit caption shows the taxi/climb/cruise/descent composition."""

    summary = landing_fuel_presentation(
        fuel_on_board_gal=29,
        fuel_status="Below reserve",
        effective_requirement_gal=60,
        alternate_and_reserve_gal=40,
        landing_minimum_gal=60,
        pilot_floor_gal=0,
        reserve_margin_gal=-31,
        taxi_fuel_gal=8.0,
        climb_fuel_gal=22.2,
        cruise_fuel_gal=210.5,
        descent_fuel_gal=12.4,
    )

    assert "Burn composition: taxi 8.0 + climb 22.2 + cruise 210.5 + descent 12.4 gal" in summary.breakdown
