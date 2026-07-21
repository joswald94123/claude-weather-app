"""Source-level contracts that keep UI styling and column wiring from drifting."""

from __future__ import annotations

import re
from pathlib import Path

APP_SOURCE = (Path(__file__).resolve().parents[1] / "streamlit_app.py").read_text(encoding="utf-8")


def test_every_emitted_margin_tone_class_has_hero_and_pill_styling():
    """A tone class the code can emit must exist in the hero and pill CSS selectors."""

    for tone_class in ("tone-moderate", "tone-caution", "tone-high"):
        assert f".route-hero.{tone_class}" in APP_SOURCE, tone_class
        assert f".route-pill.{tone_class}" in APP_SOURCE, tone_class


def test_hazard_styler_columns_match_the_matrix_row_keys():
    """The hazard-cell styling filter must name real dataframe columns."""

    match = re.search(r"for column in \(([^)]*)\)\s*\n\s*if column in df\.columns", APP_SOURCE)
    if match is None:
        match = re.search(r"for column in \(([^)]*)\)", APP_SOURCE)
    assert match is not None
    styled_columns = re.findall(r'"([^"]+)"', match.group(1))
    assert styled_columns
    for column in styled_columns:
        assert f'"{column}":' in APP_SOURCE, f"styled column {column!r} is not a matrix row key"


def test_weather_cache_contracts_hold():
    """Pin the refresh target, release-keyed cache signatures, and degraded-stash TTL."""

    assert "_cached_successful_noaa_weather.clear()" in APP_SOURCE
    assert APP_SOURCE.count("app_release: str") >= 2
    assert "_DEGRADED_WEATHER_TTL_SECONDS = 120" in APP_SOURCE
    assert "_DEGRADED_WEATHER_STASH_KEY" in APP_SOURCE
