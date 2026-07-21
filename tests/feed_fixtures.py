"""Recorded live AWC payloads served through a deterministic fake session.

The files under tests/fixtures/ are unedited responses captured from
aviationweather.gov on 2026-07-21Z for the KSTS/KBFL/KFFZ station set
(sfo + slc FD regions). They encode the feed's real shape — field names,
altitude units, severity vocabularies — so parser changes that disagree
with the wire format fail here instead of silently dropping hazards.

Recapture (from the repo root, any station set the tests rely on):
    python -c "see tests/fixtures/README.md"
"""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"

#: Stations the METAR/TAF fixtures were captured for; deterministic app tests
#: must plan missions over exactly these airports.
FIXTURE_AIRPORTS = ("KSTS", "KBFL", "KFFZ")


def load_fixture_json(name: str) -> object:
    """Load one captured JSON payload exactly as the API returned it."""

    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def load_fixture_text(name: str) -> str:
    """Load one captured text product exactly as the API returned it."""

    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


class FixtureResponse:
    """Minimal requests.Response stand-in over a recorded payload."""

    def __init__(self, *, json_payload: object = None, text_payload: str = "", status_code: int = 200):
        self._json_payload = json_payload
        self.text = text_payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> object:
        if self._json_payload is None:
            raise ValueError("missing json payload")
        return self._json_payload


class FixtureFeedSession:
    """Serve every fetch_noaa_weather endpoint from the recorded fixtures.

    Dispatch is by endpoint path, so the session answers any station list or
    forecast cycle with the same recorded weather — identical inputs on every
    call is exactly the determinism the offline app tests need.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object] | None]] = []

    def get(self, url: str, *, params=None, timeout=None, headers=None) -> FixtureResponse:
        self.calls.append((url, dict(params) if params else None))
        endpoint = url.rstrip("/").rsplit("/", 1)[-1]
        if endpoint == "windtemp":
            region = str((params or {}).get("region", "sfo"))
            candidate = FIXTURES_DIR / f"windtemp_{region}.txt"
            name = candidate.name if candidate.exists() else "windtemp_sfo.txt"
            return FixtureResponse(text_payload=load_fixture_text(name))
        if endpoint in {"metar", "taf", "gairmet", "airsigmet", "pirep"}:
            return FixtureResponse(json_payload=load_fixture_json(f"{endpoint}.json"))
        if endpoint in {"tcf", "cwa"}:
            return FixtureResponse(json_payload=load_fixture_json(f"{endpoint}.json"))
        raise RuntimeError(f"no fixture recorded for endpoint: {url}")
