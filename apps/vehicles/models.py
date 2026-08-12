import uuid
from django.db import models
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
