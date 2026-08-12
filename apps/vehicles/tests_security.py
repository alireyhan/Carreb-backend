"""Regression tests for the vehicles security fixes:
  - VehicleDataUploadView is admin-only (was AllowAny = unauthenticated catalogue overwrite).
  - D24: anonymous requests receive no gated dollar fields (vehicle detail + core-rating).
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.vehicles.models import Vehicle
from apps.vehicles.views import _strip_gated_for_anon

User = get_user_model()


def make_vehicle(**kw):
    defaults = dict(
        brand='Toyota', model='RAV4', year=2026, variant='GX', body_style='SUV',
        powertrain_type='hybrid', fcai_category='Medium SUV', test_cycle='WLTP',
        resale_value_5yr_estimate=25000,
        servicing_5yr_estimate=4200,
        total_5yr_cost_of_ownership_by_state={'NSW': 45000},
        mode_data_by_state={'NSW': {'B': {'mode_5yr_coo': 45000}}},
    )
    defaults.update(kw)
    return Vehicle.objects.create(**defaults)


class UploadPermissionTests(APITestCase):
    def setUp(self):
        self.url = reverse('vehicle-upload')

    def test_anonymous_forbidden(self):
        resp = self.client.post(self.url, {}, format='multipart')
        self.assertIn(resp.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_non_admin_forbidden(self):
        user = User.objects.create_user(email='user@example.com', password='Str0ng-pw!')
        self.client.force_authenticate(user)
        resp = self.client.post(self.url, {}, format='multipart')
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_passes_permission(self):
        admin = User.objects.create_user(email='admin@example.com', password='Str0ng-pw!')
        admin.is_staff = True
        admin.save(update_fields=['is_staff'])
        self.client.force_authenticate(admin)
        # No file supplied -> 400 (permission PASSED; it is no longer a 403).
        resp = self.client.post(self.url, {}, format='multipart')
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)


class D24VehicleDetailGatingTests(APITestCase):
    GATED = [
        'resale_value_5yr_estimate', 'total_5yr_cost_of_ownership_by_state',
        'mode_data_by_state', 'servicing_5yr_estimate',
    ]

    def setUp(self):
        self.vehicle = make_vehicle()
        self.url = reverse('vehicle-detail', kwargs={'vehicle_id': self.vehicle.id})

    def test_anonymous_does_not_receive_gated_fields(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        for field in self.GATED:
            self.assertNotIn(field, resp.data)
        self.assertEqual(resp.data['brand'], 'Toyota')  # public identity still present

    def test_authenticated_receives_gated_fields(self):
        user = User.objects.create_user(email='member@example.com', password='Str0ng-pw!')
        self.client.force_authenticate(user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertIn('resale_value_5yr_estimate', resp.data)


class _FakeUser:
    def __init__(self, authed):
        self.is_authenticated = authed


class _FakeRequest:
    def __init__(self, authed):
        self.user = _FakeUser(authed)


class D24RatingStripTests(TestCase):
    def test_strips_est_cost_difference_for_anonymous(self):
        result = _strip_gated_for_anon(
            _FakeRequest(False), {'core_rating': 4.2, 'est_cost_difference': {'delta': 1200}}
        )
        self.assertNotIn('est_cost_difference', result)
        self.assertIn('core_rating', result)

    def test_keeps_est_cost_difference_for_authenticated(self):
        result = _strip_gated_for_anon(
            _FakeRequest(True), {'est_cost_difference': {'delta': 1200}}
        )
        self.assertIn('est_cost_difference', result)
