"""Runtime version helpers for local and hosted Streamlit builds."""

from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path


VERSION_ENV_KEYS = (
    "STREAMLIT_GIT_COMMIT",
    "GITHUB_SHA",
    "COMMIT_SHA",
    "SOURCE_VERSION",
)
RELEASE_VERSION_ENV_KEY = "APP_RELEASE_VERSION"
RELEASE_VERSION_FILE = "RELEASE_VERSION"


def resolve_python_version() -> str:
    """Return the interpreter version actually running the app process."""

    return f"Python {platform.python_version()}"


def _shorten_sha(value: str) -> str:
    """Return a display-length commit identifier while preserving non-SHA labels."""

    text = (value or "").strip()
    if len(text) >= 12 and all(char in "0123456789abcdefABCDEF" for char in text[:12]):
        return text[:12]
    return text


def _resolve_build_identifier(root: Path) -> str | None:
    """Resolve an optional short commit identifier for build traceability."""

    for key in VERSION_ENV_KEYS:
        value = os.getenv(key)
        if value:
            return _shorten_sha(value)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short=12", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return None

    version = result.stdout.strip()
    return version or None


def resolve_running_version(repo_path: str | Path | None = None) -> str:
    """Resolve the human release version plus optional build traceability."""

    root = Path(repo_path) if repo_path is not None else Path(__file__).resolve().parent
    release = os.getenv(RELEASE_VERSION_ENV_KEY, "").strip()
    if not release:
        try:
            release = (root / RELEASE_VERSION_FILE).read_text(encoding="utf-8").strip()
        except Exception:
            release = "unversioned"
    if release != "unversioned" and not release.startswith("v"):
        release = f"v{release}"
    build = _resolve_build_identifier(root)
    return f"{release} · build {build}" if build else release
