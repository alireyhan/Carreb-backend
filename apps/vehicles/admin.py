from django.contrib import admin

from apps.vehicles.models import Vehicle


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = [
        "brand",
        "model",
        "year",
        "variant",
        "powertrain_type",
        "body_style",
        "fcai_category",
        "num_seats",
        "ancap_safety_rating",
        "tailpipe_co2_g_per_km",
        "updated_at",
    ]
    list_filter = [
        "powertrain_type",
        "body_style",
        "year",
        "brand",
        "fcai_category",
        "ancap_safety_rating",
        "test_cycle",
        "is_scoring_outlier",
    ]
    search_fields = ["brand", "model", "variant", "fcai_category"]
    ordering = ["brand", "model", "year", "variant"]
    readonly_fields = ["id", "created_at", "updated_at"]
    date_hierarchy = "created_at"

    fieldsets = (
        ("Identity", {
            "fields": (
                "id",
                "brand", "model", "year", "variant",
                "body_style", "num_seats",
                "powertrain_type", "fcai_category", "fcai_broad",
                "ancap_safety_rating", "is_scoring_outlier", "scoring_version",
            ),
        }),
        ("Emissions, Efficiency & Specs", {
            "fields": (
                "engine_l", "battery_kwh", "drive", "energy_norm",
                "fuel_consumption_per_100km",
                "electricity_consumption_per_100km",
                "tailpipe_co2_g_per_km",
                "wltp_range_km",
                "test_cycle",
                "annual_km_default",
            ),
        }),
        ("Pricing by State (cents)", {
            "fields": (
                "driveaway_price_by_state",
                "total_5yr_cost_of_ownership_by_state",
            ),
        }),
        ("Running Costs by State (cents)", {
            "classes": ("collapse",),
            "fields": (
                "insurance_estimate_by_state",
                "ctp_by_state",
                "registration_by_state",
                "fuel_cost_by_state",
                "electricity_cost_by_state",
                "ev_levy_by_state",
                "base_coo_by_state",
                "components_sum_by_state",
            ),
        }),
        ("Advanced Mode Data by State", {
            "classes": ("collapse",),
            "fields": (
                "mode_data_by_state",
            ),
        }),
        ("5-Year Cost Estimates (cents)", {
            "fields": (
                "servicing_5yr_estimate",
                "tyre_replacement_estimate",
                "resale_value_5yr_estimate",
            ),
        }),
        ("Provenance", {
            "classes": ("collapse",),
            "fields": ("provenance",),
        }),
        ("Timestamps", {
            "fields": ("created_at", "updated_at"),
        }),
    )
