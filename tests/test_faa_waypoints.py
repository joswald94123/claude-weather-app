"""Regression coverage for FAA cycle selection and waypoint-decoding edge cases."""

from __future__ import annotations

import datetime as dt
import gzip
import io
import json
from zipfile import ZipFile

import pytest

import faa_waypoints
from faa_waypoints import _airport_index_from_zip_bytes, _load_fallback_snapshot
from faa_waypoints import _choose_cycle_page, _extract_cycle_zip_urls
from faa_waypoints import get_faa_cycle_urls, resolve_faa_waypoint


class _FakeResponse:
    """Test helper for FakeResponse behavior."""

    def __init__(self, *, text: str = "", content: bytes = b"") -> None:
        self.text = text
        self.content = content

    def raise_for_status(self) -> None:
        return None


def test_faa_index_html_cache_expires_for_long_lived_processes(monkeypatch):
    """Verify FAA cycle-page HTML is refreshed when its bounded TTL advances."""

    calls = []
    faa_waypoints._cached_text_for_hour.cache_clear()
    monkeypatch.setattr(faa_waypoints, "_fetch_text", lambda url: calls.append(url) or "page")
    monkeypatch.setattr(faa_waypoints.time, "time", lambda: 0.0)

    assert faa_waypoints._cached_text("https://example.test") == "page"
    assert faa_waypoints._cached_text("https://example.test") == "page"
    monkeypatch.setattr(
        faa_waypoints.time,
        "time",
        lambda: float(faa_waypoints.FAA_INDEX_CACHE_TTL_SECONDS + 1),
    )
    assert faa_waypoints._cached_text("https://example.test") == "page"

    assert calls == ["https://example.test", "https://example.test"]


class _FakeSession:
    """Test helper for FakeSession behavior."""

    def __init__(self, responses: dict[str, _FakeResponse]) -> None:
        self._responses = responses

    def get(self, url: str, timeout: int | None = None):  # noqa: ARG002 - signature matches requests enough for tests
        if url not in self._responses:
            raise AssertionError(f"Unmapped FAA test URL: {url}")
        return self._responses[url]


def _zip_bytes(files: dict[str, str | bytes]) -> bytes:
    """Build an in-memory ZIP archive for FAA CSV fixture payloads."""

    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, contents in files.items():
            archive.writestr(name, contents)
    return buffer.getvalue()


FAA_INDEX_URL = "https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/"
CURRENT_CYCLE_URL = (
    "https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/2026-02-19/"
)
PREVIEW_CYCLE_URL = (
    "https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/2026-03-19/"
)
APT_URL = "https://nfdc.faa.gov/webContent/28DaySub/extra/19_Feb_2026_APT_CSV.zip"
FIX_URL = "https://nfdc.faa.gov/webContent/28DaySub/extra/19_Feb_2026_FIX_CSV.zip"
NAV_URL = "https://nfdc.faa.gov/webContent/28DaySub/extra/19_Feb_2026_NAV_CSV.zip"


def _build_session() -> _FakeSession:
    """Create a fake FAA session with index, cycle, and CSV ZIP responses."""

    index_html = f"""
    <html><body>
    <a href="{CURRENT_CYCLE_URL}">Current Subscription Data</a>
    <a href="{PREVIEW_CYCLE_URL}">Preview Subscription Data</a>
    </body></html>
    """
    cycle_html = f"""
    <html><body>
    <a href="{APT_URL}">Airports and Other Landing Facilities</a>
    <a href="{FIX_URL}">Fix/Reporting Point/Waypoint</a>
    <a href="{NAV_URL}">Navigation Aid</a>
    </body></html>
    """
    apt_zip = _zip_bytes(
        {
            "APT_BASE.csv": (
                "ARPT_ID,ICAO_ID,ARPT_NAME,LAT_DECIMAL,LONG_DECIMAL\n"
                "STS,KSTS,SONOMA COUNTY,38.5089,-122.8130\n"
            )
        }
    )
    fix_zip = _zip_bytes(
        {
            "FIX_BASE.csv": (
                "FIX_ID,LAT_DECIMAL,LONG_DECIMAL,FIX_USE_CODE\n"
                "CEDES,36.1010,-115.1660,RP\n"
            ),
            "FIX_CHRT.csv": "FIX_ID,CHARTING_TYPE_DESC\nCEDES,IAP\n",
        }
    )
    nav_zip = _zip_bytes(
        {
            "NAV_BASE.csv": (
                "NAV_ID,NAV_TYPE,NAME,LAT_DECIMAL,LONG_DECIMAL,STATE_CODE,CITY\n"
                "STS,VOR/DME,SANTA ROSA,38.5088,-122.8129,CA,SANTA ROSA\n"
                "OAL,VOR/DME,COALDALE,38.0000,-117.7690,NV,COALDALE\n"
            )
        }
    )

    return _FakeSession(
        {
            FAA_INDEX_URL: _FakeResponse(text=index_html),
            CURRENT_CYCLE_URL: _FakeResponse(text=cycle_html),
            PREVIEW_CYCLE_URL: _FakeResponse(text=cycle_html.replace("19_Feb_2026", "19_Mar_2026")),
            APT_URL: _FakeResponse(content=apt_zip),
            FIX_URL: _FakeResponse(content=fix_zip),
            NAV_URL: _FakeResponse(content=nav_zip),
            "https://nfdc.faa.gov/webContent/28DaySub/extra/19_Mar_2026_APT_CSV.zip": _FakeResponse(content=apt_zip),
            "https://nfdc.faa.gov/webContent/28DaySub/extra/19_Mar_2026_FIX_CSV.zip": _FakeResponse(content=fix_zip),
            "https://nfdc.faa.gov/webContent/28DaySub/extra/19_Mar_2026_NAV_CSV.zip": _FakeResponse(content=nav_zip),
        }
    )


def test_get_faa_cycle_urls_chooses_current_or_preview_by_reference_date():
    """Verify that get faa cycle urls chooses current or preview by reference date."""

    session = _build_session()

    current_cycle = get_faa_cycle_urls(reference_date=dt.date(2026, 3, 7), session=session)
    preview_cycle = get_faa_cycle_urls(reference_date=dt.date(2026, 3, 20), session=session)

    assert current_cycle.effective_date == dt.date(2026, 2, 19)
    assert preview_cycle.effective_date == dt.date(2026, 3, 19)


def test_resolve_faa_waypoint_prefers_airport_fix_or_navaid_by_identifier_shape():
    """Verify that resolve faa waypoint prefers airport fix or navaid by identifier shape."""

    session = _build_session()

    airport_waypoint, cycle_urls = resolve_faa_waypoint(
        "KSTS",
        reference_date=dt.date(2026, 3, 7),
        session=session,
    )
    navaid_waypoint, _ = resolve_faa_waypoint(
        "STS",
        reference_date=dt.date(2026, 3, 7),
        session=session,
    )
    fix_waypoint, _ = resolve_faa_waypoint(
        "CEDES",
        reference_date=dt.date(2026, 3, 7),
        session=session,
    )

    assert cycle_urls.effective_date == dt.date(2026, 2, 19)
    assert airport_waypoint is not None
    assert airport_waypoint.waypoint_type == "Airport"
    assert navaid_waypoint is not None
    assert navaid_waypoint.waypoint_type == "VOR/DME"
    assert navaid_waypoint.ambiguity_note is not None
    assert fix_waypoint is not None
    assert fix_waypoint.waypoint_type == "Fix"
    assert "IAP" in fix_waypoint.description


def test_resolve_faa_waypoint_tolerates_non_utf_csv_bytes():
    """Verify that resolve faa waypoint tolerates non utf csv bytes."""

    index_html = f"""
    <html><body>
    <a href="{CURRENT_CYCLE_URL}">Current Subscription Data</a>
    </body></html>
    """
    cycle_html = f"""
    <html><body>
    <a href="{APT_URL}">Airports and Other Landing Facilities</a>
    <a href="{FIX_URL}">Fix/Reporting Point/Waypoint</a>
    <a href="{NAV_URL}">Navigation Aid</a>
    </body></html>
    """
    apt_zip = _zip_bytes(
        {
            # Write the CSV in cp1252 so the resolver has to use its FAA-specific decoding fallback.
            "APT_BASE.csv": (
                b"ARPT_ID,ICAO_ID,ARPT_NAME,LAT_DECIMAL,LONG_DECIMAL\n"
                b"STS,KSTS,CAF\xe9 FIELD,38.5089,-122.8130\n"
            )
        }
    )
    session = _FakeSession(
        {
            FAA_INDEX_URL: _FakeResponse(text=index_html),
            CURRENT_CYCLE_URL: _FakeResponse(text=cycle_html),
            APT_URL: _FakeResponse(content=apt_zip),
            FIX_URL: _FakeResponse(
                content=_zip_bytes(
                    {
                        "FIX_BASE.csv": "FIX_ID,LAT_DECIMAL,LONG_DECIMAL,FIX_USE_CODE\n",
                        "FIX_CHRT.csv": "FIX_ID,CHARTING_TYPE_DESC\n",
                    }
                )
            ),
            NAV_URL: _FakeResponse(
                content=_zip_bytes(
                    {
                        "NAV_BASE.csv": "NAV_ID,NAV_TYPE,NAME,LAT_DECIMAL,LONG_DECIMAL,STATE_CODE,CITY\n",
                    }
                )
            ),
        }
    )

    waypoint, _ = resolve_faa_waypoint(
        "KSTS",
        reference_date=dt.date(2026, 3, 7),
        session=session,
    )

    assert waypoint is not None
    assert waypoint.name == "CAF\u00e9 FIELD"


def test_airport_archive_missing_expected_csv_has_actionable_error():
    """Verify a malformed FAA bundle identifies the missing member instead of leaking KeyError."""

    malformed_zip = _zip_bytes({"README.txt": "no airport CSV here"})

    with pytest.raises(RuntimeError, match="missing expected member APT_BASE.csv"):
        _airport_index_from_zip_bytes(malformed_zip)


def test_airport_archive_with_empty_index_is_valid_empty_data():
    """Verify a header-only FAA airport bundle returns an empty index deterministically."""

    empty_zip = _zip_bytes(
        {"APT_BASE.csv": "ARPT_ID,ICAO_ID,ARPT_NAME,LAT_DECIMAL,LONG_DECIMAL\n"}
    )

    assert _airport_index_from_zip_bytes(empty_zip) == {}


def test_resolve_faa_waypoint_uses_labeled_snapshot_when_live_faa_fails(tmp_path):
    """An FAA outage should use complete dated fallback data with visible provenance."""

    snapshot_path = tmp_path / "faa.json.gz"
    record = ["KSTS", 38.5089, -122.8130, "Airport", "FAA NASR APT", "SONOMA COUNTY", ""]
    payload = {
        "schema_version": 1,
        "effective_date": "2026-07-09",
        "source_page_url": CURRENT_CYCLE_URL,
        "indexes": {"airport": {"KSTS": record}, "nav": {}, "fix": {}},
    }
    with gzip.open(snapshot_path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)

    class OfflineSession:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("FAA offline")

    waypoint, cycle = resolve_faa_waypoint(
        "KSTS",
        session=OfflineSession(),  # type: ignore[arg-type]
        fallback_snapshot_path=str(snapshot_path),
    )

    assert waypoint is not None
    assert waypoint.name == "SONOMA COUNTY"
    assert waypoint.source.endswith("(offline snapshot 2026-07-09)")
    assert cycle.effective_date == dt.date(2026, 7, 9)


def test_fallback_selects_preview_only_on_or_after_its_effective_date(tmp_path):
    """Offline future planning should cross to preview data at its AIRAC boundary."""

    snapshot_path = tmp_path / "faa-two-cycle.json.gz"

    def cycle_payload(effective_date: str, airport_name: str) -> dict[str, object]:
        return {
            "effective_date": effective_date,
            "source_page_url": f"https://faa.example/{effective_date}/",
            "indexes": {
                "airport": {
                    "KSTS": [
                        "KSTS",
                        38.5089,
                        -122.8130,
                        "Airport",
                        "FAA NASR APT",
                        airport_name,
                        "",
                    ]
                },
                "nav": {},
                "fix": {},
            },
        }

    payload = {
        "schema_version": 2,
        "cycles": [
            cycle_payload("2026-07-09", "JULY DATA"),
            cycle_payload("2026-08-06", "AUGUST DATA"),
        ],
    }
    with gzip.open(snapshot_path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)

    class OfflineSession:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("FAA offline")

    july_waypoint, july_cycle = resolve_faa_waypoint(
        "KSTS",
        reference_date=dt.date(2026, 8, 5),
        session=OfflineSession(),  # type: ignore[arg-type]
        fallback_snapshot_path=str(snapshot_path),
    )
    august_waypoint, august_cycle = resolve_faa_waypoint(
        "KSTS",
        reference_date=dt.date(2026, 8, 6),
        session=OfflineSession(),  # type: ignore[arg-type]
        fallback_snapshot_path=str(snapshot_path),
    )

    assert july_waypoint is not None and july_waypoint.name == "JULY DATA"
    assert july_cycle.effective_date == dt.date(2026, 7, 9)
    assert august_waypoint is not None and august_waypoint.name == "AUGUST DATA"
    assert august_cycle.effective_date == dt.date(2026, 8, 6)


@pytest.mark.parametrize(
    "payload,error_pattern",
    [
        ({"schema_version": 99}, "unsupported schema version"),
        ({"schema_version": 2, "cycles": []}, "contains no cycles"),
        (
            {"schema_version": 2, "cycles": [{"effective_date": "", "indexes": {}}]},
            "contains no dated cycles",
        ),
    ],
)
def test_fallback_rejects_malformed_bundle_shapes(tmp_path, payload, error_pattern):
    """Malformed fallback metadata should fail with a specific operator-facing reason."""

    snapshot_path = tmp_path / "malformed.json.gz"
    with gzip.open(snapshot_path, "wt", encoding="utf-8") as handle:
        json.dump(payload, handle)

    _load_fallback_snapshot.cache_clear()
    with pytest.raises(RuntimeError, match=error_pattern):
        _load_fallback_snapshot(str(snapshot_path), "2026-07-19")


def test_resolver_reports_both_live_and_fallback_failures(tmp_path):
    """A total FAA failure should retain both causes instead of hiding the live outage."""

    missing_snapshot = tmp_path / "missing.json.gz"

    class OfflineSession:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("FAA network unavailable")

    with pytest.raises(RuntimeError) as exc_info:
        resolve_faa_waypoint(
            "KSTS",
            session=OfflineSession(),  # type: ignore[arg-type]
            fallback_snapshot_path=str(missing_snapshot),
        )

    message = str(exc_info.value)
    assert "Live FAA NASR lookup failed" in message
    assert "FAA network unavailable" in message
    assert "fallback also failed" in message
    assert "fallback snapshot is unreadable" in message


def test_empty_nasr_index_raises_actionable_error():
    """Verify an index page without cycle links fails with a named error."""

    with pytest.raises(RuntimeError, match="did not expose any cycle pages"):
        _choose_cycle_page([], reference_date=dt.date(2026, 7, 20))


def test_cycle_page_missing_csv_anchors_names_the_gaps():
    """Verify a cycle page without the NAV download names the missing product."""

    html_page = (
        '<a href="/files/2026-07-09_APT_CSV.zip">apt</a>'
        '<a href="/files/2026-07-09_FIX_CSV.zip">fix</a>'
    )

    with pytest.raises(RuntimeError, match="missing expected CSV downloads: nav"):
        _extract_cycle_zip_urls(html_page, base_url="https://example.com/cycle/")
