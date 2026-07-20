"""Regression coverage for visible running-version resolution."""

import datetime as dt

from app_version import resolve_python_version, resolve_running_version
from scripts.bump_release_version import next_release_version


def test_resolve_running_version_combines_release_and_host_build(monkeypatch, tmp_path):
    """Verify a human release remains primary while the host SHA is traceable."""

    monkeypatch.setenv("APP_RELEASE_VERSION", "2026.07.19.1")
    monkeypatch.setenv("STREAMLIT_GIT_COMMIT", "1234567890abcdef")

    assert resolve_running_version(tmp_path) == "v2026.07.19.1 · build 1234567890ab"


def test_resolve_running_version_falls_back_to_unknown_outside_git(monkeypatch, tmp_path):
    """Verify that resolve running version falls back to unknown outside git."""

    for key in ("STREAMLIT_GIT_COMMIT", "GITHUB_SHA", "COMMIT_SHA", "SOURCE_VERSION"):
        monkeypatch.delenv(key, raising=False)

    monkeypatch.delenv("APP_RELEASE_VERSION", raising=False)
    assert resolve_running_version(tmp_path) == "unversioned"


def test_next_release_version_increments_each_same_day_deployment():
    """Verify same-day deployments increment and a new date resets the sequence."""

    assert next_release_version("2026.07.19.1", dt.date(2026, 7, 19)) == "2026.07.19.2"
    assert next_release_version("2026.07.19.9", dt.date(2026, 7, 20)) == "2026.07.20.1"


def test_resolve_python_version_reports_actual_interpreter():
    """Verify the visible runtime label is sourced from the active interpreter."""

    value = resolve_python_version()

    assert value.startswith("Python 3.")
    assert len(value.split(".")) == 3
