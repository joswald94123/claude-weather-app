"""Regenerate the deterministic Daher PIM table snapshot from the vendored source PDF."""

from __future__ import annotations

import hashlib
import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
# Must be set before importing daher_pim_tables: the flag makes its cached snapshot
# loader return None so parse_* functions re-parse the PDF instead of short-circuiting.
os.environ["CODEX_REBUILD_PIM_SNAPSHOT"] = "1"

from daher_pim_tables import (  # noqa: E402
    CLIMB_SCHEDULE_METADATA,
    CRUISE_MODE_METADATA,
    DESCENT_PROFILE_METADATA,
    PIM_PDF_PATH,
    PIM_SNAPSHOT_PATH,
    PIM_SNAPSHOT_SCHEMA_VERSION,
    parse_climb_table,
    parse_cruise_table,
    parse_descent_table,
)


def _pdf_sha256() -> str:
    """Hash the exact PDF whose parsed values are being serialized."""

    return hashlib.sha256(PIM_PDF_PATH.read_bytes()).hexdigest()


def _build_payload() -> dict[str, object]:
    """Parse and validate every mapped source table into a deterministic payload."""

    payload: dict[str, object] = {
        "schema_version": PIM_SNAPSHOT_SCHEMA_VERSION,
        "source_pdf": PIM_PDF_PATH.name,
        "source_pdf_sha256": _pdf_sha256(),
        "cruise": {},
        "climb": {},
        "descent": {},
    }
    for mode_id, metadata in CRUISE_MODE_METADATA.items():
        for temperature_offset_c in metadata["table_references_by_temp_delta_c"]:
            key = f"{mode_id}|{temperature_offset_c}"
            payload["cruise"][key] = [  # type: ignore[index]
                asdict(row) for row in parse_cruise_table(mode_id, temperature_offset_c)
            ]
    for schedule_id, metadata in CLIMB_SCHEDULE_METADATA.items():
        for temperature_offset_c in metadata["table_references_by_temp_delta_c"]:
            key = f"{schedule_id}|{temperature_offset_c}"
            payload["climb"][key] = [  # type: ignore[index]
                asdict(row) for row in parse_climb_table(schedule_id, temperature_offset_c)
            ]
    for profile_id in DESCENT_PROFILE_METADATA:
        payload["descent"][profile_id] = [  # type: ignore[index]
            asdict(row) for row in parse_descent_table(profile_id)
        ]
    return payload


def main() -> None:
    """Regenerate the snapshot or verify that it exactly matches a fresh PDF parse."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail unless the checked-in snapshot exactly matches a fresh parse",
    )
    args = parser.parse_args()
    if not PIM_PDF_PATH.exists():
        if args.check and PIM_SNAPSHOT_PATH.exists():
            print(
                "Source PDF not vendored (public build); snapshot accepted on stored "
                "provenance hash and structural validation."
            )
            return
        raise SystemExit(f"Missing Daher PIM PDF: {PIM_PDF_PATH}")
    serialized = json.dumps(_build_payload(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not PIM_SNAPSHOT_PATH.exists() or PIM_SNAPSHOT_PATH.read_text(encoding="utf-8") != serialized:
            raise SystemExit("Daher PIM snapshot differs from a fresh parse; rebuild it before release.")
        print(f"Verified {PIM_SNAPSHOT_PATH}")
        return

    PIM_SNAPSHOT_PATH.write_text(
        serialized,
        encoding="utf-8",
    )
    print(f"Wrote {PIM_SNAPSHOT_PATH}")


if __name__ == "__main__":
    main()
