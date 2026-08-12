import uuid
from django.db import models
from django.utils import timezone
from apps.users.models import User
from apps.vehicles.models import Vehicle

class CurrentVehicle(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='current_vehicle')
    vehicle_catalogue = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='current_vehicles')
    manual_entry_data = models.JSONField(null=True, blank=True)
    purchase_date = models.DateField(null=True, blank=True)
    licence_plate = models.CharField(max_length=255, null=True, blank=True) # Encrypted at service level
    odometer_reading = models.IntegerField(null=True, blank=True)
    state = models.CharField(max_length=10, null=True, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Current Vehicle for {self.user.email}"

class GarageVehicleStatus(models.TextChoices):
    ACTIVE = 'active', 'Active'
    ARCHIVED = 'archived', 'Archived'

class GarageVehicleSource(models.TextChoices):
    SMART_CAR_FINDER = 'smart_car_finder', 'Smart Car Finder'
    AI_SMART_MATCH = 'ai_smart_match', 'AI Smart Match'
    VEHICLE_LOOKUP = 'vehicle_lookup', 'Vehicle Lookup'
    FREE_MATCH_WIZARD = 'free_match_wizard', 'Free Match Wizard'
    CORE_DIAL = 'core_dial', 'CORE Dial'

class GarageVehicle(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='garage_vehicles')
    vehicle_catalogue = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='garage_entries')
    status = models.CharField(max_length=20, choices=GarageVehicleStatus.choices, default=GarageVehicleStatus.ACTIVE)
    source = models.CharField(max_length=50, choices=GarageVehicleSource.choices)
    
    read_only = models.BooleanField(default=False)
    re_subscribe_required = models.BooleanField(default=False)
    
    added_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    
    def __str__(self):
        return f"Garage: {self.vehicle_catalogue} for {self.user.email}"

class VehicleOverride(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='vehicle_overrides')
    current_vehicle = models.ForeignKey(CurrentVehicle, on_delete=models.CASCADE, null=True, blank=True, related_name='overrides')
    garage_vehicle = models.ForeignKey(GarageVehicle, on_delete=models.CASCADE, null=True, blank=True, related_name='overrides')
    field_name = models.CharField(max_length=100)
    override_value = models.JSONField()
    overridden_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        # A specific field on a specific vehicle can only be overridden once.
        constraints = [
            models.UniqueConstraint(
                fields=['current_vehicle', 'field_name'], 
                name='unique_current_vehicle_override',
                condition=models.Q(current_vehicle__isnull=False)
            ),
            models.UniqueConstraint(
                fields=['garage_vehicle', 'field_name'], 
                name='unique_garage_vehicle_override',
                condition=models.Q(garage_vehicle__isnull=False)
            )
        ]

    def __str__(self):
        ref = self.current_vehicle or self.garage_vehicle
        return f"Override {self.field_name} for {ref}"

class CoreMode(models.TextChoices):
    PURE_COST = 'pure_cost', 'Pure Cost'
    COOL_CHOICE = 'cool_choice', 'Cool Choice'
    TRUE_VALUE = 'true_value', 'True Value'
    FULL_PICTURE = 'full_picture', 'Full Picture'

class UserPriorityState(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='priority_state')
    budget_min = models.IntegerField(null=True, blank=True)
    budget_max = models.IntegerField(null=True, blank=True)
    priority_slider_position = models.IntegerField(default=3) # 1-5
    active_core_mode = models.CharField(max_length=50, choices=CoreMode.choices, default=CoreMode.PURE_COST)
    updated_at = models.DateTimeField(auto_now=True)
    
    def __str__(self):
        return f"Priority state for {self.user.email}"

class AISmartMatchRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ai_smart_match_requests')
    raw_query = models.CharField(max_length=500)
    interpreted_filters = models.JSONField(null=True, blank=True)
    result_vehicle_ids = models.JSONField(default=list)
    result_count = models.IntegerField(default=0)
    ai_service_available = models.BooleanField(default=True)
    match_count_at_time = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"AI Match for {self.user.email} at {self.created_at}"

class RideRevealCreditType(models.TextChoices):
    PLAN_INCLUDED = 'plan_included', 'Plan Included'
    PURCHASED_SINGLE = 'purchased_single', 'Purchased Single'
    PURCHASED_BUNDLE = 'purchased_bundle', 'Purchased Bundle'

class RideRevealCredit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='ridereveal_credit')
    plan = models.CharField(max_length=50)
    credits_included = models.IntegerField(default=0)
    credits_purchased = models.IntegerField(default=0)
    credits_used = models.IntegerField(default=0)
    period_start = models.DateField(null=True, blank=True)
    period_end = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    @property
    def credits_available(self):
        return max(0, self.credits_included + self.credits_purchased - self.credits_used)
        
    def __str__(self):
        return f"RideReveal Credits for {self.user.email}"

class RideRevealResult(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ridereveal_results')
    garage_vehicle = models.ForeignKey(GarageVehicle, on_delete=models.CASCADE, related_name='ridereveal_results')
    vehicle_catalogue = models.ForeignKey(Vehicle, on_delete=models.CASCADE, related_name='ridereveal_results')
    custom_plate = models.CharField(max_length=7, null=True, blank=True)
    
    archetype_name = models.CharField(max_length=255)
    archetype_tagline = models.CharField(max_length=255)
    self_insight_text = models.TextField()
    social_perception_text = models.TextField()
    did_you_know_fact = models.TextField()
    share_caption = models.CharField(max_length=280)
    disclaimer = models.CharField(max_length=255, default="for entertainment and educational purposes only")
    ai_model_version = models.CharField(max_length=50)
    generation_duration_ms = models.IntegerField()
    generated_at = models.DateTimeField(auto_now_add=True)
    credit_type = models.CharField(max_length=50, choices=RideRevealCreditType.choices)

    def __str__(self):
        return f"RideReveal {self.archetype_name} for {self.garage_vehicle}"

class RideRevealJobStatus(models.TextChoices):
    PENDING = 'pending', 'Pending'
    PROCESSING = 'processing', 'Processing'
    COMPLETE = 'complete', 'Complete'
    FAILED = 'failed', 'Failed'

class RideRevealJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ridereveal_jobs')
    garage_vehicle = models.ForeignKey(GarageVehicle, on_delete=models.CASCADE, related_name='ridereveal_jobs')
    custom_plate = models.CharField(max_length=7, null=True, blank=True)
    status = models.CharField(max_length=20, choices=RideRevealJobStatus.choices, default=RideRevealJobStatus.PENDING)
    result = models.ForeignKey(RideRevealResult, on_delete=models.SET_NULL, null=True, blank=True, related_name='jobs')
    error_message = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"RideRevealJob {self.id} - {self.status}"

class DealerSearchLog(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='dealer_searches')
    vehicle_brand = models.CharField(max_length=255)
    postcode = models.CharField(max_length=4)
    radius_km = models.IntegerField()
    results_count = models.IntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"DealerSearchLog {self.id} for {self.user.email} - {self.vehicle_brand}"
