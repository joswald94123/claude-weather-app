"""Advance the calendar-version deployment sequence in RELEASE_VERSION."""

from __future__ import annotations

import argparse
import datetime as dt
import re
from pathlib import Path

VERSION_PATTERN = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})\.(\d+)$")


def next_release_version(current: str, release_date: dt.date) -> str:
    """Return YYYY.MM.DD.N, incrementing N only for the same release date."""

    match = VERSION_PATTERN.fullmatch(current.strip())
    date_prefix = release_date.strftime("%Y.%m.%d")
    if match and ".".join(match.groups()[:3]) == date_prefix:
        return f"{date_prefix}.{int(match.group(4)) + 1}"
    return f"{date_prefix}.1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Override release date as YYYY-MM-DD for reproducible automation.")
    args = parser.parse_args()
    release_date = dt.date.fromisoformat(args.date) if args.date else dt.datetime.now().astimezone().date()
    version_path = Path(__file__).resolve().parents[1] / "RELEASE_VERSION"
    current = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else ""
    next_version = next_release_version(current, release_date)
    version_path.write_text(f"{next_version}\n", encoding="utf-8")
    print(next_version)


if __name__ == "__main__":
    main()
