"""AppTest guardrail: rendered mission text must equal the computed document.

The repo's rule is that the UI renders numbers the core computed and never
re-derives them. The core goldens pin what the document contains; this test
closes the last seam — core to screen — by running the real app offline
(fixture weather, offline-snapshot FAA data) and asserting that the hero
pill and fuel cards show the document's fields verbatim.
"""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import faa_waypoints
import weather_core
import wxcore.feeds
from feed_fixtures import FixtureFeedSession
from ui_presenters import landing_fuel_presentation

APP_PATH = str(Path(__file__).resolve().parents[1] / "streamlit_app.py")
RUN_TIMEOUT_SECONDS = 240


@pytest.fixture()
def offline_app(monkeypatch):
    """Force the whole app onto recorded weather and the vendored FAA snapshot."""

    real_fetch = weather_core.fetch_noaa_weather

    def fixture_fetch(icaos, **kwargs):
        kwargs["session"] = FixtureFeedSession()
        return real_fetch(icaos, **kwargs)

    def no_network(url, **kwargs):
        raise RuntimeError(f"network disabled by the guardrail test: {url}")

    # The app imports fetch_noaa_weather from the weather_core shim; the AVWX
    # lookup is an internal call, so it is patched where its caller resolves it.
    monkeypatch.setattr(weather_core, "fetch_noaa_weather", fixture_fetch)
    monkeypatch.setattr(wxcore.feeds, "_lookup_airport_from_avwx", lambda *args, **kwargs: None)
    monkeypatch.setattr(faa_waypoints, "_fetch_text", no_network)
    monkeypatch.setattr(faa_waypoints, "_fetch_bytes", no_network)
    faa_waypoints._cached_text_for_hour.cache_clear()
    return AppTest.from_file(APP_PATH, default_timeout=RUN_TIMEOUT_SECONDS)


def _plan_mission(at: AppTest, *, departure: str, destination: str, route: str = "", fuel_stops: str = ""):
    """Drive the sidebar exactly as a pilot would and rerun to a computed brief."""

    at.run()
    at.text_input(key="dep_icao").set_value(departure)
    at.text_input(key="arr_icao").set_value(destination)
    if route:
        at.text_area(key="route_waypoints_text").set_value(route)
    if fuel_stops:
        at.text_input(key="fuel_stop_waypoints_text").set_value(fuel_stops)
    at.run()
    at.date_input(key="etd_date").set_value(dt.date.today() + dt.timedelta(days=2))
    at.selectbox(key="etd_hour").select(10)
    at.selectbox(key="etd_minute").select("00")
    at.selectbox(key="etd_ampm").select("AM")
    cruise = at.selectbox(key="cruise_flight_level")
    cruise.select(cruise.options[1])
    at.run()
    assert not at.exception
    return at.session_state["_mission_document_for_tests"]


def _rendered_corpus(at: AppTest) -> str:
    """Collect every rendered markdown/caption body the assertions search."""

    parts = [str(element.value) for element in at.markdown]
    parts.extend(str(element.value) for element in at.caption)
    return "\n".join(parts)


def test_nonstop_hero_and_fuel_card_render_the_document_verbatim(offline_app):
    """KSTS->KBFL: pill margin and FOB card must equal document fields exactly."""

    document = _plan_mission(offline_app, departure="KSTS", destination="KBFL")
    headline = document.mission_headline
    corpus = _rendered_corpus(offline_app)

    assert headline.basis != "multi-leg"
    assert f"Reserve margin {headline.reserve_margin_gal:+d} gal" in corpus

    focus_point = document.focus_point
    assert focus_point is not None
    ledger = focus_point.fuel_ledger
    expected_card = landing_fuel_presentation(
        fuel_on_board_gal=int(focus_point.fuel_at_dest),
        fuel_status=str(focus_point.fuel_status),
        effective_requirement_gal=int(focus_point.required_landing_fuel_gal),
        alternate_and_reserve_gal=int(focus_point.calculated_required_landing_fuel_gal),
        landing_minimum_gal=int(math.ceil(60.0)),
        pilot_floor_gal=int(focus_point.reserve_floor_gal),
        reserve_margin_gal=int(focus_point.reserve_margin_gal),
        taxi_fuel_gal=ledger.taxi_fuel_gal if ledger else None,
        climb_fuel_gal=ledger.climb_fuel_gal if ledger else None,
        cruise_fuel_gal=ledger.cruise_fuel_gal if ledger else None,
        descent_fuel_gal=ledger.descent_fuel_gal if ledger else None,
    )
    assert f"{headline.fob_at_landing_gal} gal" in corpus
    assert expected_card.card_detail in corpus


def test_multi_leg_hero_reports_the_documents_worst_leg_verbatim(offline_app):
    """KSTS->KBFL->KFFZ: the hero and FOB card must carry the worst-leg fields."""

    document = _plan_mission(
        offline_app,
        departure="KSTS",
        destination="KFFZ",
        route="KBFL",
        fuel_stops="KBFL",
    )
    headline = document.mission_headline
    corpus = _rendered_corpus(offline_app)

    assert headline.basis == "multi-leg"
    assert (
        f"Worst leg reserve margin {headline.reserve_margin_gal:+d} gal ({headline.margin_leg_label})"
        in corpus
    )
    assert f"{headline.fob_at_landing_gal} gal" in corpus
    assert (
        "Gross touchdown FOB after planned fuel stops | worst leg margin "
        f"{headline.reserve_margin_gal:+d} gal ({headline.margin_leg_label})"
    ) in corpus
    assert document.multi_leg_plan is not None
    assert len(document.multi_leg_plan.legs) == 2
