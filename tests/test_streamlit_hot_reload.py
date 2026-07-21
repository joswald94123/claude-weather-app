"""Regression coverage for hosted Streamlit in-process deployment reloads."""

from pathlib import Path

import route_planning
import route_vertical_profile
from streamlit.testing.v1 import AppTest


def test_app_refreshes_stale_route_planning_module(monkeypatch):
    """Recover when Community Cloud retains a route module from before deployment."""

    monkeypatch.delattr(route_planning, "resolve_mission_headline")
    monkeypatch.delattr(route_planning, "destination_arrival_fuel_gal")

    app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=60)

    assert not app.exception
    assert hasattr(route_planning, "resolve_mission_headline")
    assert hasattr(route_planning, "destination_arrival_fuel_gal")


def test_app_refreshes_stale_route_vertical_profile_module(monkeypatch):
    """Recover when the hosted process retains the pre-interactive profile renderer."""

    monkeypatch.delattr(route_vertical_profile, "build_interactive_route_vertical_profile_html")

    app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=60)

    assert not app.exception
    assert hasattr(route_vertical_profile, "build_interactive_route_vertical_profile_html")


def test_departure_only_input_does_not_require_a_route_plan():
    """Keep the partial-input state safe when only one airport has an FAA cycle label."""

    app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    app = AppTest.from_file(str(app_path)).run(timeout=60)
    app.text_input(key="dep_icao").input("KSTS")

    app.run(timeout=60)

    assert not app.exception
