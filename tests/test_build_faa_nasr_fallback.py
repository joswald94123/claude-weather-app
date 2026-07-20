"""Regression coverage for the reproducible FAA NASR fallback builder."""

from __future__ import annotations

import datetime as dt
import gzip
import json

from faa_waypoints import FAA_FALLBACK_SCHEMA_VERSION, FaaCycleUrls
from scripts import build_faa_nasr_fallback


def test_rebuild_preserves_existing_preview_when_faa_exposes_only_current(tmp_path, monkeypatch):
    """Verify a transient missing preview cannot regress the checked-in two-cycle bundle."""

    output_path = tmp_path / "faa.json.gz"
    existing_payload = {
        "schema_version": FAA_FALLBACK_SCHEMA_VERSION,
        "cycles": [
            {"effective_date": "2026-07-09", "counts": {}, "indexes": {}},
            {"effective_date": "2026-08-06", "counts": {}, "indexes": {}},
        ],
    }
    with gzip.open(output_path, "wt", encoding="utf-8") as handle:
        json.dump(existing_payload, handle)

    current_cycle = FaaCycleUrls(
        effective_date=dt.date(2026, 7, 9),
        page_url="current",
        apt_url="apt",
        fix_url="fix",
        nav_url="nav",
    )
    monkeypatch.setattr(build_faa_nasr_fallback, "get_faa_cycle_urls", lambda reference_date: current_cycle)
    monkeypatch.setattr(
        build_faa_nasr_fallback,
        "_encoded_cycle",
        lambda cycle: {"effective_date": cycle.effective_date.isoformat(), "counts": {}, "indexes": {}},
    )

    build_faa_nasr_fallback.build_snapshot(
        output_path=output_path,
        reference_date=dt.date(2026, 7, 20),
    )

    with gzip.open(output_path, "rt", encoding="utf-8") as handle:
        rebuilt = json.load(handle)
    assert [cycle["effective_date"] for cycle in rebuilt["cycles"]] == ["2026-07-09", "2026-08-06"]
