"""Regression tests for the social-login account-takeover fix.

Before the fix, the endpoint trusted a client-supplied `email`, so anyone could log in as
any user. Now the Firebase `id_token` is verified server-side and the identity is taken from
the VERIFIED token.
"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

User = get_user_model()


class SocialLoginSecurityTests(APITestCase):
    def setUp(self):
        self.url = reverse('login-social')

    def test_missing_id_token_is_rejected(self):
        resp = self.client.post(
            self.url, {'provider': 'google', 'email': 'attacker@example.com'}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    @patch('firebase_admin.auth.verify_id_token')
    def test_forged_token_rejected_and_no_account_created(self, mock_verify):
        mock_verify.side_effect = Exception('invalid token')
        resp = self.client.post(
            self.url, {'id_token': 'forged', 'email': 'victim@example.com'}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(User.objects.filter(email='victim@example.com').exists())

    @patch('firebase_admin.auth.verify_id_token')
    def test_identity_taken_from_verified_token_not_request_body(self, mock_verify):
        # Attacker supplies the victim's email, but the VERIFIED token belongs to someone else.
        mock_verify.return_value = {
            'uid': 'firebase-uid-123',
            'email': 'real.user@example.com',
            'name': 'Real User',
            'firebase': {'sign_in_provider': 'google.com'},
        }
        resp = self.client.post(
            self.url, {'id_token': 'valid', 'email': 'victim@example.com'}, format='json'
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertTrue(User.objects.filter(email='real.user@example.com').exists())
        self.assertFalse(User.objects.filter(email='victim@example.com').exists())
