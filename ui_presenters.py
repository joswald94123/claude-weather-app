"""Pure presentation helpers shared by the Streamlit mission-planning UI."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(frozen=True)
class LandingFuelPresentation:
    """Concise card copy plus the complete auditable landing-fuel breakdown."""

    card_detail: str
    breakdown: str


def next_five_minute_mark(value: dt.datetime) -> dt.datetime:
    """Round a timestamp forward to the next selectable five-minute mark."""

    base = value.replace(second=0, microsecond=0)
    step = (5 - (base.minute % 5)) % 5
    if step == 0 and (value.second > 0 or value.microsecond > 0):
        step = 5
    return base + dt.timedelta(minutes=step)


def default_departure_time(now_local: dt.datetime, *, lead_minutes: int = 15) -> dt.datetime:
    """Return a rounded default ETD with enough lead time to survive app startup."""

    return next_five_minute_mark(
        now_local + dt.timedelta(minutes=max(int(lead_minutes), 0))
    )


def is_departure_time_stale(
    selected_etd: dt.datetime,
    now_local: dt.datetime,
    *,
    grace_minutes: int = 5,
) -> bool:
    """Flag meaningfully past ETDs without warning during the selected minute."""

    if selected_etd.tzinfo is None or now_local.tzinfo is None:
        raise ValueError("ETD staleness checks require timezone-aware timestamps")
    grace = dt.timedelta(minutes=max(int(grace_minutes), 0))
    return selected_etd < now_local - grace


def landing_fuel_presentation(
    *,
    fuel_on_board_gal: int,
    fuel_status: str,
    effective_requirement_gal: int,
    alternate_and_reserve_gal: int,
    landing_minimum_gal: int,
    pilot_floor_gal: int,
) -> LandingFuelPresentation:
    """Keep the summary card compact while retaining every fuel-requirement input."""

    margin_gal = fuel_on_board_gal - effective_requirement_gal
    if margin_gal > 0:
        comparison = f"{margin_gal} gal above {effective_requirement_gal} gal required"
    elif margin_gal < 0:
        comparison = f"{abs(margin_gal)} gal below {effective_requirement_gal} gal required"
    else:
        comparison = f"Meets {effective_requirement_gal} gal required"

    return LandingFuelPresentation(
        card_detail=f"Gross touchdown FOB | {comparison}",
        breakdown=(
            f"Fuel basis — {fuel_status}: gross FOB at touchdown is {fuel_on_board_gal} gal before "
            f"alternate or reserve use. Effective requirement {effective_requirement_gal} gal = max(alt + reserve "
            f"{alternate_and_reserve_gal}, landing minimum {landing_minimum_gal}, pilot floor {pilot_floor_gal}). "
            "Landing minimum protects arrival at the destination; a diversion draws it down en route to the alternate."
        ),
    )
