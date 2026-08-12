from rest_framework import serializers
from apps.mygarage.models import CurrentVehicle, GarageVehicle, VehicleOverride, UserPriorityState, GarageVehicleSource
from apps.vehicles.models import Vehicle, GreenVehicleGuideRecord

class CurrentVehicleSerializer(serializers.ModelSerializer):
    class Meta:
        model = CurrentVehicle
        fields = ['id', 'vehicle_catalogue_id', 'manual_entry_data', 'purchase_date', 'licence_plate', 'odometer_reading', 'state', 'created_at', 'updated_at']

class GarageVehicleSerializer(serializers.ModelSerializer):
    class Meta:
        model = GarageVehicle
        fields = ['id', 'vehicle_catalogue_id', 'status', 'source', 'added_at', 'archived_at']

class VehicleOverrideSerializer(serializers.ModelSerializer):
    class Meta:
        model = VehicleOverride
        fields = ['id', 'field_name', 'override_value', 'overridden_at']

class UserPriorityStateSerializer(serializers.ModelSerializer):
    class Meta:
        model = UserPriorityState
        fields = ['budget_min', 'budget_max', 'priority_slider_position', 'active_core_mode', 'updated_at']
        read_only_fields = ['updated_at']

class CurrentVehicleCreateSerializer(serializers.Serializer):
    vehicle_catalogue_id = serializers.UUIDField(required=False, allow_null=True)
    # A GreenVehicleGuideRecord pk from the current-vehicle search ("source": "gvg").
    # The view enriches manual_entry_data from the record server-side, provenance intact.
    gvg_record_id = serializers.IntegerField(required=False, allow_null=True)
    manual_entry_data = serializers.JSONField(required=False, allow_null=True)
    purchase_date = serializers.DateField(required=False, allow_null=True)
    licence_plate = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    odometer_reading = serializers.IntegerField(required=False, allow_null=True)
    state = serializers.CharField(required=False, allow_blank=True, allow_null=True)

    def validate_vehicle_catalogue_id(self, value):
        if value and not Vehicle.objects.filter(id=value).exists():
            raise serializers.ValidationError("Vehicle with this ID does not exist in the catalogue.")
        return value

    def validate_gvg_record_id(self, value):
        if value and not GreenVehicleGuideRecord.objects.filter(pk=value).exists():
            raise serializers.ValidationError("GVG record with this ID does not exist.")
        return value

class GarageVehicleCreateSerializer(serializers.Serializer):
    vehicle_catalogue_id = serializers.UUIDField()
    source = serializers.ChoiceField(choices=GarageVehicleSource.choices)
    
    def validate_vehicle_catalogue_id(self, value):
        if value and not Vehicle.objects.filter(id=value).exists():
            raise serializers.ValidationError("Vehicle with this ID does not exist in the catalogue.")
        return value
    
class VehicleDetailOverrideSerializer(serializers.Serializer):
    field_name = serializers.CharField()
    override_value = serializers.JSONField()

class AISmartMatchInputSerializer(serializers.Serializer):
    query = serializers.CharField(max_length=500, required=True)
    state = serializers.ChoiceField(
        choices=['NSW', 'VIC', 'QLD', 'SA', 'WA', 'NT', 'TAS', 'ACT'], 
        required=False,
        allow_null=True
    )

import re
from apps.mygarage.models import RideRevealResult

class RideRevealGenerateSerializer(serializers.Serializer):
    garage_vehicle_id = serializers.UUIDField(required=True)
    custom_plate = serializers.CharField(max_length=7, required=False, allow_blank=True, allow_null=True)

    def validate_custom_plate(self, value):
        if value:
            if not re.match(r'^[A-Za-z0-9]+$', value):
                raise serializers.ValidationError("Custom plate must be alphanumeric only (A-Z, 0-9) and up to 7 characters.")
        return value

class RideRevealPurchaseSerializer(serializers.Serializer):
    purchase_type = serializers.ChoiceField(choices=['single', 'bundle'], required=True)

class RideRevealResultSerializer(serializers.ModelSerializer):
    vehicle = serializers.SerializerMethodField()

    class Meta:
        model = RideRevealResult
        fields = [
            'archetype_name', 'archetype_tagline', 'self_insight_text', 
            'social_perception_text', 'did_you_know_fact', 'share_caption',
            'disclaimer', 'custom_plate', 'generated_at', 'vehicle'
        ]

    def get_vehicle(self, obj):
        from apps.vehicles.services.rating_engine import calculate_core_rating
        from apps.mygarage.models import VehicleOverride
        vehicle = obj.vehicle_catalogue
        user = obj.user
        
        cv = getattr(user, 'current_vehicle', None)
        state = cv.state if cv and cv.state else 'NSW'
        
        priority_state = getattr(user, 'priority_state', None)
        priority = priority_state.priority_slider_position if priority_state else 3
        mode = priority_state.active_core_mode if priority_state else 'pure_cost'
        
        overrides_qs = VehicleOverride.objects.filter(user=user, garage_vehicle=obj.garage_vehicle)
        user_inputs = {o.field_name: o.override_value for o in overrides_qs}
        
        engine_data = calculate_core_rating(vehicle, state, priority, mode, user_inputs)
        
        return {
            'make': vehicle.brand,
            'model': vehicle.model,
            'year': vehicle.year,
            'variant': vehicle.variant,
            'powertrain_type': vehicle.powertrain_type,
            'image_url': vehicle.image_url,
            'core_rating': engine_data.get('core_rating'),
            'band_name': engine_data.get('band_name')
        }

def is_valid_australian_postcode(postcode: str) -> bool:
    if not postcode.isdigit() or len(postcode) != 4:
        return False
    val = int(postcode)
    if val < 200 or val > 9999:
        return False
    if 300 <= val <= 799:
        return False
    if (200 <= val <= 299) or (800 <= val <= 899) or (1000 <= val <= 9999):
        return True
    return False

class FindDealerRequestSerializer(serializers.Serializer):
    garage_vehicle_id = serializers.UUIDField(required=True)
    postcode = serializers.CharField(required=True)
    radius_km = serializers.IntegerField(required=True)

    def validate_postcode(self, value):
        if not is_valid_australian_postcode(value):
            raise serializers.ValidationError("invalid_postcode")
        return value

    def validate_radius_km(self, value):
        if value not in [5, 25, 50, 100]:
            raise serializers.ValidationError("invalid_radius")
        return value

class DealerLinkClickedSerializer(serializers.Serializer):
    vehicle_brand = serializers.CharField(required=True, max_length=255)

