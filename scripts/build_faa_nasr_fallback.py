"""Build the compact dated FAA NASR fallback from the official active cycle."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from faa_waypoints import (
    FAA_FALLBACK_SCHEMA_VERSION,
    FAA_FALLBACK_SNAPSHOT,
    FaaCycleUrls,
    FaaWaypoint,
    _airport_index_from_zip_bytes,
    _fetch_bytes,
    _fix_index_from_zip_bytes,
    _nav_index_from_zip_bytes,
    get_faa_cycle_urls,
)


def _encoded_index(index: dict[str, FaaWaypoint]) -> dict[str, list[object]]:
    """Encode repeated waypoint records compactly while retaining lookup aliases."""

    return {
        key: [
            waypoint.identifier,
            waypoint.latitude,
            waypoint.longitude,
            waypoint.waypoint_type,
            waypoint.source,
            waypoint.name,
            waypoint.description,
        ]
        for key, waypoint in sorted(index.items())
    }


def _encoded_cycle(cycle: FaaCycleUrls) -> dict[str, object]:
    """Download and encode the FAA cycle effective for one planning date."""

    indexes = {
        "airport": _airport_index_from_zip_bytes(_fetch_bytes(cycle.apt_url)),
        "nav": _nav_index_from_zip_bytes(_fetch_bytes(cycle.nav_url)),
        "fix": _fix_index_from_zip_bytes(_fetch_bytes(cycle.fix_url)),
    }
    if any(not index for index in indexes.values()):
        raise RuntimeError("Refusing to write an incomplete FAA NASR fallback snapshot.")

    return {
        "effective_date": cycle.effective_date.isoformat(),
        "source_page_url": cycle.page_url,
        "counts": {name: len(index) for name, index in indexes.items()},
        "indexes": {name: _encoded_index(index) for name, index in indexes.items()},
    }


def build_snapshot(*, output_path: Path, reference_date: dt.date) -> None:
    """Write complete current and preview FAA cycles for offline date-aware lookup."""

    candidate_dates = (reference_date, reference_date + dt.timedelta(days=28))
    cycles_by_date = {
        cycle.effective_date.isoformat(): cycle
        for cycle in (get_faa_cycle_urls(reference_date=date) for date in candidate_dates)
    }
    existing_cycles_by_date: dict[str, dict[str, object]] = {}
    if output_path.exists():
        try:
            with gzip.open(output_path, "rt", encoding="utf-8") as handle:
                existing_payload = json.load(handle)
            existing_dates = sorted(
                str(cycle["effective_date"])
                for cycle in existing_payload.get("cycles", [])
                if isinstance(cycle, dict) and cycle.get("effective_date")
            )
            existing_cycles_by_date = {
                str(cycle["effective_date"]): cycle
                for cycle in existing_payload.get("cycles", [])
                if isinstance(cycle, dict) and cycle.get("effective_date")
            }
            if (
                existing_payload.get("schema_version") == FAA_FALLBACK_SCHEMA_VERSION
                and existing_dates == sorted(cycles_by_date)
            ):
                print(f"FAA NASR fallback is current for cycles: {', '.join(existing_dates)}")
                return
        except (OSError, ValueError, KeyError, TypeError):
            # An unreadable or older artifact should be replaced by a fully
            # validated current/preview bundle below.
            pass

    # Preserve a previously captured preview if the FAA temporarily exposes only
    # the current cycle while this refresh runs; newly downloaded cycles win.
    encoded_by_date = dict(existing_cycles_by_date)
    for cycle in cycles_by_date.values():
        encoded = _encoded_cycle(cycle)
        encoded_by_date[str(encoded["effective_date"])] = encoded

    ordered_dates = sorted(encoded_by_date)
    current_dates = [date for date in ordered_dates if dt.date.fromisoformat(date) <= reference_date]
    preview_dates = [date for date in ordered_dates if dt.date.fromisoformat(date) > reference_date]
    retained_dates = ([current_dates[-1]] if current_dates else []) + ([preview_dates[0]] if preview_dates else [])
    if len(retained_dates) < 2 and len(ordered_dates) >= 2:
        retained_dates = ordered_dates[-2:]

    payload = {
        "schema_version": FAA_FALLBACK_SCHEMA_VERSION,
        "generated_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "cycles": [encoded_by_date[key] for key in retained_dates],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(output_path, "wt", encoding="utf-8", compresslevel=9) as handle:
        json.dump(payload, handle, separators=(",", ":"), ensure_ascii=False)
    summaries = [
        f"{cycle['effective_date']}: {cycle['counts']}"
        for cycle in payload["cycles"]
    ]
    print(f"Wrote {output_path} with " + "; ".join(summaries))


def main() -> None:
    """Parse CLI arguments and build the fallback artifact."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=FAA_FALLBACK_SNAPSHOT)
    parser.add_argument("--reference-date", type=dt.date.fromisoformat, default=dt.date.today())
    args = parser.parse_args()
    build_snapshot(output_path=args.output, reference_date=args.reference_date)


if __name__ == "__main__":
    main()
