"""Online FAA NASR waypoint lookup for airports, navaids, and fixes."""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import io
import json
import re
import time
from dataclasses import dataclass, replace
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin
from zipfile import ZipFile

import requests

FAA_NASR_INDEX_URL = "https://www.faa.gov/air_traffic/flight_info/aeronav/aero_data/NASR_Subscription/"
REQUEST_TIMEOUT_SECONDS = 30
FAA_INDEX_CACHE_TTL_SECONDS = 3600
FAA_FALLBACK_SNAPSHOT = Path(__file__).resolve().parent / "assets" / "faa_nasr_fallback.json.gz"
FAA_FALLBACK_SCHEMA_VERSION = 2


@dataclass(frozen=True)
class FaaCycleUrls:
    """Resolved FAA cycle page and the three CSV bundles used by route lookup."""

    effective_date: dt.date
    page_url: str
    apt_url: str
    fix_url: str
    nav_url: str


@dataclass(frozen=True)
class FaaWaypoint:
    """FAA waypoint record normalized for route planning and UI messaging."""

    identifier: str
    latitude: float
    longitude: float
    waypoint_type: str
    source: str
    name: str = ""
    description: str = ""
    ambiguity_note: str | None = None


class _AnchorCollector(HTMLParser):
    """Small HTML parser that captures anchor href/text pairs without extra dependencies."""

    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[tuple[str, str]] = []
        self._current_href: str | None = None
        self._current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        self._current_href = dict(attrs).get("href")
        self._current_text = []

    def handle_data(self, data: str) -> None:
        if self._current_href is not None:
            self._current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or self._current_href is None:
            return
        text = " ".join(part.strip() for part in self._current_text if part.strip()).strip()
        self.anchors.append((self._current_href, text))
        self._current_href = None
        self._current_text = []


def _normalize_identifier(identifier: str) -> str:
    """Normalize user-entered FAA identifiers for dictionary lookups."""

    return re.sub(r"[^A-Z0-9]", "", (identifier or "").upper())


def _fetch_text(url: str, *, session: requests.Session | object | None = None) -> str:
    """Fetch text from FAA pages, using an injected session in tests."""

    client = session or requests
    response = client.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return getattr(response, "text")


def _fetch_bytes(url: str, *, session: requests.Session | object | None = None) -> bytes:
    """Fetch binary FAA ZIP content, tolerating text-only fake responses in tests."""

    client = session or requests
    response = client.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    content = getattr(response, "content", None)
    if content is None:
        text = getattr(response, "text", "")
        return text.encode("utf-8")
    return bytes(content)


@lru_cache(maxsize=16)
def _cached_text_for_hour(url: str, cache_bucket: int) -> str:
    """Fetch FAA HTML once per bounded cache bucket."""

    _ = cache_bucket
    return _fetch_text(url)


def _cached_text(url: str) -> str:
    """Cache FAA index HTML briefly so long-lived apps still discover new cycles."""

    cache_bucket = int(time.time() // FAA_INDEX_CACHE_TTL_SECONDS)
    return _cached_text_for_hour(url, cache_bucket)


def _anchors_from_html(html_text: str, *, base_url: str) -> list[tuple[str, str]]:
    """Extract absolute anchor URLs plus link text from an FAA HTML page."""

    parser = _AnchorCollector()
    parser.feed(html_text)
    return [(urljoin(base_url, href), text) for href, text in parser.anchors if href]


def _cycle_pages_from_index(index_html: str) -> list[tuple[dt.date, str]]:
    """Find dated NASR subscription cycle pages from the FAA index page."""

    cycle_pages: dict[str, dt.date] = {}
    for absolute_url, _text in _anchors_from_html(index_html, base_url=FAA_NASR_INDEX_URL):
        match = re.search(r"/NASR_Subscription/(\d{4}-\d{2}-\d{2})/?", absolute_url)
        if match:
            cycle_pages[absolute_url.rstrip("/") + "/"] = dt.date.fromisoformat(match.group(1))
    return sorted(((effective_date, page_url) for page_url, effective_date in cycle_pages.items()))


def _choose_cycle_page(
    cycle_pages: list[tuple[dt.date, str]],
    *,
    reference_date: dt.date,
) -> tuple[dt.date, str]:
    """Choose the NASR cycle effective for the requested planning date."""

    if not cycle_pages:
        raise RuntimeError("FAA NASR index did not expose any cycle pages.")

    # Use the effective cycle for the planned flight date so future-dated briefs can pick preview data.
    current_or_past = [item for item in cycle_pages if item[0] <= reference_date]
    if current_or_past:
        return current_or_past[-1]
    return cycle_pages[0]


def _extract_cycle_zip_urls(cycle_page_html: str, *, base_url: str) -> tuple[str, str, str]:
    """Locate the APT, FIX, and NAV CSV ZIP downloads on one cycle page."""

    discovered_urls: dict[str, str] = {}
    for absolute_url, _text in _anchors_from_html(cycle_page_html, base_url=base_url):
        upper_url = absolute_url.upper()
        if upper_url.endswith("_APT_CSV.ZIP"):
            discovered_urls["apt"] = absolute_url
        elif upper_url.endswith("_FIX_CSV.ZIP"):
            discovered_urls["fix"] = absolute_url
        elif upper_url.endswith("_NAV_CSV.ZIP"):
            discovered_urls["nav"] = absolute_url

    missing = [name for name in ("apt", "fix", "nav") if name not in discovered_urls]
    if missing:
        raise RuntimeError(f"FAA cycle page is missing expected CSV downloads: {', '.join(missing)}")
    return discovered_urls["apt"], discovered_urls["fix"], discovered_urls["nav"]


def get_faa_cycle_urls(
    *,
    reference_date: dt.date | None = None,
    session: requests.Session | object | None = None,
) -> FaaCycleUrls:
    """Resolve the active FAA cycle from the live NASR index for the planned flight date."""

    target_date = reference_date or dt.date.today()
    index_html = _fetch_text(FAA_NASR_INDEX_URL, session=session) if session else _cached_text(FAA_NASR_INDEX_URL)
    effective_date, page_url = _choose_cycle_page(
        _cycle_pages_from_index(index_html),
        reference_date=target_date,
    )
    cycle_html = _fetch_text(page_url, session=session) if session else _cached_text(page_url)
    apt_url, fix_url, nav_url = _extract_cycle_zip_urls(cycle_html, base_url=page_url)
    return FaaCycleUrls(
        effective_date=effective_date,
        page_url=page_url,
        apt_url=apt_url,
        fix_url=fix_url,
        nav_url=nav_url,
    )


def _iter_csv_rows(zip_bytes: bytes, *, csv_name: str) -> Iterable[dict[str, str]]:
    """Decode FAA CSV members even when the published bundle is not strict UTF-8."""

    try:
        with ZipFile(io.BytesIO(zip_bytes)) as archive:
            raw_csv_bytes = archive.read(csv_name)
    except KeyError as exc:
        raise RuntimeError(f"FAA NASR archive is missing expected member {csv_name}.") from exc
    except Exception as exc:
        raise RuntimeError(f"FAA NASR archive for {csv_name} is unreadable.") from exc

    last_decode_error: UnicodeDecodeError | None = None
    for encoding_name in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            decoded_csv = raw_csv_bytes.decode(encoding_name)
            yield from csv.DictReader(io.StringIO(decoded_csv, newline=""))
            return
        except UnicodeDecodeError as exc:
            last_decode_error = exc

    if last_decode_error is not None:
        raise last_decode_error


def _airport_index_from_zip_bytes(zip_bytes: bytes) -> dict[str, FaaWaypoint]:
    """Parse the FAA airport CSV bundle into lookup records keyed by ICAO/local ID."""

    index: dict[str, FaaWaypoint] = {}
    for row in _iter_csv_rows(zip_bytes, csv_name="APT_BASE.csv"):
        lat = row.get("LAT_DECIMAL")
        lon = row.get("LONG_DECIMAL")
        if not lat or not lon:
            continue
        canonical_identifier = _normalize_identifier(row.get("ICAO_ID") or row.get("ARPT_ID") or "")
        if not canonical_identifier:
            continue
        waypoint = FaaWaypoint(
            identifier=canonical_identifier,
            latitude=float(lat),
            longitude=float(lon),
            waypoint_type="Airport",
            source="FAA NASR APT",
            name=(row.get("ARPT_NAME") or canonical_identifier).strip(),
        )
        for raw_key in (row.get("ICAO_ID", ""), row.get("ARPT_ID", "")):
            key = _normalize_identifier(raw_key)
            if key:
                index[key] = waypoint
    return index


def _nav_index_from_zip_bytes(zip_bytes: bytes) -> dict[str, FaaWaypoint]:
    """Parse the FAA navaid CSV bundle into route waypoint lookup records."""

    index: dict[str, FaaWaypoint] = {}
    for row in _iter_csv_rows(zip_bytes, csv_name="NAV_BASE.csv"):
        key = _normalize_identifier(row.get("NAV_ID", ""))
        lat = row.get("LAT_DECIMAL")
        lon = row.get("LONG_DECIMAL")
        if not key or not lat or not lon:
            continue
        nav_type = (row.get("NAV_TYPE") or "Navaid").strip()
        name = (row.get("NAME") or key).strip()
        city = (row.get("CITY") or "").strip()
        state_code = (row.get("STATE_CODE") or "").strip()
        description_parts = [part for part in (city, state_code) if part]
        index[key] = FaaWaypoint(
            identifier=key,
            latitude=float(lat),
            longitude=float(lon),
            waypoint_type=nav_type,
            source="FAA NASR NAV",
            name=name,
            description=", ".join(description_parts),
        )
    return index


def _fix_index_from_zip_bytes(zip_bytes: bytes) -> dict[str, FaaWaypoint]:
    """Parse the FAA fix CSV bundle and preserve chart-use context for UI notes."""

    charting_types: dict[str, set[str]] = {}
    for row in _iter_csv_rows(zip_bytes, csv_name="FIX_CHRT.csv"):
        key = _normalize_identifier(row.get("FIX_ID", ""))
        chart_type = (row.get("CHARTING_TYPE_DESC") or "").strip()
        if key and chart_type:
            charting_types.setdefault(key, set()).add(chart_type)

    index: dict[str, FaaWaypoint] = {}
    for row in _iter_csv_rows(zip_bytes, csv_name="FIX_BASE.csv"):
        key = _normalize_identifier(row.get("FIX_ID", ""))
        lat = row.get("LAT_DECIMAL")
        lon = row.get("LONG_DECIMAL")
        if not key or not lat or not lon:
            continue
        use_code = (row.get("FIX_USE_CODE") or "").strip()
        chart_desc = ", ".join(sorted(charting_types.get(key, set())))
        description_parts = [part for part in (use_code, chart_desc) if part]
        index[key] = FaaWaypoint(
            identifier=key,
            latitude=float(lat),
            longitude=float(lon),
            waypoint_type="Fix",
            source="FAA NASR FIX",
            name=key,
            description=" | ".join(description_parts),
        )
    return index


@lru_cache(maxsize=8)
def _cached_airport_index(zip_url: str) -> dict[str, FaaWaypoint]:
    """Return a cached airport lookup index for one FAA cycle ZIP URL."""

    return _airport_index_from_zip_bytes(_fetch_bytes(zip_url))


@lru_cache(maxsize=8)
def _cached_nav_index(zip_url: str) -> dict[str, FaaWaypoint]:
    """Return a cached navaid lookup index for one FAA cycle ZIP URL."""

    return _nav_index_from_zip_bytes(_fetch_bytes(zip_url))


@lru_cache(maxsize=8)
def _cached_fix_index(zip_url: str) -> dict[str, FaaWaypoint]:
    """Return a cached fix lookup index for one FAA cycle ZIP URL."""

    return _fix_index_from_zip_bytes(_fetch_bytes(zip_url))


def _load_indexes_for_cycle(
    cycle_urls: FaaCycleUrls,
    *,
    session: requests.Session | object | None = None,
) -> tuple[dict[str, FaaWaypoint], dict[str, FaaWaypoint], dict[str, FaaWaypoint]]:
    """Load airport, navaid, and fix indexes for the resolved NASR cycle."""

    if session is None:
        # Cached ZIP parsing is only used on the normal runtime path; tests can inject a custom session.
        return (
            _cached_airport_index(cycle_urls.apt_url),
            _cached_nav_index(cycle_urls.nav_url),
            _cached_fix_index(cycle_urls.fix_url),
        )

    return (
        _airport_index_from_zip_bytes(_fetch_bytes(cycle_urls.apt_url, session=session)),
        _nav_index_from_zip_bytes(_fetch_bytes(cycle_urls.nav_url, session=session)),
        _fix_index_from_zip_bytes(_fetch_bytes(cycle_urls.fix_url, session=session)),
    )


@lru_cache(maxsize=2)
def _load_fallback_snapshot(
    snapshot_path: str = str(FAA_FALLBACK_SNAPSHOT),
    reference_date_iso: str = "",
) -> tuple[FaaCycleUrls, dict[str, FaaWaypoint], dict[str, FaaWaypoint], dict[str, FaaWaypoint]]:
    """Load the effective fallback cycle for the requested planning date."""

    path = Path(snapshot_path)
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        raise RuntimeError(f"FAA NASR fallback snapshot is unreadable: {path.name}.") from exc

    schema_version = payload.get("schema_version")
    if schema_version not in {1, FAA_FALLBACK_SCHEMA_VERSION}:
        raise RuntimeError("FAA NASR fallback snapshot has an unsupported schema version.")
    cycle_payloads = payload.get("cycles") if schema_version == FAA_FALLBACK_SCHEMA_VERSION else [payload]
    if not isinstance(cycle_payloads, list) or not cycle_payloads:
        raise RuntimeError("FAA NASR fallback snapshot contains no cycles.")
    target_date = dt.date.fromisoformat(reference_date_iso) if reference_date_iso else dt.date.today()
    dated_cycles = sorted(
        (
            dt.date.fromisoformat(str(cycle_payload["effective_date"])),
            cycle_payload,
        )
        for cycle_payload in cycle_payloads
        if isinstance(cycle_payload, dict) and cycle_payload.get("effective_date")
    )
    if not dated_cycles:
        raise RuntimeError("FAA NASR fallback snapshot contains no dated cycles.")
    eligible_cycles = [item for item in dated_cycles if item[0] <= target_date]
    effective_date, selected_payload = eligible_cycles[-1] if eligible_cycles else dated_cycles[0]
    source_page_url = str(
        selected_payload.get("source_page_url") or "vendored FAA NASR snapshot"
    )
    cycle = FaaCycleUrls(
        effective_date=effective_date,
        page_url=source_page_url,
        apt_url=f"vendored:{path.name}:apt",
        fix_url=f"vendored:{path.name}:fix",
        nav_url=f"vendored:{path.name}:nav",
    )

    def decode_index(category: str) -> dict[str, FaaWaypoint]:
        records = selected_payload.get("indexes", {}).get(category, {})
        if not isinstance(records, dict):
            raise RuntimeError(f"FAA NASR fallback snapshot has no valid {category} index.")
        decoded: dict[str, FaaWaypoint] = {}
        for key, values in records.items():
            if not isinstance(values, list) or len(values) != 7:
                continue
            decoded[str(key)] = FaaWaypoint(
                identifier=str(values[0]),
                latitude=float(values[1]),
                longitude=float(values[2]),
                waypoint_type=str(values[3]),
                source=f"{values[4]} (offline snapshot {effective_date.isoformat()})",
                name=str(values[5]),
                description=str(values[6]),
            )
        return decoded

    return cycle, decode_index("airport"), decode_index("nav"), decode_index("fix")


def resolve_faa_waypoint(
    identifier: str,
    *,
    reference_date: dt.date | None = None,
    session: requests.Session | object | None = None,
    fallback_snapshot_path: str = str(FAA_FALLBACK_SNAPSHOT),
) -> tuple[FaaWaypoint | None, FaaCycleUrls]:
    """Resolve a waypoint from live FAA data, falling back to the labeled dated snapshot."""

    normalized_identifier = _normalize_identifier(identifier)
    try:
        cycle_urls = get_faa_cycle_urls(reference_date=reference_date, session=session)
        airport_index, nav_index, fix_index = _load_indexes_for_cycle(cycle_urls, session=session)
    except Exception as live_error:
        try:
            cycle_urls, airport_index, nav_index, fix_index = _load_fallback_snapshot(
                fallback_snapshot_path,
                (reference_date or dt.date.today()).isoformat(),
            )
        except Exception as fallback_error:
            raise RuntimeError(
                f"Live FAA NASR lookup failed ({live_error}); fallback also failed ({fallback_error})."
            ) from fallback_error

    candidates = {
        "airport": airport_index.get(normalized_identifier),
        "navaid": nav_index.get(normalized_identifier),
        "fix": fix_index.get(normalized_identifier),
    }
    available_candidates = {name: waypoint for name, waypoint in candidates.items() if waypoint is not None}
    if not available_candidates:
        return None, cycle_urls

    # FAA identifiers can collide across airports, navaids, and fixes, so prefer the category
    # that best matches the token shape a pilot would normally type for that point type.
    preferred_order = (
        ("fix", "navaid", "airport")
        if len(normalized_identifier) >= 5
        else ("airport", "fix", "navaid")
        if len(normalized_identifier) == 4
        else ("navaid", "airport", "fix")
    )
    selected_key = next(name for name in preferred_order if available_candidates.get(name) is not None)
    selected_waypoint = available_candidates[selected_key]
    ambiguity_note = None
    if len(available_candidates) > 1:
        ambiguity_note = (
            f"Identifier also matched {', '.join(sorted(name for name in available_candidates if name != selected_key))}; "
            f"using FAA {selected_key} record."
        )

    return (
        replace(
            selected_waypoint,
            identifier=normalized_identifier,
            ambiguity_note=ambiguity_note,
        ),
        cycle_urls,
    )
