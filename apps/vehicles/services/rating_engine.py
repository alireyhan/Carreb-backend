from typing import Dict, Any, Optional
from decimal import Decimal
import math
import statistics
from django.utils import timezone
from apps.vehicles.models import Vehicle, PowertrainType
from apps.vehicles.selectors import get_vehicles_by_fcai_category

# Trademarked Band Names
BAND_NAMES = {
    (4.5, 5.0): "Net Zero Ninja™",
    (3.5, 4.4): "Smart Move™",
    (2.5, 3.4): "Steady Cruiser™",
    (1.5, 2.4): "The Detour™",
    (1.0, 1.4): "Captain Carbon™",
}

def get_band_name(rating: float) -> str:
    for (min_val, max_val), name in BAND_NAMES.items():
        if min_val <= rating <= max_val:
            return name
    return "Captain Carbon™" # Fallback

def get_band_slug(rating: float) -> str:
    if rating >= 4.5: return "net-zero-ninja"
    if rating >= 3.5: return "smart-move"
    if rating >= 2.5: return "steady-cruiser"
    if rating >= 1.5: return "the-detour"
    return "captain-carbon"


# ---------------------------------------------------------------------------
# CORE rating — SPEC §4/E2 FIXED-BAND method.
#
# Percentile / quintile mapping is PROHIBITED (Dev Spec §4: "Fixed-band thresholds are
# permanent — percentile/quintile mapping is PROHIBITED for regulatory defensibility").
# The pre-launch batch stores the authoritative price_score / emissions_score / core_score /
# stars per (state, CORE Mode) in Vehicle.mode_data_by_state. The live engine REUSES those
# scores and only re-weights them for the priority slider — it never re-scores against peers.
# ---------------------------------------------------------------------------

CORE_MODE_TO_KEY = {
    'pure_cost': 'B',     # B = Pure Cost
    'cool_choice': 'E',   # E = COOL Choice (B + finance)
    'true_value': 'C',    # C = True Value (B - resale)
    'full_picture': 'F',  # F = Full Picture (E - resale)
}


def get_stored_mode_scores(vehicle: Vehicle, state: str, core_mode: str):
    """Return (price_score, emissions_score) from the authoritative batch, or None if absent."""
    state_data = (vehicle.mode_data_by_state or {}).get(state) or {}
    md = state_data.get(CORE_MODE_TO_KEY.get(core_mode, 'B'))
    if not md:  # tolerate alternate spellings of the 'CORE Mode' column in the source sheet
        for alt in (core_mode.upper(), core_mode.replace('_', ' ').upper()):
            if state_data.get(alt):
                md = state_data[alt]
                break
    if not md:
        return None
    ps, es = md.get('price_score'), md.get('emissions_score')
    if ps is None or es is None:
        return None
    return float(ps), float(es)


def _co2_star_cap(powertrain, tailpipe_co2) -> Optional[float]:
    """Absolute CO2 star-cap (Scheme B). BEV/FCEV are never capped."""
    if powertrain in (PowertrainType.BEV, PowertrainType.FCEV):
        return None
    co2 = tailpipe_co2 or 0
    if co2 >= 250:
        return 2.4
    if co2 >= 200:
        return 3.4
    if co2 >= 150:
        return 4.4
    return None


def core_rating_from_scores(price_score, emissions_score, powertrain, tailpipe_co2,
                            coo_weight, co2_weight):
    """Spec §4/E2 fixed-band rating from stored scores.

    Returns (rating_capped, uncapped_rating, cap) where each rating is a one-decimal 0.0-5.0 value.
    """
    # EV / Hydrogen: emissions_score is hard-set to 1.0 (spec E2).
    es = 1.0 if powertrain in (PowertrainType.BEV, PowertrainType.FCEV) else emissions_score
    ps = max(0.0, min(1.0, price_score))
    es = max(0.0, min(1.0, es))
    core_score = coo_weight * ps + co2_weight * es          # 0.0-1.0
    rating = round(core_score * 5.0, 1)                     # 0.0-5.0, one decimal
    cap = _co2_star_cap(powertrain, tailpipe_co2)
    rating_capped = min(rating, cap) if cap is not None else rating
    rating_capped = max(0.0, min(5.0, rating_capped))
    return rating_capped, rating, cap


def calculate_monthly_repayment(principal: float, annual_rate: float, months: int) -> float:
    if annual_rate == 0:
        return principal / months
    monthly_rate = (annual_rate / 100.0) / 12
    repayment = principal * (monthly_rate * math.pow(1 + monthly_rate, months)) / (math.pow(1 + monthly_rate, months) - 1)
    return repayment

def _get_state_value(json_field: dict, state: str, default_val=0) -> int:
    if not json_field: return default_val
    return json_field.get(state, default_val)

def calculate_vehicle_cost_breakdown(
    vehicle: Vehicle,
    state: str,
    core_mode: str,
    user_inputs: Dict[str, Any]
) -> Dict[str, Any]:

    annual_km = user_inputs.get('annual_km', vehicle.annual_km_default or 13300)

    driveaway = user_inputs.get('driveaway_override')
    if not driveaway:
        driveaway = _get_state_value(vehicle.driveaway_price_by_state, state)

    # 1. Fuel / Energy
    # Simplified calculation: (consumption / 100) * annual_km * cost_per_unit
    # Assuming $2.00 per L for fuel and $0.30 per kWh for electricity for this simplified logic
    fuel_annual = 0
    if vehicle.fuel_consumption_per_100km:
        fuel_annual = (vehicle.fuel_consumption_per_100km / 100) * annual_km * 200 # cents
    if vehicle.electricity_consumption_per_100km:
        fuel_annual += (vehicle.electricity_consumption_per_100km / 100) * annual_km * 30 # cents

    fuel_annual = int(fuel_annual)

    # 2. Insurance
    insurance = user_inputs.get('insurance_override')
    if not insurance:
        insurance = _get_state_value(vehicle.insurance_estimate_by_state, state)

    ctp = _get_state_value(vehicle.ctp_by_state, state)
    rego = _get_state_value(vehicle.registration_by_state, state)

    servicing_annual = int((vehicle.servicing_5yr_estimate or 0) / 5)
    tyres_annual = int((vehicle.tyre_replacement_estimate or 0) / 5)

    if not insurance and not ctp and not rego and not servicing_annual and not tyres_annual:
        total_5yr = _get_state_value(vehicle.total_5yr_cost_of_ownership_by_state, state)
        if total_5yr:
            annual_aggregate = int(total_5yr / 5)
            non_fuel_annual = max(0, annual_aggregate - fuel_annual)
            servicing_annual = non_fuel_annual

    finance_annual = 0
    if core_mode in ['cool_choice', 'full_picture']:
        loan_amount = user_inputs.get('loan_amount', driveaway * 0.8) # 20% deposit
        interest_rate = user_inputs.get('interest_rate', 7.5)
        loan_term_months = user_inputs.get('loan_term_months', 60)
        monthly = calculate_monthly_repayment(loan_amount / 100.0, interest_rate, loan_term_months)
        finance_annual = int(monthly * 12 * 100)

    resale_annual = 0
    if core_mode in ['true_value', 'full_picture']:
        resale_annual = int((vehicle.resale_value_5yr_estimate or 0) / 5)

    total = fuel_annual + insurance + ctp + rego + servicing_annual + tyres_annual
    if core_mode in ['cool_choice', 'full_picture']:
        total += finance_annual
    if core_mode in ['true_value', 'full_picture']:
        total -= resale_annual

    return {
        "fuel_or_energy_annual": fuel_annual,
        "insurance_annual": insurance,
        "ctp_annual": ctp,
        "registration_annual": rego,
        "servicing_annual": servicing_annual,
        "tyres_annual": tyres_annual,
        "finance_annual": finance_annual if core_mode in ['cool_choice', 'full_picture'] else 0,
        "resale_credit_annual": resale_annual if core_mode in ['true_value', 'full_picture'] else 0,
        "total_annual": total,
        "driveaway_price": driveaway
    }


def calculate_core_rating(
    vehicle: Vehicle,
    state: str,
    priority_slider: int,
    core_mode: str,
    user_inputs: Dict[str, Any],
    budget_min: Optional[int] = None,
    budget_max: Optional[int] = None,
    current_vehicle_annual_cost: Optional[int] = None,
    category_metrics_cache: Optional[Dict[str, Dict[str, list]]] = None
) -> dict:

    if vehicle.powertrain_type == PowertrainType.FCEV:
        return {
            "core_rating": None,
            "message": "CORE Rating not available for hydrogen vehicles at launch.",
            "is_estimate": True
        }

    # Weights (priority slider re-weights the STORED price/emissions scores; stored baseline is 80/20)
    weights = {
        1: (1.0, 0.0),
        2: (0.75, 0.25),
        3: (0.50, 0.50),
        4: (0.25, 0.75),
        5: (0.0, 1.0)
    }
    coo_weight, co2_weight = weights.get(priority_slider, (0.5, 0.5))

    breakdown = calculate_vehicle_cost_breakdown(vehicle, state, core_mode, user_inputs)
    total_cost = breakdown['total_annual']

    fcai_category = vehicle.fcai_category

    if category_metrics_cache is not None and fcai_category in category_metrics_cache:
        peer_costs = category_metrics_cache[fcai_category]["peer_costs"]
        peer_co2 = category_metrics_cache[fcai_category]["peer_co2"]
        peer_powertrains = category_metrics_cache[fcai_category]["peer_powertrains"]
        peer_breakdowns = category_metrics_cache[fcai_category]["peer_breakdowns"]
        peer_scores = category_metrics_cache[fcai_category]["peer_scores"]
    else:
        peers = get_vehicles_by_fcai_category(fcai_category)

        peer_costs = []
        peer_co2 = []
        peer_powertrains = []
        peer_breakdowns = []
        peer_scores = []

        for peer in peers:
            peer_br = calculate_vehicle_cost_breakdown(peer, state, core_mode, user_inputs)
            peer_costs.append(peer_br['total_annual'])
            peer_co2.append(peer.tailpipe_co2_g_per_km or 0)
            peer_powertrains.append(peer.powertrain_type)
            peer_breakdowns.append(peer_br)
            peer_scores.append(get_stored_mode_scores(peer, state, core_mode))

        if category_metrics_cache is not None:
            category_metrics_cache[fcai_category] = {
                "peer_costs": peer_costs,
                "peer_co2": peer_co2,
                "peer_powertrains": peer_powertrains,
                "peer_breakdowns": peer_breakdowns,
                "peer_scores": peer_scores
            }

    # SPEC §4/E2 FIXED-BAND method (percentile mapping is PROHIBITED). Reuse the batch-stored
    # price_score / emissions_score; the slider only re-weights them, it never re-scores.
    main_scores = get_stored_mode_scores(vehicle, state, core_mode)
    if main_scores is None:
        return {
            "core_rating": None,
            "message": "CORE Rating unavailable - no scored batch data for this vehicle/state/mode.",
            "is_estimate": True
        }
    price_score, emissions_score = main_scores
    blended_score, uncapped_rating, applied_cap = core_rating_from_scores(
        price_score, emissions_score, vehicle.powertrain_type,
        vehicle.tailpipe_co2_g_per_km, coo_weight, co2_weight
    )
    co2_cap_applied = applied_cap is not None and blended_score < uncapped_rating
    co2_cap_threshold = applied_cap if co2_cap_applied else None

    # Est cost difference
    def _build_est_cost_diff_obj(baseline: int, total: int, is_current_vehicle: bool) -> dict:
        diff = baseline - total
        comp_mode = "current_vehicle" if is_current_vehicle else "category_avg"
        if is_current_vehicle:
            base_disp = f"Est. ${int(round(baseline / 100)):,}/year"
        else:
            base_disp = f"Est. ${int(round(baseline / 100)):,}/year\n      ({fcai_category} category average)"

        if diff < 0:
            direction = "more_expensive"
            val = abs(diff)
            bucket = None
        elif diff > 0:
            direction = "saving"
            val = diff
            s5 = val * 5
            if s5 < 250000: bucket = "essentials_covered"
            elif s5 < 750000: bucket = "home_family"
            elif s5 < 1250000: bucket = "savings_security"
            else: bucket = "future_proof"
        else:
            direction = "same"
            val = 0
            bucket = None

        s5 = val * 5
        return {
            "value_cents": val,
            "value_display": f"Est. ${int(round(val / 100)):,}/year",
            "direction": direction,
            "comparison_mode": comp_mode,
            "baseline_cost": baseline,
            "baseline_cost_display": base_disp,
            "est_5yr_savings": s5,
            "est_5yr_savings_display": f"Est. ${int(round(s5 / 100)):,} over 5 years",
            "savings_bucket": bucket
        }

    if current_vehicle_annual_cost is not None:
        est_cost_difference = _build_est_cost_diff_obj(current_vehicle_annual_cost, total_cost, True)
    else:
        avg_class_cost = sum(peer_costs) / len(peer_costs) if peer_costs else total_cost
        est_cost_difference = _build_est_cost_diff_obj(int(avg_class_cost), total_cost, False)

    # Calculate peer distribution and medians
    peer_band_stats = {
        "net-zero-ninja": {"count": 0, "breakdowns": [], "co2": []},
        "smart-move": {"count": 0, "breakdowns": [], "co2": []},
        "steady-cruiser": {"count": 0, "breakdowns": [], "co2": []},
        "the-detour": {"count": 0, "breakdowns": [], "co2": []},
        "captain-carbon": {"count": 0, "breakdowns": [], "co2": []}
    }

    annual_km = user_inputs.get('annual_km', vehicle.annual_km_default or 13300)

    for p_cost, p_co2, p_pt, p_br, p_scores in zip(peer_costs, peer_co2, peer_powertrains, peer_breakdowns, peer_scores):
        if p_pt == PowertrainType.FCEV:
            continue

        # Budget filtering check
        driveaway = p_br.get('driveaway_price', 0)
        if budget_min is not None and driveaway < budget_min:
            continue
        if budget_max is not None and driveaway > budget_max:
            continue

        # Same FIXED-BAND method as the subject vehicle (no percentile) for a consistent dial.
        if p_scores is None:
            continue
        p_blended, _p_uncapped, _p_cap = core_rating_from_scores(
            p_scores[0], p_scores[1], p_pt, p_co2, coo_weight, co2_weight
        )
        band_slug = get_band_slug(p_blended)

        peer_band_stats[band_slug]["count"] += 1
        peer_band_stats[band_slug]["breakdowns"].append(p_br)
        peer_band_stats[band_slug]["co2"].append(p_co2)

    # Calculate medians for the response
    peer_distribution = {}
    peer_band_medians = {}

    def get_median_or_zero(data: list) -> int:
        if not data: return 0
        return int(statistics.median(data))

    for slug, stats in peer_band_stats.items():
        peer_distribution[slug] = stats["count"]
        if stats["count"] > 0:
            avg_class_cost = sum(peer_costs) / len(peer_costs) if peer_costs else total_cost
            med_total = get_median_or_zero([b["total_annual"] for b in stats["breakdowns"]])
            if current_vehicle_annual_cost is not None:
                med_est_diff = _build_est_cost_diff_obj(current_vehicle_annual_cost, med_total, True)
            else:
                med_est_diff = _build_est_cost_diff_obj(int(avg_class_cost), med_total, False)

            med_tailpipe = get_median_or_zero(stats["co2"])
            med_annual_co2_kg = int((med_tailpipe * annual_km) / 1000)

            peer_band_medians[slug] = {
                "total_annual_cost": med_total,
                "annual_co2_kg": med_annual_co2_kg,
                "est_cost_difference": med_est_diff,
                "annual_cost_breakdown": {
                    "fuel_or_energy_annual": get_median_or_zero([b["fuel_or_energy_annual"] for b in stats["breakdowns"]]),
                    "insurance_annual": get_median_or_zero([b["insurance_annual"] for b in stats["breakdowns"]]),
                    "ctp_annual": get_median_or_zero([b["ctp_annual"] for b in stats["breakdowns"]]),
                    "registration_annual": get_median_or_zero([b["registration_annual"] for b in stats["breakdowns"]]),
                    "servicing_annual": get_median_or_zero([b["servicing_annual"] for b in stats["breakdowns"]]),
                    "tyres_annual": get_median_or_zero([b["tyres_annual"] for b in stats["breakdowns"]]),
                    "finance_annual": get_median_or_zero([b["finance_annual"] for b in stats["breakdowns"]]),
                    "resale_credit_annual": get_median_or_zero([b["resale_credit_annual"] for b in stats["breakdowns"]]),
                }
            }
        else:
            peer_band_medians[slug] = None

    annual_km = user_inputs.get('annual_km', vehicle.annual_km_default or 13300)
    tailpipe_co2 = vehicle.tailpipe_co2_g_per_km or 0
    annual_co2_kg = int((tailpipe_co2 * annual_km) / 1000)

    # emissions_score exposed to the client already reflects the EV/Hydrogen hard-set to 1.0.
    exposed_emissions_score = 1.0 if vehicle.powertrain_type in (PowertrainType.BEV, PowertrainType.FCEV) else emissions_score

    return {
        "core_rating": float(blended_score),
        # Exposed so the client can re-apply the priority slider without a round-trip (spec C4).
        "price_score": round(float(price_score), 4),
        "emissions_score": round(float(exposed_emissions_score), 4),
        "co2_cap_tier": float(co2_cap_threshold) if co2_cap_threshold else None,
        "band_name": get_band_name(blended_score),
        "band_slug": get_band_slug(blended_score),
        "co2_cap_applied": co2_cap_applied,
        "co2_cap_threshold": float(co2_cap_threshold) if co2_cap_threshold else None,
        "annual_cost_breakdown": breakdown,
        "total_annual_cost": total_cost,
        "est_cost_difference": est_cost_difference,
        "tailpipe_co2_g_per_km": float(tailpipe_co2),
        "annual_co2_kg": annual_co2_kg,
        "peer_band_distribution": peer_distribution,
        "peer_band_medians": peer_band_medians,
        "is_estimate": True
    }
