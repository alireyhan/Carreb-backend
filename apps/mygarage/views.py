import re

from rest_framework import views, status, generics
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.utils import timezone
from django.core.cache import cache
from django.db import models, connection
from django.contrib.postgres.search import TrigramSimilarity
from django.shortcuts import get_object_or_404
from apps.mygarage.models import (
    CurrentVehicle, GarageVehicle, GarageVehicleStatus, GarageVehicleSource,
    VehicleOverride, UserPriorityState, CoreMode, AISmartMatchRequest
)
from apps.vehicles.models import Vehicle, GreenVehicleGuideRecord
from apps.users.models import SubscriptionTier
from apps.mygarage.serializers import (
    CurrentVehicleSerializer, GarageVehicleSerializer,
    UserPriorityStateSerializer, CurrentVehicleCreateSerializer,
    GarageVehicleCreateSerializer, VehicleDetailOverrideSerializer,
    AISmartMatchInputSerializer
)
from apps.mygarage.services import check_cap_limit, get_cap_counter, calculate_spotlights
from apps.mygarage.ai_service import extract_search_parameters, build_recommendation_reason
from apps.vehicles.services.rating_engine import calculate_core_rating
from apps.vehicles.tasks import emit_analytics_event

def _get_overrides(user, current_vehicle=None, garage_vehicle=None):
    overrides = VehicleOverride.objects.filter(
        user=user, 
        current_vehicle=current_vehicle, 
        garage_vehicle=garage_vehicle
    )
    return {o.field_name: o.override_value for o in overrides}

def _build_calculated_vehicle_response(user, vehicle_instance, priority_state, current_cost=None, is_current_vehicle=False):
    base_data = None
    overrides_qs = None
    
    if is_current_vehicle:
        base_data = CurrentVehicleSerializer(vehicle_instance).data
        overrides_qs = VehicleOverride.objects.filter(user=user, current_vehicle=vehicle_instance)
        # Current vehicle ALWAYS uses pure cost
        mode = CoreMode.PURE_COST
        priority = 3
        state = vehicle_instance.state or 'NSW'
        catalogue = vehicle_instance.vehicle_catalogue
    else:
        base_data = GarageVehicleSerializer(vehicle_instance).data
        overrides_qs = VehicleOverride.objects.filter(user=user, garage_vehicle=vehicle_instance)
        mode = priority_state.active_core_mode
        priority = priority_state.priority_slider_position
        # Assume state from current vehicle if exists, else default
        cv = getattr(user, 'current_vehicle', None)
        state = cv.state if cv and cv.state else 'NSW'
        catalogue = vehicle_instance.vehicle_catalogue
        
    if catalogue and base_data:
        base_data['image_url'] = catalogue.image_url

    user_inputs = {o.field_name: o.override_value for o in overrides_qs}
    
    engine_data = None
    if catalogue:
        engine_data = calculate_core_rating(
            catalogue, 
            state, 
            priority, 
            mode, 
            user_inputs, 
            current_vehicle_annual_cost=current_cost
        )
        
    return {
        "vehicle_data": base_data,
        "engine_data": engine_data,
        "overrides": user_inputs,
        "meta": {
            "calculated_at": timezone.now().isoformat(),
            "is_estimate": True,
            "disclaimer": "All figures are estimates only."
        }
    }

class CurrentVehicleView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        try:
            cv = request.user.current_vehicle
            state = getattr(request.user, 'priority_state', None)
            if not state:
                state = UserPriorityState.objects.create(user=request.user)
            resp = _build_calculated_vehicle_response(request.user, cv, state, is_current_vehicle=True)
            return Response(resp)
        except CurrentVehicle.DoesNotExist:
            return Response(None)
            
    def post(self, request):
        serializer = CurrentVehicleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        
        vehicle_catalogue_id = data.get('vehicle_catalogue_id')

        # GVG-sourced selection (prior-year vehicle from the type-ahead): enrich
        # manual_entry_data from the record server-side so identity + the two spec
        # fields land with provenance (source='gvg', dataset, retrieved date) and the
        # existing manual/benchmark calc path takes over from there. User-provided
        # manual_entry_data keys win over GVG values.
        gvg_record_id = data.get('gvg_record_id')
        if gvg_record_id and not vehicle_catalogue_id:
            gvg = GreenVehicleGuideRecord.objects.get(pk=gvg_record_id)
            gvg_fields = {
                'brand': gvg.brand, 'model': gvg.model, 'variant': gvg.variant,
                'year': gvg.year, 'body_style': gvg.body_style,
                'engine_desc': gvg.engine_desc,
                'powertrain_type': gvg.powertrain_type,
                'fuel_consumption_per_100km': gvg.fuel_consumption_per_100km,
                'electric_energy_wh_per_km': gvg.electric_energy_wh_per_km,
                'tailpipe_co2_g_per_km': gvg.tailpipe_co2_g_per_km,
                'electric_range_km': gvg.electric_range_km,
                'source': 'gvg', 'source_dataset': gvg.source_dataset,
                'test_cycle': gvg.test_cycle,
                'retrieved_at': gvg.retrieved_at.isoformat() if gvg.retrieved_at else None,
            }
            gvg_fields = {k: v for k, v in gvg_fields.items() if v not in (None, '')}
            data['manual_entry_data'] = {**gvg_fields, **(data.get('manual_entry_data') or {})}

        exists_in_garage = False

        if vehicle_catalogue_id:
            exists_in_garage = GarageVehicle.objects.filter(
                user=request.user,
                vehicle_catalogue_id=vehicle_catalogue_id,
                status=GarageVehicleStatus.ACTIVE
            ).exists()
            
            if not exists_in_garage and not check_cap_limit(request.user):
                cap = get_cap_counter(request.user)
                return Response({
                    "detail": f"Plan limit reached. You are on {cap['plan']} which allows {cap['limit']} vehicles.",
                    "cap_counter": cap
                }, status=status.HTTP_403_FORBIDDEN)
        
        cv, created = CurrentVehicle.objects.update_or_create(
            user=request.user,
            defaults={
                'vehicle_catalogue_id': data.get('vehicle_catalogue_id'),
                'manual_entry_data': data.get('manual_entry_data'),
                'purchase_date': data.get('purchase_date'),
                'licence_plate': data.get('licence_plate'),
                'odometer_reading': data.get('odometer_reading'),
                'state': data.get('state'),
            }
        )
        
        if vehicle_catalogue_id and not exists_in_garage:
            GarageVehicle.objects.create(
                user=request.user,
                vehicle_catalogue_id=vehicle_catalogue_id,
                source=GarageVehicleSource.VEHICLE_LOOKUP
            )
            if getattr(request.user, 'analytics_consent', False):
                emit_analytics_event.delay(str(request.user.id), 'vehicle_added_to_garage', {})
        
        state = getattr(request.user, 'priority_state', None)
        if not state:
            state = UserPriorityState.objects.create(user=request.user)
            
        resp = _build_calculated_vehicle_response(request.user, cv, state, is_current_vehicle=True)
        cache.delete(f"spotlights_{request.user.id}")
        return Response(resp)

class CurrentVehicleSearchView(views.APIView):
    """Current-vehicle type-ahead: local catalogue -> GVG table (Decision Note 8-Aug-2026).

    The Carreb catalogue holds only vehicles ON SALE TODAY, so a typical current
    vehicle (2019 Corolla, 2016 Ranger) can never match it - the GVG tier is what
    makes this search work for prior-year cars. Rows are DB-only, never AI-generated.
    Dedup: same (year, brand, model, variant) never shown twice; catalogue row wins.
    (Exempt from the Patch 6.4.1 Match-List Selection Rule - exact-vehicle tool.)
    """
    authentication_classes = []
    permission_classes = [AllowAny]

    MAX_RESULTS = 10
    MIN_QUERY_LEN = 3

    def post(self, request):
        query = (request.data.get('query') or '').strip()
        if len(query) < self.MIN_QUERY_LEN:
            return Response([])

        # Tokenise: a 4-digit token filters year; the rest match brand/model/variant.
        year_match = re.search(r'\b(19|20)\d{2}\b', query)
        year = int(year_match.group()) if year_match else None
        words = [w for w in re.sub(r'\b(19|20)\d{2}\b', ' ', query).split() if w]

        def text_filter(qs):
            for w in words:
                qs = qs.filter(
                    models.Q(brand__icontains=w) |
                    models.Q(model__icontains=w) |
                    models.Q(variant__icontains=w)
                )
            return qs

        results, seen = [], set()

        def identity_key(brand, model_name, variant, yr):
            return ((brand or '').casefold(), (model_name or '').casefold(),
                    (variant or '').casefold(), yr)

        # Tier 1 - Carreb catalogue (current new vehicles; preferred on collision)
        cat_qs = text_filter(Vehicle.objects.all())
        if year:
            cat_qs = cat_qs.filter(year=year)
        for v in cat_qs.order_by('brand', 'model', '-year')[:self.MAX_RESULTS]:
            seen.add(identity_key(v.brand, v.model, v.variant, v.year))
            results.append({
                "id": v.id, "name": str(v), "source": "catalogue",
                "brand": v.brand, "model": v.model, "variant": v.variant,
                "year": v.year, "body_style": v.body_style,
                "fuel_consumption_per_100km": v.fuel_consumption_per_100km,
                "tailpipe_co2_g_per_km": v.tailpipe_co2_g_per_km,
            })

        # Tier 2 - GVG records (current + older CSVs; covers prior-year vehicles)
        if len(results) < self.MAX_RESULTS:
            gvg_qs = GreenVehicleGuideRecord.objects.all()
            if connection.vendor == 'postgresql':
                needle = ' '.join(([str(year)] if year else []) + words) or query
                gvg_qs = gvg_qs.annotate(
                    sim=TrigramSimilarity('search_text', needle)
                ).filter(sim__gt=0.05).order_by('-sim', '-year')
                if year:
                    gvg_qs = gvg_qs.filter(year=year)
            else:  # non-postgres fallback (local sqlite dev)
                gvg_qs = text_filter(gvg_qs)
                if year:
                    gvg_qs = gvg_qs.filter(year=year)
                gvg_qs = gvg_qs.order_by('brand', 'model', '-year')

            for r in gvg_qs[:self.MAX_RESULTS * 2]:
                if len(results) >= self.MAX_RESULTS:
                    break
                key = identity_key(r.brand, r.model, r.variant, r.year)
                if key in seen:
                    continue  # catalogue row wins (Decision Note dedup rule)
                seen.add(key)
                results.append({
                    "id": f"gvg-{r.pk}", "name": str(r), "source": "gvg",
                    "gvg_record_id": r.pk,
                    "brand": r.brand, "model": r.model, "variant": r.variant,
                    "year": r.year, "body_style": r.body_style,
                    "engine_desc": r.engine_desc,
                    "fuel_consumption_per_100km": r.fuel_consumption_per_100km,
                    "tailpipe_co2_g_per_km": r.tailpipe_co2_g_per_km,
                    "test_cycle": r.test_cycle, "source_dataset": r.source_dataset,
                })

        return Response(results)

class CurrentVehicleDetailsView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def patch(self, request):
        try:
            cv = request.user.current_vehicle
        except CurrentVehicle.DoesNotExist:
            return Response({"detail": "No current vehicle set"}, status=status.HTTP_404_NOT_FOUND)
            
        serializer = VehicleDetailOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        VehicleOverride.objects.update_or_create(
            user=request.user,
            current_vehicle=cv,
            field_name=serializer.validated_data['field_name'],
            defaults={'override_value': serializer.validated_data['override_value']}
        )
        
        if getattr(request.user, 'analytics_consent', False):
            emit_analytics_event.delay(str(request.user.id), 'vehicle_field_overridden', {'field_name': serializer.validated_data['field_name']})
            
        state = getattr(request.user, 'priority_state', None)
        cache.delete(f"spotlights_{request.user.id}")
        return Response(_build_calculated_vehicle_response(request.user, cv, state, is_current_vehicle=True))

class CurrentVehicleOverrideDeleteView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def delete(self, request, field_name):
        try:
            cv = request.user.current_vehicle
        except CurrentVehicle.DoesNotExist:
            return Response({"detail": "No current vehicle set"}, status=status.HTTP_404_NOT_FOUND)
            
        deleted, _ = VehicleOverride.objects.filter(user=request.user, current_vehicle=cv, field_name=field_name).delete()
        if not deleted:
            return Response({"detail": "Override not found"}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            
        state = getattr(request.user, 'priority_state', None)
        cache.delete(f"spotlights_{request.user.id}")
        return Response(_build_calculated_vehicle_response(request.user, cv, state, is_current_vehicle=True))

class CurrentVehicleAutoFillView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        section = request.data.get('section')
        if section not in ['technical_details', 'vehicle_costs', 'finance']:
            return Response({"detail": "Invalid section"}, status=status.HTTP_400_BAD_REQUEST)
        # Placeholder for AI logic
        return Response({
            "suggested_values": {
                "annual_km": 15000,
                "insurance_override": 1200
            }
        })

class GarageVehicleListView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        qs = GarageVehicle.objects.filter(user=request.user, status=GarageVehicleStatus.ACTIVE)
        
        state = getattr(request.user, 'priority_state', None)
        if not state:
            state = UserPriorityState.objects.create(user=request.user)
            
        cv_cost = None
        cv = getattr(request.user, 'current_vehicle', None)
        if cv:
            cv_resp = _build_calculated_vehicle_response(request.user, cv, state, is_current_vehicle=True)
            if cv_resp['engine_data']:
                cv_cost = cv_resp['engine_data'].get('total_annual_cost')
        
        results = []
        for gv in qs:
            results.append(_build_calculated_vehicle_response(request.user, gv, state, current_cost=cv_cost))
            
        # --- FILTERING LOGIC ---
        core_ratings_param = request.query_params.get('core_ratings')
        coo_5yrs_min_param = request.query_params.get('coo_5yrs_min')
        coo_5yrs_max_param = request.query_params.get('coo_5yrs_max')
        co2_emissions_param = request.query_params.get('co2_emissions')
        savings_param = request.query_params.get('savings')
        driveaway_min_param = request.query_params.get('driveaway_min')
        driveaway_max_param = request.query_params.get('driveaway_max')

        filtered_results = []
        for res in results:
            engine_data = res.get('engine_data')
            if not engine_data:
                # If there's no calculated data, we either include or exclude. 
                # Let's include by default unless specific filters are applied.
                if any([core_ratings_param, coo_5yrs_min_param, coo_5yrs_max_param, co2_emissions_param, savings_param, driveaway_min_param, driveaway_max_param]):
                    continue
                filtered_results.append(res)
                continue

            # 1. CORE Rating Filter
            if core_ratings_param:
                try:
                    valid_ratings = [int(r.strip()) for r in core_ratings_param.split(',')]
                    rounded_rating = int(round(engine_data.get('core_rating', 0)))
                    if rounded_rating not in valid_ratings:
                        continue
                except ValueError:
                    pass

            # 2. COO 5yrs Filter
            coo_5yrs = engine_data.get('total_annual_cost', 0) * 5
            if coo_5yrs_min_param:
                try:
                    if coo_5yrs < float(coo_5yrs_min_param): continue
                except ValueError: pass
            if coo_5yrs_max_param:
                try:
                    if coo_5yrs > float(coo_5yrs_max_param): continue
                except ValueError: pass

            # 3. CO2 Emissions Filter
            if co2_emissions_param:
                valid_buckets = [b.strip().lower() for b in co2_emissions_param.split(',')]
                tailpipe = engine_data.get('tailpipe_co2_g_per_km', 0)
                bucket = ''
                if tailpipe <= 50: bucket = 'very_low'
                elif 51 <= tailpipe <= 130: bucket = 'low'
                elif 131 <= tailpipe <= 180: bucket = 'medium'
                else: bucket = 'high'
                
                if bucket not in valid_buckets:
                    continue

            # 4. Savings (5yr) Filter
            if savings_param:
                valid_buckets = [b.strip().lower() for b in savings_param.split(',')]
                est_diff_obj = engine_data.get('est_cost_difference')
                bucket = ''
                if isinstance(est_diff_obj, dict) and est_diff_obj.get('direction') == 'saving':
                    bucket = est_diff_obj.get('savings_bucket') or ''

                if bucket not in valid_buckets:
                    continue

            # 5. Driveaway Price Filter
            driveaway_price = engine_data.get('annual_cost_breakdown', {}).get('driveaway_price', 0)
            if driveaway_min_param:
                try:
                    if driveaway_price < float(driveaway_min_param): continue
                except ValueError: pass
            if driveaway_max_param:
                try:
                    if driveaway_price > float(driveaway_max_param): continue
                except ValueError: pass

            filtered_results.append(res)
            
        # Use filtered results moving forward
        results = filtered_results

        # Optional sorting/filtering can be applied to `results` here
        sort_by = request.query_params.get('sort_by')
        if sort_by in ['core_rating', 'annual_cost', 'cost_difference', 'driveaway_price']:
            def sort_key(x):
                if not x.get('engine_data'): return 0
                if sort_by == 'cost_difference':
                    diff_obj = x['engine_data'].get('est_cost_difference')
                    if isinstance(diff_obj, dict):
                        return diff_obj.get('value_cents', 0) if diff_obj.get('direction') == 'saving' else -diff_obj.get('value_cents', 0)
                    return diff_obj or 0
                return x['engine_data'].get(sort_by) or 0
            results.sort(key=sort_key)
            
        return Response({
            "cap_counter": get_cap_counter(request.user),
            "vehicles": results
        })

    def post(self, request):
        serializer = GarageVehicleCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        if not check_cap_limit(request.user):
            cap = get_cap_counter(request.user)
            return Response({
                "detail": f"Plan limit reached. You are on {cap['plan']} which allows {cap['limit']} vehicles.",
                "cap_counter": cap
            }, status=status.HTTP_403_FORBIDDEN)
            
        gv = GarageVehicle.objects.create(
            user=request.user,
            vehicle_catalogue_id=serializer.validated_data['vehicle_catalogue_id'],
            source=serializer.validated_data['source']
        )
        
        if getattr(request.user, 'analytics_consent', False):
            emit_analytics_event.delay(str(request.user.id), 'vehicle_added_to_garage', {})
            
        cache.delete(f"spotlights_{request.user.id}")
        return Response(GarageVehicleSerializer(gv).data, status=status.HTTP_201_CREATED)

class GarageVehicleDetailView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request, garage_vehicle_id):
        gv = get_object_or_404(GarageVehicle, id=garage_vehicle_id, user=request.user)
        state = getattr(request.user, 'priority_state', None)
        
        cv_cost = None
        cv = getattr(request.user, 'current_vehicle', None)
        if cv:
            cv_resp = _build_calculated_vehicle_response(request.user, cv, state, is_current_vehicle=True)
            if cv_resp['engine_data']:
                cv_cost = cv_resp['engine_data'].get('total_annual_cost')
                
        return Response(_build_calculated_vehicle_response(request.user, gv, state, current_cost=cv_cost))

class GarageVehicleOverrideView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def patch(self, request, garage_vehicle_id):
        gv = get_object_or_404(GarageVehicle, id=garage_vehicle_id, user=request.user)
        serializer = VehicleDetailOverrideSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        VehicleOverride.objects.update_or_create(
            user=request.user,
            garage_vehicle=gv,
            field_name=serializer.validated_data['field_name'],
            defaults={'override_value': serializer.validated_data['override_value']}
        )
        
        if getattr(request.user, 'analytics_consent', False):
            emit_analytics_event.delay(str(request.user.id), 'vehicle_field_overridden', {'field_name': serializer.validated_data['field_name']})
            
        state = getattr(request.user, 'priority_state', None)
        return Response(_build_calculated_vehicle_response(request.user, gv, state))

    def delete(self, request, garage_vehicle_id, field_name):
        gv = get_object_or_404(GarageVehicle, id=garage_vehicle_id, user=request.user)
        deleted, _ = VehicleOverride.objects.filter(user=request.user, garage_vehicle=gv, field_name=field_name).delete()
        if not deleted:
            return Response({"detail": "Override not found"}, status=status.HTTP_422_UNPROCESSABLE_ENTITY)
            
        state = getattr(request.user, 'priority_state', None)
        return Response(_build_calculated_vehicle_response(request.user, gv, state))

class GarageVehicleArchiveView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request, garage_vehicle_id):
        gv = get_object_or_404(GarageVehicle, id=garage_vehicle_id, user=request.user)
        gv.status = GarageVehicleStatus.ARCHIVED
        gv.archived_at = timezone.now()
        gv.save()
        
        if getattr(request.user, 'analytics_consent', False):
            emit_analytics_event.delay(str(request.user.id), 'vehicle_archived', {})
            
        cache.delete(f"spotlights_{request.user.id}")
        return Response({"status": "archived"})

class GarageVehicleUnarchiveView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def post(self, request, garage_vehicle_id):
        gv = get_object_or_404(GarageVehicle, id=garage_vehicle_id, user=request.user)
        if gv.status == GarageVehicleStatus.ACTIVE:
            return Response({"detail": "Already active"}, status=status.HTTP_400_BAD_REQUEST)
            
        if not check_cap_limit(request.user):
            cap = get_cap_counter(request.user)
            return Response({
                "detail": f"Plan limit reached. You are on {cap['plan']} which allows {cap['limit']} vehicles.",
                "cap_counter": cap
            }, status=status.HTTP_403_FORBIDDEN)
            
        gv.status = GarageVehicleStatus.ACTIVE
        gv.archived_at = None
        gv.save()
        
        if getattr(request.user, 'analytics_consent', False):
            emit_analytics_event.delay(str(request.user.id), 'vehicle_unarchived', {})
            
        cache.delete(f"spotlights_{request.user.id}")
        return Response({"status": "active"})

class GarageVehicleDeleteView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def delete(self, request, garage_vehicle_id):
        gv = get_object_or_404(GarageVehicle, id=garage_vehicle_id, user=request.user)
        if gv.status != GarageVehicleStatus.ARCHIVED:
            return Response({"detail": "Vehicle must be archived before deletion"}, status=status.HTTP_400_BAD_REQUEST)
            
        gv.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

class ArchiveListView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        qs = GarageVehicle.objects.filter(user=request.user, status=GarageVehicleStatus.ARCHIVED)
        state = getattr(request.user, 'priority_state', None)
        
        results = []
        for gv in qs:
            results.append(_build_calculated_vehicle_response(request.user, gv, state))
            
        return Response(results)

class PriorityStateView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        state, _ = UserPriorityState.objects.get_or_create(user=request.user)
        return Response(UserPriorityStateSerializer(state).data)
        
    def patch(self, request):
        state, _ = UserPriorityState.objects.get_or_create(user=request.user)
        
        mode = request.data.get('active_core_mode')
        if mode in ['cool_choice', 'true_value', 'full_picture'] and request.user.subscription_tier == SubscriptionTier.FREE:
            if getattr(request.user, 'analytics_consent', False):
                emit_analytics_event.delay(str(request.user.id), 'core_mode_upgrade_prompt_shown', {})
            return Response({"detail": "Premium mode requested on free tier"}, status=status.HTTP_403_FORBIDDEN)
            
        serializer = UserPriorityStateSerializer(state, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        
        if getattr(request.user, 'analytics_consent', False):
            emit_analytics_event.delay(str(request.user.id), 'core_dial_adjusted', {
                'priority_slider_position': serializer.validated_data.get('priority_slider_position'),
                'active_core_mode': serializer.validated_data.get('active_core_mode')
            })
            
        # recalculate band counts
        qs = GarageVehicle.objects.filter(user=request.user, status=GarageVehicleStatus.ACTIVE)
        bands = {'net_zero_ninja': 0, 'smart_move': 0, 'steady_cruiser': 0, 'the_detour': 0, 'captain_carbon': 0}
        
        cv = getattr(request.user, 'current_vehicle', None)
        user_state = getattr(cv, 'state', 'NSW') if cv else 'NSW'
        
        for gv in qs:
            resp = _build_calculated_vehicle_response(request.user, gv, serializer.instance)
            b = resp['engine_data'].get('band_slug') if resp.get('engine_data') else None
            if b:
                b_key = b.replace('-', '_')
                if b_key in bands:
                    bands[b_key] += 1
                    
        cache.delete(f"spotlights_{request.user.id}")
        return Response({"state": serializer.data, "band_counts": bands})

class CoreDialBandBreakdownView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request, band_slug):
        qs = GarageVehicle.objects.filter(user=request.user, status=GarageVehicleStatus.ACTIVE)
        state = getattr(request.user, 'priority_state', None)
        
        results = []
        for gv in qs:
            resp = _build_calculated_vehicle_response(request.user, gv, state)
            if resp['engine_data'] and resp['engine_data'].get('band_slug') == band_slug:
                results.append(resp)
                
        return Response(results)

class SavingsSummaryView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        cv = getattr(request.user, 'current_vehicle', None)
        if not cv:
            return Response({"message_variant": "no_current_vehicle"})
            
        state = getattr(request.user, 'priority_state', None)
        cv_resp = _build_calculated_vehicle_response(request.user, cv, state, is_current_vehicle=True)
        cv_cost = cv_resp['engine_data'].get('total_annual_cost') if cv_resp.get('engine_data') else None
        
        qs = GarageVehicle.objects.filter(user=request.user, status=GarageVehicleStatus.ACTIVE)
        if not qs.exists():
            return Response({"message_variant": "no_garage_vehicles", "current_vehicle_annual_cost": cv_cost})
            
        max_savings = -999999999
        best_gv = None
        
        for gv in qs:
            resp = _build_calculated_vehicle_response(request.user, gv, state, current_cost=cv_cost)
            diff_obj = resp['engine_data'].get('est_cost_difference') if resp.get('engine_data') else None
            diff_cents = None
            if isinstance(diff_obj, dict):
                if diff_obj.get('direction') == 'saving':
                    diff_cents = diff_obj.get('value_cents')
                elif diff_obj.get('direction') == 'more_expensive':
                    diff_cents = -diff_obj.get('value_cents')
                else:
                    diff_cents = 0
            else:
                diff_cents = diff_obj
                
            if diff_cents is not None and diff_cents > max_savings:
                max_savings = diff_cents
                best_gv = gv
                
        if max_savings > 0 and best_gv:
            return Response({
                "message_variant": "savings_available",
                "current_vehicle_annual_cost": cv_cost,
                "best_garage_vehicle_id": best_gv.id,
                "max_potential_annual_savings": max_savings
            })
        else:
            return Response({
                "message_variant": "no_savings_available",
                "current_vehicle_annual_cost": cv_cost
            })

class SpotlightsView(views.APIView):
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        if request.user.subscription_tier == SubscriptionTier.FREE:
            return Response({
                "error": "spotlights_locked",
                "message": "Spotlight badges are available on Car Buying Sprint and above.",
                "upgrade_required": True,
                "minimum_plan": "car_buying_sprint"
            }, status=status.HTTP_403_FORBIDDEN)
            
        cache_key = f"spotlights_{request.user.id}"
        cached = cache.get(cache_key)
        if cached is not None:
            return Response(cached)
            
        qs = GarageVehicle.objects.filter(
            user=request.user, 
            status=GarageVehicleStatus.ACTIVE
        ).select_related('vehicle_catalogue')
        
        user_state = getattr(request.user, 'priority_state', None)
        
        cv = getattr(request.user, 'current_vehicle', None)
        s = getattr(cv, 'state', 'NSW') if cv else 'NSW'
        
        # Calculate cv_cost once to pass along
        cv_cost = None
        if cv:
            cv_resp = _build_calculated_vehicle_response(request.user, cv, user_state, is_current_vehicle=True)
            cv_cost = cv_resp['engine_data'].get('total_annual_cost') if cv_resp.get('engine_data') else None
            
        results = calculate_spotlights(list(qs), user_state, s, cv_cost)
        
        cache.set(cache_key, results, 300)
        return Response(results)

class AISmartMatchView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = AISmartMatchInputSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        query = serializer.validated_data['query']
        state = serializer.validated_data.get('state')
        if not state:
            cv = getattr(request.user, 'current_vehicle', None)
            state = cv.state if cv and getattr(cv, 'state', None) else 'NSW'

        user = request.user
        limit = user.get_match_limit()
        used = user.match_count_used
        
        # Step 1: Limit check
        if limit is not None and used >= limit:
            plan_name = dict(SubscriptionTier.choices).get(user.subscription_tier, "Free Starter")
            return Response({
                "error": "match_limit_reached",
                "match_count_used": used,
                "match_count_limit": limit,
                "plan": plan_name,
                "upgrade_message": "Upgrade to CORE Advantage for unlimited matches."
            }, status=status.HTTP_403_FORBIDDEN)
            
        # Step 2 & 3: AI extraction & Fallback
        ai_available, filters = extract_search_parameters(query)
        
        if not ai_available:
            AISmartMatchRequest.objects.create(
                user=user,
                raw_query=query,
                interpreted_filters=None,
                result_vehicle_ids=[],
                result_count=0,
                ai_service_available=False,
                match_count_at_time=used
            )
            if getattr(user, 'analytics_consent', False):
                emit_analytics_event.delay(str(user.id), 'ai_smart_match_fallback_shown', {})
                
            return Response({
                "results": [],
                "ai_service_available": False,
                "fallback_message": "AI matching is temporarily unavailable. Please use Smart Car Finder to find your vehicle."
            }, status=status.HTTP_200_OK)
            
        # Step 4: Query vehicles
        queryset = Vehicle.objects.all()
        
        def _ensure_list(val):
            if isinstance(val, str):
                return [val]
            return val if isinstance(val, list) else []

        if filters.get('powertrain_types'):
            pts = _ensure_list(filters['powertrain_types'])
            queryset = queryset.filter(powertrain_type__in=[p.lower() for p in pts])
            
        PRIMARY_USE_BODY_STYLES = {
            'city_commuter':  ["hatchback", "sedan"],
            'family_car':     ["suv", "wagon"],
            'mixed_use':      ["suv", "hatchback"],
            'long_distance':  ["suv", "sedan"],
            'work_vehicle':   ["ute", "van"],
        }
        
        if filters.get('body_styles'):
            bss = _ensure_list(filters['body_styles'])
            q = models.Q()
            for b in bss:
                q |= models.Q(body_style__icontains=b)
            queryset = queryset.filter(q)
        elif filters.get('primary_use'):
            use = filters['primary_use'].lower()
            bss = PRIMARY_USE_BODY_STYLES.get(use, [])
            if bss:
                q = models.Q()
                for b in bss:
                    q |= models.Q(body_style__icontains=b)
                queryset = queryset.filter(q)
                
        if filters.get('fcai_categories'):
            fcs = _ensure_list(filters['fcai_categories'])
            queryset = queryset.filter(fcai_category__in=fcs)
        if filters.get('num_seats_min'):
            queryset = queryset.filter(num_seats__gte=filters['num_seats_min'])
        if filters.get('ancap_rating_min'):
            queryset = queryset.filter(ancap_safety_rating__gte=filters['ancap_rating_min'])
            
        # Step 5: Score and sort
        user_state = getattr(user, 'priority_state', None)
        priority = user_state.priority_slider_position if user_state else 3
        mode = user_state.active_core_mode if user_state else CoreMode.PURE_COST
        
        cv = getattr(user, 'current_vehicle', None)
        cv_cost = None
        if cv and cv.vehicle_catalogue:
            cv_resp = calculate_core_rating(cv.vehicle_catalogue, state, priority, mode, {})
            cv_cost = cv_resp.get('total_annual_cost')
            
        all_vehicles = list(queryset)
        mode_map = {
            'pure_cost': 'B',
            'cool_choice': 'C',
            'true_value': 'E',
            'full_picture': 'F'
        }
        mode_letter = mode_map.get(mode, 'B')
        
        cheapest_variants = {}
        for vehicle in all_vehicles:
            val = float('inf')
            try:
                state_data = vehicle.mode_data_by_state.get(state, {})
                if isinstance(state_data, dict):
                    mode_data = state_data.get(mode_letter, {})
                    if isinstance(mode_data, dict):
                        coo = mode_data.get("mode_5yr_coo")
                        if coo is not None:
                            val = float(coo)
            except (AttributeError, ValueError, TypeError):
                pass
            
            key = (vehicle.brand, vehicle.model, vehicle.year)
            if key not in cheapest_variants:
                cheapest_variants[key] = (val, vehicle)
            else:
                if val < cheapest_variants[key][0]:
                    cheapest_variants[key] = (val, vehicle)
                    
        unique_vehicles = [v for _, v in cheapest_variants.values()]

        scored_vehicles = []
        category_metrics_cache = {}
        for v in unique_vehicles:
            driveaway = v.driveaway_price_by_state.get(state)
            
            if filters.get('budget_max') and driveaway and driveaway > filters['budget_max'] * 100:
                continue
            if filters.get('budget_min') and driveaway and driveaway < filters['budget_min'] * 100:
                continue
                
            res = calculate_core_rating(
                vehicle=v,
                state=state,
                priority_slider=priority,
                core_mode=mode,
                user_inputs={},
                current_vehicle_annual_cost=cv_cost,
                category_metrics_cache=category_metrics_cache
            )
            rating = res.get('core_rating', 0)
            
            if filters.get('core_rating_min') and (not rating or rating < filters['core_rating_min']):
                continue
                
            reason = build_recommendation_reason(filters, v)
            
            diff_obj = res.get('est_cost_difference')
            if isinstance(diff_obj, dict):
                diff_val = diff_obj.get('value_cents')
                if diff_obj.get('direction') == 'more_expensive':
                    diff_val = -diff_val if diff_val else 0
                diff_display = diff_obj.get('value_display')
            else:
                diff_val = diff_obj
                if diff_val is not None:
                    diff_display = f"${abs(diff_val) // 100:,} {'less' if diff_val > 0 else 'more'}"
                else:
                    diff_display = None
                
            scored_vehicles.append({
                "vehicle_id": str(v.id),
                "make": v.brand,
                "model": v.model,
                "year": v.year,
                "variant": v.variant,
                "powertrain_type": v.powertrain_type,
                "core_rating": round(rating, 1) if rating else None,
                "band_name": res.get('band_name'),
                "total_annual_cost": res.get('total_annual_cost'),
                "total_annual_cost_display": f"${res.get('total_annual_cost', 0) // 100:,}",
                "est_cost_difference": diff_val,
                "est_cost_difference_display": diff_display,
                "co2_g_per_km": v.tailpipe_co2_g_per_km,
                "driveaway_price": driveaway,
                "driveaway_price_display": f"${driveaway // 100:,}" if driveaway else None,
                "recommendation_reason": reason,
                "is_estimate": True
            })
            
        scored_vehicles.sort(key=lambda x: x['core_rating'] or 0, reverse=True)
        scored_vehicles = scored_vehicles[:10]
        
        if not scored_vehicles:
            return Response({
                "results": [],
                "ai_service_available": True,
                "interpreted_filters": filters,
                "empty_message": "No vehicles matched your search. Try broadening your criteria or use Smart Car Finder.",
                "match_count_used": used,
                "match_count_limit": limit
            }, status=status.HTTP_200_OK)
            
        # Step 6: Increment counter
        user.match_count_used += 1
        user.save()
        
        # Step 7: Save request
        AISmartMatchRequest.objects.create(
            user=user,
            raw_query=query,
            interpreted_filters=filters,
            result_vehicle_ids=[v['vehicle_id'] for v in scored_vehicles],
            result_count=len(scored_vehicles),
            ai_service_available=True,
            match_count_at_time=used
        )
        
        if getattr(user, 'analytics_consent', False):
            emit_analytics_event.delay(str(user.id), 'ai_smart_match_performed', {
                'ai_service_available': True,
                'result_count': len(scored_vehicles)
            })

        # Step 8: Return
        return Response({
            "results": scored_vehicles,
            "match_count_used": user.match_count_used,
            "match_count_limit": limit,
            "ai_service_available": True,
            "meta": {
                "calculated_at": timezone.now().isoformat(),
                "is_estimate": True,
                "disclaimer": "All figures are estimates only. Actual costs will vary based on individual driving habits, insurer quotes, dealer pricing, and personal circumstances."
            }
        }, status=status.HTTP_200_OK)
from rest_framework import views, status, generics
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny
from django.shortcuts import get_object_or_404
from apps.mygarage.models import RideRevealJob, RideRevealResult
from apps.mygarage.serializers import (
    RideRevealGenerateSerializer, 
    RideRevealPurchaseSerializer,
    RideRevealResultSerializer
)
from apps.mygarage.ridereveal_services import (
    get_ridereveal_credits,
    trigger_ridereveal_generation,
    create_stripe_payment_intent,
    process_stripe_webhook
)

class RideRevealCreditsView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = get_ridereveal_credits(request.user)
        return Response(data, status=status.HTTP_200_OK)

class RideRevealGenerateView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = RideRevealGenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        garage_vehicle_id = serializer.validated_data['garage_vehicle_id']
        custom_plate = serializer.validated_data.get('custom_plate')
        
        result = trigger_ridereveal_generation(request.user, garage_vehicle_id, custom_plate)
        
        status_code = result.pop('status_code')
        return Response(result, status=status_code)

class RideRevealJobView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, job_id):
        job = get_object_or_404(RideRevealJob, id=job_id, user=request.user)
        
        response_data = {
            "job_id": str(job.id),
            "status": job.status
        }
        
        if job.status == 'complete' and job.result:
            response_data['result'] = RideRevealResultSerializer(job.result).data
        elif job.status == 'failed':
            response_data['error_message'] = job.error_message
            response_data['credit_refunded'] = True
            
        return Response(response_data, status=status.HTTP_200_OK)

class RideRevealResultView(views.APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, garage_vehicle_id):
        try:
            result = RideRevealResult.objects.get(user=request.user, garage_vehicle_id=garage_vehicle_id)
            return Response(RideRevealResultSerializer(result).data, status=status.HTTP_200_OK)
        except RideRevealResult.DoesNotExist:
            return Response({"error": "not_found", "message": "No result found"}, status=status.HTTP_404_NOT_FOUND)

class RideRevealPurchaseView(views.APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        serializer = RideRevealPurchaseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        purchase_type = serializer.validated_data['purchase_type']
        
        try:
            client_secret = create_stripe_payment_intent(request.user, purchase_type)
            return Response({"client_secret": client_secret}, status=status.HTTP_200_OK)
        except Exception as e:
            return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

class StripeWebhookView(views.APIView):
    permission_classes = [AllowAny]
    authentication_classes = []

    def post(self, request):
        payload = request.body
        sig_header = request.META.get('HTTP_STRIPE_SIGNATURE', '')
        
        success = process_stripe_webhook(payload, sig_header)
        
        if success:
            return Response(status=status.HTTP_200_OK)
        else:
            return Response(status=status.HTTP_400_BAD_REQUEST)
