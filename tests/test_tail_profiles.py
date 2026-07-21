"""Regression coverage for tail-specific loading and profile portability."""

import pytest

from tail_profiles import (
    TailProfile,
    compute_planning_weights,
    deserialize_tail_profile,
    gallons_from_pounds,
    serialize_tail_profile,
)


def test_compute_planning_weights_uses_takeoff_and_mid_cruise_fuel():
    profile = TailProfile(tail_number="N960TB", basic_operating_weight_lb=4700, payload_lb=500)

    weights = compute_planning_weights(
        profile,
        fuel_load_gal=292,
        startup_taxi_fuel_gal=8,
        landing_fuel_gal=60,
    )

    assert weights.takeoff_weight_lb == pytest.approx(7156.4)
    assert weights.climb_weight_lb == weights.takeoff_weight_lb
    assert weights.cruise_weight_lb == pytest.approx(6352.4)


def test_tail_profile_json_round_trip_is_versioned():
    profile = TailProfile("N960TB", 4700, 500, 4.5, 3.0)

    assert deserialize_tail_profile(serialize_tail_profile(profile)) == profile


def test_tail_profile_rejects_unknown_schema():
    with pytest.raises(ValueError, match="Unsupported"):
        deserialize_tail_profile('{"schema_version": 2, "profile": {}}')


def test_gallons_from_pounds_uses_planning_density():
    """Verify the shared lb-to-gal conversion matches the Jet-A planning density."""

    assert gallons_from_pounds(670.0) == pytest.approx(100.0)
