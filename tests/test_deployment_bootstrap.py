"""Regression coverage for release-boundary module cache eviction."""

from pathlib import Path
from types import ModuleType, SimpleNamespace

from deployment_bootstrap import refresh_repo_modules_for_release


def _module_at(name: str, path: Path) -> ModuleType:
    module = ModuleType(name)
    module.__file__ = str(path)
    return module


def test_release_change_evicts_all_cached_repo_modules(tmp_path):
    """A reused process must not retain any application module from the prior release."""

    repo_root = tmp_path / "app"
    repo_root.mkdir()
    entrypoint = repo_root / "streamlit_app.py"
    project_module = _module_at("weather_core", repo_root / "weather_core.py")
    nested_project_module = _module_at("helpers.route", repo_root / "helpers" / "route.py")
    protected_entrypoint = _module_at("streamlit_app", entrypoint)
    external_module = _module_at("streamlit", tmp_path / "site-packages" / "streamlit.py")
    modules = {
        "weather_core": project_module,
        "helpers.route": nested_project_module,
        "streamlit_app": protected_entrypoint,
        "streamlit": external_module,
    }
    state = SimpleNamespace(_codex_weather_brief_loaded_release="2026.07.20.10")

    evicted = refresh_repo_modules_for_release(
        repo_root=repo_root,
        release="2026.07.20.11",
        protected_paths=(entrypoint,),
        modules=modules,
        state=state,
    )

    assert evicted == ("helpers.route", "weather_core")
    assert set(modules) == {"streamlit_app", "streamlit"}
    assert state._codex_weather_brief_loaded_release == "2026.07.20.11"


def test_same_release_keeps_cached_repo_modules(tmp_path):
    """Ordinary Streamlit reruns keep their module cache when the release is unchanged."""

    repo_root = tmp_path / "app"
    repo_root.mkdir()
    project_module = _module_at("weather_core", repo_root / "weather_core.py")
    modules = {"weather_core": project_module}
    state = SimpleNamespace(_codex_weather_brief_loaded_release="2026.07.20.11")

    evicted = refresh_repo_modules_for_release(
        repo_root=repo_root,
        release="2026.07.20.11",
        modules=modules,
        state=state,
    )

    assert evicted == ()
    assert modules == {"weather_core": project_module}


def test_entrypoint_runs_release_bootstrap_before_application_imports():
    """Keep the cache boundary ahead of every import that could bind stale project code."""

    app_path = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    source = app_path.read_text(encoding="utf-8")

    bootstrap_position = source.index("_deployment_bootstrap.refresh_repo_modules_for_release")
    first_application_import = source.index("from app_version import")
    assert bootstrap_position < first_application_import
