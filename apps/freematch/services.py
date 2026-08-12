"""
Free Match Wizard — service layer.

All vehicle scoring is delegated to the CORE Rating Engine
(apps.vehicles.services.rating_engine).  No cost or rating
figures are computed here — this module only filters candidates
and picks the highest-scoring one.
State mutations and complex business logic live here.
"""

import logging
import random
from typing import Optional

from django.db import transaction

from apps.freematch.models import FreeMatchSession, PriorityPreference
from apps.freematch.selectors import (
    get_freematch_candidates,
    get_freematch_candidates_base,
    get_freematch_candidates_fuel_only,
)
from apps.vehicles.models import Vehicle
from apps.vehicles.services.rating_engine import calculate_core_rating
from apps.mygarage.models import GarageVehicle, GarageVehicleSource

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────

WIZARD_CORE_MODE = "pure_cost"
WIZARD_DEFAULT_STATE = "NSW"

PRIORITY_TO_SLIDER = {
    PriorityPreference.COST_ONLY: 1,
    PriorityPreference.MOSTLY_COST: 2,
    PriorityPreference.BALANCED: 3,
    PriorityPreference.MOSTLY_GREEN: 4,
    PriorityPreference.GREEN_ONLY: 5,
}

# ── Helpers ──────────────────────────────────────────────────────────────────

def _score_and_pick_best(candidates, state: str, priority_slider: int) -> Optional[Vehicle]:
    """
    Score each candidate with the CORE Rating Engine and return
    a single highest-scoring vehicle. Break ties randomly across models.
    """
    if not candidates:
        return None

    best_score = -1.0
    best_vehicles = []
    category_metrics_cache = {}

    unique_candidates = []
    seen_models = set()
    for v in candidates:
        model_key = (v.brand, v.model, v.year)
        if model_key not in seen_models:
            seen_models.add(model_key)
            unique_candidates.append(v)

    for vehicle in unique_candidates:
        try:
            result = calculate_core_rating(
                vehicle=vehicle,
                state=state,
                priority_slider=priority_slider,
                core_mode=WIZARD_CORE_MODE,
                user_inputs={},
                current_vehicle_annual_cost=None,
                category_metrics_cache=category_metrics_cache
            )
            rating = result.get("core_rating")
            if rating is None:
                continue
            if rating > best_score:
                best_score = rating
                best_vehicles = [vehicle]
            elif rating == best_score:
                best_vehicles.append(vehicle)
        except Exception:
            logger.exception("CORE Rating Engine error for vehicle %s — skipping", vehicle.id)

    if not best_vehicles:
        return None
        
    return random.choice(best_vehicles)


# ── Match Logic ──────────────────────────────────────────────────────────────

def match_vehicle_for_wizard(session: FreeMatchSession) -> Optional[Vehicle]:
    """
    Run the Free Match Wizard matching algorithm.
    """
    state = session.state or WIZARD_DEFAULT_STATE
    priority_slider = PRIORITY_TO_SLIDER.get(session.priority, 3)

    # ── Attempt 1: all specific filters ─────────────────────────────────────
    candidates = list(get_freematch_candidates(session))

    if candidates:
        vehicle = _score_and_pick_best(candidates, state, priority_slider)
        if vehicle:
            return vehicle

    # ── Attempt 1.5: relax budget and nice-to-have features ───────────────────
    logger.info(
        "Free match wizard: no results with specific filters — relaxing budget & ANCAP (session %s)",
        session.session_token[:8],
    )
    from apps.freematch.selectors import get_freematch_candidates_relaxed
    candidates = list(get_freematch_candidates_relaxed(session))

    if candidates:
        vehicle = _score_and_pick_best(candidates, state, priority_slider)
        if vehicle:
            return vehicle

    # ── Attempt 2: fuel only (relax body style, seating, etc) ──────────────────────────
    logger.info(
        "Free match wizard: no results with relaxed filters — dropping to fuel only (session %s)",
        session.session_token[:8],
    )
    candidates = list(get_freematch_candidates_fuel_only(session.fuel_type_preference))

    if candidates:
        vehicle = _score_and_pick_best(candidates, state, priority_slider)
        if vehicle:
            return vehicle

    # ── Attempt 3: no filters (relax fuel type too) ──────────────────────
    logger.info(
        "Free match wizard: no results with fuel filter — relaxing all (session %s)",
        session.session_token[:8],
    )
    candidates = list(get_freematch_candidates_base())

    if not candidates:
        raise RuntimeError("Vehicle catalogue is empty — cannot produce a match.")

    vehicle = _score_and_pick_best(candidates, state, priority_slider)
    if not vehicle:
        raise RuntimeError("CORE Rating Engine returned no scorable vehicle.")

    return vehicle


# ── Response Builders ────────────────────────────────────────────────────────

def build_match_response(vehicle: Vehicle, session: FreeMatchSession) -> dict:
    from django.utils import timezone
    
    state = session.state or WIZARD_DEFAULT_STATE
    priority_slider = PRIORITY_TO_SLIDER.get(session.priority, 3)

    engine_data = calculate_core_rating(
        vehicle=vehicle,
        state=state,
        priority_slider=priority_slider,
        core_mode=WIZARD_CORE_MODE,
        user_inputs={},
        current_vehicle_annual_cost=None,
    )

    annual_cost_cents = engine_data.get("total_annual_cost") or 0
    co2 = vehicle.tailpipe_co2_g_per_km

    return {
        "vehicle": {
            "id": str(vehicle.id),
            "make": vehicle.brand,
            "model": vehicle.model,
            "year": vehicle.year,
            "variant": vehicle.variant,
            "powertrain_type": vehicle.powertrain_type,
            "core_rating": engine_data.get("core_rating"),
            "band_name": engine_data.get("band_name"),
            "annual_running_cost_estimate": annual_cost_cents,
            "annual_running_cost_display": _format_cents(annual_cost_cents),
            "co2_g_per_km": co2,
            "is_estimate": True,
            "resale_value_5yr_estimate": {"locked": True, "value": None},
            "est_cost_difference": {"locked": True, "value": None},
            "driveaway_price_estimate": {"locked": True, "value": None},
        },
        "session_token": session.session_token,
        "meta": {
            "calculated_at": timezone.now().isoformat(),
            "is_estimate": True,
            "disclaimer": (
                "All figures are estimates only. Actual costs will vary based on "
                "individual driving habits, insurer quotes, dealer pricing, and "
                "personal circumstances."
            ),
        },
    }

def build_unlocked_response(vehicle: Vehicle, user, garage_vehicle) -> dict:
    from django.utils import timezone

    cv = getattr(user, "current_vehicle", None)
    state = (cv.state if cv and cv.state else None) or WIZARD_DEFAULT_STATE

    try:
        priority_state = user.priority_state
        priority_slider = priority_state.priority_slider_position
        core_mode = priority_state.active_core_mode
    except Exception:
        priority_slider = PRIORITY_TO_SLIDER[PriorityPreference.BALANCED]
        core_mode = WIZARD_CORE_MODE

    engine_data = calculate_core_rating(
        vehicle=vehicle,
        state=state,
        priority_slider=priority_slider,
        core_mode=core_mode,
        user_inputs={},
        current_vehicle_annual_cost=None,
    )

    annual_cost_cents = engine_data.get("total_annual_cost") or 0
    driveaway_cents = vehicle.driveaway_price_by_state.get(state) or 0
    resale_cents = vehicle.resale_value_5yr_estimate or 0
    diff_obj = engine_data.get("est_cost_difference")
    if isinstance(diff_obj, dict):
        est_diff_cents = diff_obj.get("value_cents", 0)
        if diff_obj.get("direction") == "more_expensive":
            est_diff_cents = -est_diff_cents
    else:
        est_diff_cents = diff_obj or 0

    return {
        "garage_vehicle_id": str(garage_vehicle.id),
        "vehicle": {
            "id": str(vehicle.id),
            "make": vehicle.brand,
            "model": vehicle.model,
            "year": vehicle.year,
            "variant": vehicle.variant,
            "powertrain_type": vehicle.powertrain_type,
            "core_rating": engine_data.get("core_rating"),
            "band_name": engine_data.get("band_name"),
            "band_slug": engine_data.get("band_slug"),
            "annual_running_cost_estimate": annual_cost_cents,
            "annual_running_cost_display": _format_cents(annual_cost_cents),
            "co2_g_per_km": vehicle.tailpipe_co2_g_per_km,
            "is_estimate": True,
            "resale_value_5yr_estimate": resale_cents,
            "resale_value_5yr_display": _format_cents(resale_cents),
            "est_cost_difference": est_diff_cents,
            "est_cost_difference_display": _format_cents(est_diff_cents),
            "driveaway_price_estimate": driveaway_cents,
            "driveaway_price_display": _format_cents(driveaway_cents),
        },
        "meta": {
            "calculated_at": timezone.now().isoformat(),
            "is_estimate": True,
            "disclaimer": (
                "All figures are estimates only. Actual costs will vary based on "
                "individual driving habits, insurer quotes, dealer pricing, and "
                "personal circumstances."
            ),
        },
    }

def _format_cents(cents: int) -> str:
    if cents is None:
        return "$0"
    dollars = cents / 100
    sign = "-" if dollars < 0 else ""
    return f"{sign}${abs(dollars):,.0f}"

# ── State Mutations ──────────────────────────────────────────────────────────

def process_free_match(payload: dict) -> dict:
    """
    Creates a new FreeMatchSession with the payload, calculates the matched vehicle,
    associates it with the session, and returns the response payload including
    the session_token and the vehicle match data.
    """
    session = FreeMatchSession.objects.create(
        priority=payload.get("priority", PriorityPreference.BALANCED),
        primary_use=payload.get("primary_use"),
        annual_km=payload.get("annual_km"),
        budget_min=payload.get("budget_min"),
        budget_max=payload.get("budget_max"),
        state=payload.get("state"),
        has_current_vehicle=payload.get("has_current_vehicle", False),
    )
    
    cv_data = payload.get("current_vehicle")
    if cv_data:
        session.current_vehicle_make = cv_data.get("make")
        session.current_vehicle_model = cv_data.get("model")
        session.current_vehicle_year = cv_data.get("year")
        session.current_vehicle_odometer = cv_data.get("odometer")
        
    sn_data = payload.get("specific_needs")
    if sn_data:
        session.fuel_type_preference = sn_data.get("fuel_type")
        session.style_preference = sn_data.get("style")
        session.seating_preference = sn_data.get("seating")
        session.ancap_5_star_only = sn_data.get("ancap_5_star_only", False)
        session.towing_capacity_required = sn_data.get("towing_capacity_required", False)
        
    session.save()
    
    vehicle = match_vehicle_for_wizard(session)
    if not vehicle:
        raise RuntimeError("No match could be found.")
        
    session.matched_vehicle = vehicle
    session.save(update_fields=["matched_vehicle"])
    
    return build_match_response(vehicle, session)


@transaction.atomic
def convert_session_to_garage(session: FreeMatchSession, user) -> GarageVehicle:
    garage_vehicle = GarageVehicle.objects.create(
        user=user,
        vehicle_catalogue=session.matched_vehicle,
        source=GarageVehicleSource.FREE_MATCH_WIZARD,
    )
    session.converted_user = user
    session.save(update_fields=["converted_user"])
    return garage_vehicle

def discard_freematch_session(session: FreeMatchSession):
    session.delete()
