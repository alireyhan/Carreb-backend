import uuid
from django.db import models
from django.contrib.postgres.indexes import GinIndex
from django.utils.translation import gettext_lazy as _

class PowertrainType(models.TextChoices):
    PETROL = 'petrol', 'Petrol'
    DIESEL = 'diesel', 'Diesel'
    MILD_HYBRID = 'mild_hybrid', 'Mild Hybrid'
    HYBRID = 'hybrid', 'Hybrid'
    PHEV = 'phev', 'Plug-in Hybrid (PHEV)'
    BEV = 'bev', 'Battery Electric (BEV)'
    FCEV = 'fcev', 'Hydrogen / FCEV'

class TestCycle(models.TextChoices):
    WLTP = 'WLTP', 'WLTP'
    ADR = 'ADR', 'ADR'
    NEDC = 'NEDC', 'NEDC'

class Vehicle(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    
    brand = models.CharField(max_length=255)
    model = models.CharField(max_length=255)
    year = models.IntegerField()
    variant = models.CharField(max_length=255)
    body_style = models.CharField(max_length=100)
    image_url = models.URLField(max_length=2000, blank=True, null=True)
    num_seats = models.IntegerField(default=5)
    powertrain_type = models.CharField(max_length=50, choices=PowertrainType.choices)
    fcai_category = models.CharField(max_length=100)
    fcai_broad = models.CharField(max_length=100, blank=True)
    energy_norm = models.CharField(max_length=50, blank=True)
    drive = models.CharField(max_length=50, blank=True)
    engine_l = models.FloatField(null=True, blank=True)
    battery_kwh = models.FloatField(null=True, blank=True)
    
    ancap_safety_rating = models.IntegerField(null=True, blank=True)
    driveaway_price_by_state = models.JSONField(default=dict)
    
    fuel_consumption_per_100km = models.FloatField(null=True, blank=True)
    electricity_consumption_per_100km = models.FloatField(null=True, blank=True)
    tailpipe_co2_g_per_km = models.FloatField(null=True, blank=True)
    wltp_range_km = models.FloatField(null=True, blank=True)
    test_cycle = models.CharField(max_length=20, choices=TestCycle.choices)
    
    annual_km_default = models.IntegerField(default=13300)
    insurance_estimate_by_state = models.JSONField(default=dict)
    ctp_by_state = models.JSONField(default=dict)
    registration_by_state = models.JSONField(default=dict)
    servicing_5yr_estimate = models.IntegerField(null=True, blank=True)
    tyre_replacement_estimate = models.IntegerField(null=True, blank=True)
    resale_value_5yr_estimate = models.IntegerField(null=True, blank=True)
    total_5yr_cost_of_ownership_by_state = models.JSONField(default=dict)
    
    fuel_cost_by_state = models.JSONField(default=dict)
    electricity_cost_by_state = models.JSONField(default=dict)
    ev_levy_by_state = models.JSONField(default=dict)
    base_coo_by_state = models.JSONField(default=dict)
    components_sum_by_state = models.JSONField(default=dict)
    
    mode_data_by_state = models.JSONField(default=dict)
    is_scoring_outlier = models.BooleanField(default=False)
    scoring_version = models.CharField(max_length=50, blank=True)
    
    provenance = models.JSONField(default=dict) 
    # Example:
    # {
    #   "brand": {"source_category": "Manufacturer", "retrieved_at": "2026-01-01T00:00:00Z"},
    #   "fuel_consumption_per_100km": {"source_category": "GVG", "retrieved_at": "2026-01-01T00:00:00Z"}
    # }

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['brand', 'model', 'year', 'variant']
        indexes = [
            models.Index(fields=['brand', 'model', 'year', 'variant'], name='vehicle_identity_idx'),
            models.Index(fields=['brand', 'model'], name='vehicle_brand_model_idx'),
            models.Index(fields=['fcai_category'], name='vehicle_fcai_category_idx'),
            models.Index(fields=['powertrain_type'], name='vehicle_powertrain_idx'),
        ]

    def __str__(self):
        return f"{self.year} {self.brand} {self.model} {self.variant}"


class GVGSourceDataset(models.TextChoices):
    """SourceRegistry ids for the two Green Vehicle Guide CSVs (Decision Note 8-Aug-2026)."""
    CURRENT = 'S-GVG-CURRENT', 'GVG current-vehicles CSV'
    OLDER_8603 = 'S-GVG-OLDER-8603', 'GVG older-vehicles CSV (1986-2003 Fuel Consumption Guide)'


class GreenVehicleGuideRecord(models.Model):
    """A vehicle identity + efficiency record imported from the Green Vehicle Guide CSVs.

    Serves the current-vehicle lookup chain (Decision Note - Vehicle Internet Lookup,
    8-Aug-2026, Option 1): local Carreb catalogue -> this table -> AI-assisted lookup /
    manual entry. Read-only at runtime; rows are written only by `import_gvg_csv`.
    NEVER CORE-scored and never surfaced in match results - identity + two spec fields
    (fuel consumption, tailpipe CO2) for the cost/CO2 baseline of a user's CURRENT car.

    Provenance: the 1986-2003 Fuel Consumption Guide data comes from a different test
    cycle and is NOT directly comparable with current GVG figures (GVG's own caveat) -
    `source_dataset` carries that flag; surface prior-year figures with the standard
    "estimated" labelling + source + "as at" date.
    """
    brand = models.CharField(max_length=255)
    model = models.CharField(max_length=255)
    variant = models.CharField(max_length=255, blank=True, default='')
    year = models.IntegerField()
    body_style = models.CharField(max_length=100, blank=True, default='')
    engine_desc = models.CharField(max_length=100, blank=True, default='')  # e.g. "2.0L Petrol" (display only)
    fuel_type_raw = models.CharField(max_length=100, blank=True, default='')  # GVG's own label, verbatim
    powertrain_type = models.CharField(  # normalised to Carreb's canonical 7; blank when unmappable
        max_length=50, choices=PowertrainType.choices, blank=True, default=''
    )
    fuel_consumption_per_100km = models.FloatField(null=True, blank=True)  # combined L/100km
    electric_energy_wh_per_km = models.FloatField(null=True, blank=True)
    tailpipe_co2_g_per_km = models.FloatField(null=True, blank=True)
    electric_range_km = models.FloatField(null=True, blank=True)
    test_cycle = models.CharField(  # only set when the CSV states it - never guessed
        max_length=20, choices=TestCycle.choices, blank=True, default=''
    )
    source_dataset = models.CharField(max_length=30, choices=GVGSourceDataset.choices)
    source_url = models.URLField(max_length=500, blank=True, default='')
    retrieved_at = models.DateTimeField(null=True, blank=True)  # when the CSV was downloaded
    raw_row = models.JSONField(default=dict, blank=True)  # verbatim CSV row for the audit trail
    # denormalised "<year> <brand> <model> <variant>" maintained by the importer;
    # backs the pg_trgm GIN index for <300ms type-ahead search
    search_text = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['brand', 'model', 'year', 'variant']
        constraints = [
            models.UniqueConstraint(
                fields=['brand', 'model', 'variant', 'year'], name='gvg_identity_uniq'
            ),
        ]
        indexes = [
            models.Index(fields=['brand', 'model'], name='gvg_brand_model_idx'),
            models.Index(fields=['year'], name='gvg_year_idx'),
            # pg_trgm-backed fuzzy type-ahead ("corolla 2019" -> rows in <50ms)
            GinIndex(fields=['search_text'], name='gvg_search_trgm_idx', opclasses=['gin_trgm_ops']),
        ]

    def build_search_text(self):
        return ' '.join(p for p in [str(self.year), self.brand, self.model, self.variant] if p)

    def save(self, *args, **kwargs):
        self.search_text = self.build_search_text()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.year} {self.brand} {self.model} {self.variant}".strip()
