"""Tail-specific loading and calibration helpers for the Streamlit workspace."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

JET_A_POUNDS_PER_GALLON = 6.7
# TBM 960 usable fuel when full — confirmed by the owner 2026-07-20. Re-verify
# against the PIM before trusting for a different tail.
MAX_USABLE_FUEL_GAL = 292.0


def gallons_from_pounds(pounds: float) -> float:
    """Convert a Jet-A quantity from pounds to gallons at the planning density."""

    return float(pounds) / JET_A_POUNDS_PER_GALLON


@dataclass(frozen=True)
class TailProfile:
    """Pilot-entered loading and observed-performance settings for one aircraft."""

    tail_number: str = ""
    basic_operating_weight_lb: float = 4700.0
    payload_lb: float = 500.0
    time_calibration_pct: float = 0.0
    fuel_calibration_pct: float = 0.0


@dataclass(frozen=True)
class PlanningWeights:
    """Computed takeoff, climb, and representative mid-cruise weights."""

    takeoff_weight_lb: float
    climb_weight_lb: float
    cruise_weight_lb: float


def compute_planning_weights(
    profile: TailProfile,
    *,
    fuel_load_gal: float,
    startup_taxi_fuel_gal: float,
    landing_fuel_gal: float,
) -> PlanningWeights:
    """Compute source weights from loading and a representative mission fuel curve.

    Climb begins at takeoff weight. Cruise uses the midpoint between post-taxi
    weight and planned landing weight, which is a stable, auditable estimate
    before the wind-dependent mission solution is available.
    """

    bounded_fuel = max(float(fuel_load_gal), 0.0)
    takeoff_weight = (
        max(float(profile.basic_operating_weight_lb), 0.0)
        + max(float(profile.payload_lb), 0.0)
        + (bounded_fuel * JET_A_POUNDS_PER_GALLON)
    )
    post_taxi_fuel = max(bounded_fuel - max(float(startup_taxi_fuel_gal), 0.0), 0.0)
    landing_fuel = min(max(float(landing_fuel_gal), 0.0), post_taxi_fuel)
    representative_cruise_fuel = (post_taxi_fuel + landing_fuel) / 2.0
    cruise_weight = (
        max(float(profile.basic_operating_weight_lb), 0.0)
        + max(float(profile.payload_lb), 0.0)
        + (representative_cruise_fuel * JET_A_POUNDS_PER_GALLON)
    )
    return PlanningWeights(takeoff_weight, takeoff_weight, cruise_weight)


def serialize_tail_profile(profile: TailProfile) -> str:
    """Export a tail profile as portable JSON for durable browser-side storage."""

    return json.dumps({"schema_version": 1, "profile": asdict(profile)}, indent=2, sort_keys=True)


def deserialize_tail_profile(raw_json: str) -> TailProfile:
    """Validate and load a versioned tail-profile JSON export."""

    payload = json.loads(raw_json)
    if payload.get("schema_version") != 1 or not isinstance(payload.get("profile"), dict):
        raise ValueError("Unsupported tail-profile file")
    values = payload["profile"]
    return TailProfile(
        tail_number=str(values.get("tail_number", "")).strip().upper(),
        basic_operating_weight_lb=float(values["basic_operating_weight_lb"]),
        payload_lb=float(values["payload_lb"]),
        time_calibration_pct=float(values.get("time_calibration_pct", 0.0)),
        fuel_calibration_pct=float(values.get("fuel_calibration_pct", 0.0)),
    )
