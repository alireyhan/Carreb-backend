from rest_framework import generics, views, status, pagination
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, AllowAny, IsAdminUser
from django.utils import timezone
from apps.vehicles.models import Vehicle
from apps.vehicles.serializers import (
    VehicleListSerializer, VehicleDetailSerializer,
    CoreRatingRequestSerializer, CoreRatingBatchRequestSerializer,
    CoreSearchRequestSerializer
)
from apps.vehicles.services.rating_engine import calculate_core_rating
from apps.vehicles.tasks import emit_analytics_event, process_vehicle_excel_upload
import os
from django.core.files.storage import default_storage
from apps.users.models import SubscriptionTier

class StandardResultsSetPagination(pagination.PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

class VehicleListView(generics.ListAPIView):
    queryset = Vehicle.objects.all()
    serializer_class = VehicleListSerializer
    pagination_class = StandardResultsSetPagination
    permission_classes = [AllowAny]
    
    def get_queryset(self):
        queryset = super().get_queryset()
        params = self.request.query_params
        
        if 'brand' in params:
            queryset = queryset.filter(brand__iexact=params['brand'])
        if 'model' in params:
            queryset = queryset.filter(model__iexact=params['model'])
        if 'year' in params:
            queryset = queryset.filter(year=params['year'])
        if 'powertrain_type' in params:
            queryset = queryset.filter(powertrain_type__iexact=params['powertrain_type'])
        if 'body_style' in params:
            queryset = queryset.filter(body_style__iexact=params['body_style'])
        if 'num_seats_min' in params:
            queryset = queryset.filter(num_seats__gte=params['num_seats_min'])
        if 'fcai_category' in params:
            queryset = queryset.filter(fcai_category__iexact=params['fcai_category'])
        if 'ancap_rating_min' in params:
            queryset = queryset.filter(ancap_safety_rating__gte=params['ancap_rating_min'])
            
        return queryset

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        if request.user.is_authenticated and getattr(request.user, 'analytics_consent', False):
            emit_analytics_event.delay(str(request.user.id), 'vehicle_search_performed', dict(request.query_params))
        return response

class VehicleDetailView(generics.RetrieveAPIView):
    queryset = Vehicle.objects.all()
    serializer_class = VehicleDetailSerializer
    permission_classes = [AllowAny]
    lookup_field = 'id'
    lookup_url_kwarg = 'vehicle_id'

class VehicleBrandsView(views.APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    
    def get(self, request):
        brands = Vehicle.objects.values_list('brand', flat=True).distinct().order_by('brand')
        return Response(brands)

class VehicleModelsView(views.APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    
    def get(self, request):
        brand = request.query_params.get('brand')
        if not brand:
            return Response({"detail": "brand is required"}, status=status.HTTP_400_BAD_REQUEST)
        models = Vehicle.objects.filter(brand__iexact=brand).values_list('model', flat=True).distinct().order_by('model')
        return Response(models)

class VehicleYearsView(views.APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    
    def get(self, request):
        brand = request.query_params.get('brand')
        model = request.query_params.get('model')
        if not brand or not model:
            return Response({"detail": "brand and model are required"}, status=status.HTTP_400_BAD_REQUEST)
        years = Vehicle.objects.filter(brand__iexact=brand, model__iexact=model).values_list('year', flat=True).distinct().order_by('-year')
        return Response(years)

class VehicleVariantsView(views.APIView):
    authentication_classes = []
    permission_classes = [AllowAny]
    
    def get(self, request):
        brand = request.query_params.get('brand')
        model = request.query_params.get('model')
        year = request.query_params.get('year')
        if not brand or not model or not year:
            return Response({"detail": "brand, model and year are required"}, status=status.HTTP_400_BAD_REQUEST)
        variants = Vehicle.objects.filter(brand__iexact=brand, model__iexact=model, year=year).values_list('variant', flat=True).distinct().order_by('variant')
        return Response(variants)


# D24: anonymous callers must not receive real gated dollar figures (e.g. the savings delta).
GATED_RATING_KEYS = ('est_cost_difference',)


def _strip_gated_for_anon(request, result):
    user = getattr(request, 'user', None)
    if user and getattr(user, 'is_authenticated', False):
        return result
    if isinstance(result, dict):
        for key in GATED_RATING_KEYS:
            result.pop(key, None)
    return result


class CoreRatingCalculateView(views.APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = CoreRatingRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        
        vehicle_id = data['vehicle_id']
        state = data.get('state')
        priority_slider = data['priority_slider']
        core_mode = data['core_mode']
        user_inputs = data.get('user_inputs', {})
        current_cost = data.get('current_vehicle_annual_cost')
        budget_min = data.get('budget_min')
        budget_max = data.get('budget_max')
        
        if not state:
            return Response({"detail": "state is required", "requires_state": True}, status=status.HTTP_400_BAD_REQUEST)
            
        try:
            vehicle = Vehicle.objects.get(id=vehicle_id)
        except Vehicle.DoesNotExist:
            return Response({"detail": "Vehicle not found"}, status=status.HTTP_404_NOT_FOUND)
            
        # Check subscription lock
        is_premium_mode = core_mode in ['cool_choice', 'true_value', 'full_picture']
        if is_premium_mode:
            if not request.user.is_authenticated or request.user.subscription_tier == SubscriptionTier.FREE:
                return Response({"locked": True, "detail": "Premium subscription required for this mode"}, status=status.HTTP_403_FORBIDDEN)
        
        result = calculate_core_rating(
            vehicle, state, priority_slider, core_mode, user_inputs,
            budget_min=budget_min, budget_max=budget_max,
            current_vehicle_annual_cost=current_cost
        )
        result['image_url'] = vehicle.image_url
        _strip_gated_for_anon(request, result)

        if request.user.is_authenticated and getattr(request.user, 'analytics_consent', False):
            emit_analytics_event.delay(str(request.user.id), 'vehicle_match_result_viewed', {'vehicle_id': str(vehicle.id), 'core_mode': core_mode})
            
        return Response({
            "meta": {
                "calculated_at": timezone.now().isoformat(),
                "is_estimate": True,
                "disclaimer": "All figures are estimates only. Actual costs will vary based on individual driving habits, insurer quotes, dealer pricing, and personal circumstances."
            },
            "data": result
        })

class CoreRatingBatchView(views.APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        serializer = CoreRatingBatchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        
        vehicle_ids = data['vehicle_ids']
        state = data.get('state')
        priority_slider = data['priority_slider']
        core_mode = data['core_mode']
        user_inputs = data.get('user_inputs', {})
        current_cost = data.get('current_vehicle_annual_cost')
        budget_min = data.get('budget_min')
        budget_max = data.get('budget_max')
        
        if not state:
            return Response({"detail": "state is required", "requires_state": True}, status=status.HTTP_400_BAD_REQUEST)
            
        is_premium_mode = core_mode in ['cool_choice', 'true_value', 'full_picture']
        if is_premium_mode:
            if not request.user.is_authenticated or request.user.subscription_tier == SubscriptionTier.FREE:
                return Response({"locked": True, "detail": "Premium subscription required for this mode"}, status=status.HTTP_403_FORBIDDEN)
                
        vehicles = Vehicle.objects.filter(id__in=vehicle_ids)
        vehicle_map = {v.id: v for v in vehicles}
        
        results = []
        category_metrics_cache = {}
        for vid in vehicle_ids:
            vehicle = vehicle_map.get(vid)
            if not vehicle:
                continue
            res = calculate_core_rating(
                vehicle, state, priority_slider, core_mode, user_inputs,
                budget_min=budget_min, budget_max=budget_max,
                current_vehicle_annual_cost=current_cost,
                category_metrics_cache=category_metrics_cache
            )
            res['vehicle_id'] = str(vid)
            res['image_url'] = vehicle.image_url
            _strip_gated_for_anon(request, res)
            results.append(res)
            
        return Response({
            "meta": {
                "calculated_at": timezone.now().isoformat(),
                "is_estimate": True,
                "disclaimer": "All figures are estimates only. Actual costs will vary based on individual driving habits, insurer quotes, dealer pricing, and personal circumstances."
            },
            "data": results
        })

class VehicleDataUploadView(views.APIView):
    permission_classes = [IsAdminUser]  # SECURITY: catalogue overwrite - admins only (was AllowAny = unauthenticated overwrite)
    
    def post(self, request, *args, **kwargs):
        file_obj = request.FILES.get('file')
        if not file_obj:
            return Response({"error": "No file provided."}, status=status.HTTP_400_BAD_REQUEST)
            
        # Save file temporarily
        file_name = default_storage.save(file_obj.name, file_obj)
        file_path = default_storage.path(file_name)
        
        # Dispatch Celery task
        process_vehicle_excel_upload.delay(file_path)
        
        return Response({
            "message": "File accepted for processing in the background."
        }, status=status.HTTP_202_ACCEPTED)

class CoreSearchView(views.APIView):
    permission_classes = [AllowAny]
    
    def post(self, request):
        serializer = CoreSearchRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        
        target_band_slug = data['target_band_slug']
        fcai_category = data.get('fcai_category')
        state = data.get('state')
        priority_slider = data['priority_slider']
        core_mode = data['core_mode']
        user_inputs = data.get('user_inputs', {})
        current_cost = data.get('current_vehicle_annual_cost')
        budget_min = data.get('budget_min')
        budget_max = data.get('budget_max')
        
        if not state:
            return Response({"detail": "state is required", "requires_state": True}, status=status.HTTP_400_BAD_REQUEST)
            
        is_premium_mode = core_mode in ['cool_choice', 'true_value', 'full_picture']
        if is_premium_mode:
            if not request.user.is_authenticated or request.user.subscription_tier == SubscriptionTier.FREE:
                return Response({"locked": True, "detail": "Premium subscription required for this mode"}, status=status.HTTP_403_FORBIDDEN)
                
        # Filter initial vehicles
        queryset = Vehicle.objects.all()
        if fcai_category:
            queryset = queryset.filter(fcai_category__iexact=fcai_category)
            
        all_vehicles = list(queryset)
        
        # Deduplicate variants: keep only the cheapest variant per (brand, model, year)
        # for the requested core_mode and state to optimize processing.
        # Spec §4: B=Pure Cost, E=COOL Choice, C=True Value, F=Full Picture (cool_choice/true_value were swapped).
        mode_map = {
            'pure_cost': 'B',
            'cool_choice': 'E',
            'true_value': 'C',
            'full_picture': 'F'
        }
        mode_letter = mode_map.get(core_mode, 'B')
        
        cheapest_variants = {}
        for vehicle in all_vehicles:
            val = float('inf')
            try:
                # mode_data_by_state is expected to be a dict. 
                # Path: state -> mode_letter -> "mode_5yr_coo"
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
                    
        # Extract the optimized unique vehicles
        vehicles = [v for _, v in cheapest_variants.values()]
        
        results = []
        category_metrics_cache = {}
        for vehicle in vehicles:
            res = calculate_core_rating(
                vehicle, state, priority_slider, core_mode, user_inputs,
                budget_min=budget_min, budget_max=budget_max,
                current_vehicle_annual_cost=current_cost,
                category_metrics_cache=category_metrics_cache
            )
            
            # Filter by band
            if res.get('band_slug') != target_band_slug:
                continue
                
            # Filter by budget directly on the vehicle's driveaway price
            br = res.get('annual_cost_breakdown', {})
            driveaway = br.get('driveaway_price', 0)
            if budget_min is not None and driveaway < budget_min:
                continue
            if budget_max is not None and driveaway > budget_max:
                continue
                
            # Embed basic vehicle data
            res['vehicle_id'] = str(vehicle.id)
            res['brand'] = vehicle.brand
            res['model'] = vehicle.model
            res['year'] = vehicle.year
            res['variant'] = vehicle.variant
            res['image_url'] = vehicle.image_url
            
            results.append(res)
            
        # Sort by core rating (highest first)
        results.sort(key=lambda x: x.get('core_rating') or 0.0, reverse=True)
        
        # Take top 10
        top_10 = results[:10]
        for _r in top_10:
            _strip_gated_for_anon(request, _r)

        return Response({
            "meta": {
                "calculated_at": timezone.now().isoformat(),
                "total_matches": len(results),
                "is_estimate": True,
                "disclaimer": "All figures are estimates only. Actual costs will vary based on individual driving habits, insurer quotes, dealer pricing, and personal circumstances."
            },
            "data": top_10
        })
