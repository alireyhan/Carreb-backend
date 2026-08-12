import pyotp
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from apps.users.models import User, TwoFactorAuth, TwoFactorMethod

class TwoFactorAuthDisableTestCase(APITestCase):
    def setUp(self):
        # Create standard user
        self.user = User.objects.create_user(
            email='testuser@carreb.com.au',
            password='testpassword123'
        )
        self.url = reverse('2fa-disable')

    def test_disable_2fa_unauthenticated(self):
        response = self.client.post(self.url, {'totp_code': '123456'})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_disable_2fa_not_enabled(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(self.url, {'totp_code': '123456'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("2FA is not enabled", response.data['detail'])

    def test_disable_2fa_invalid_totp_code(self):
        self.client.force_authenticate(user=self.user)
        # Enable 2FA first
        secret = pyotp.random_base32()
        TwoFactorAuth.objects.create(
            user=self.user,
            method=TwoFactorMethod.AUTHENTICATOR_APP,
            totp_secret=secret,
            is_enabled=True,
            enrolled_at=timezone.now()
        )

        response = self.client.post(self.url, {'totp_code': '000000'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Invalid TOTP code", response.data['totp_code'])

    def test_disable_2fa_success(self):
        self.client.force_authenticate(user=self.user)
        # Enable 2FA first
        secret = pyotp.random_base32()
        tfa = TwoFactorAuth.objects.create(
            user=self.user,
            method=TwoFactorMethod.AUTHENTICATOR_APP,
            totp_secret=secret,
            is_enabled=True,
            enrolled_at=timezone.now()
        )

        # Generate a valid TOTP token using pyotp
        totp = pyotp.TOTP(secret)
        valid_code = totp.now()

        response = self.client.post(self.url, {'totp_code': valid_code})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['detail'], '2FA disabled successfully.')

        # Refresh from database and assert
        tfa.refresh_from_db()
        self.assertFalse(tfa.is_enabled)
        self.assertIsNone(tfa.totp_secret)
        self.assertIsNone(tfa.enrolled_at)

    def test_disable_2fa_on_email_change(self):
        # Enable 2FA
        secret = pyotp.random_base32()
        tfa = TwoFactorAuth.objects.create(
            user=self.user,
            method=TwoFactorMethod.AUTHENTICATOR_APP,
            totp_secret=secret,
            is_enabled=True,
            enrolled_at=timezone.now()
        )

        # Create verification token for email change
        from apps.users.models import EmailVerificationToken
        verification_token = EmailVerificationToken.objects.create(
            user=self.user,
            new_email='newemail@carreb.com.au',
            expires_at=timezone.now() + timezone.timedelta(hours=24)
        )

        # Call verify-email endpoint
        verify_url = reverse('verify-email')
        response = self.client.post(verify_url, {'token': str(verification_token.token)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Refresh from DB and verify email changed + 2FA disabled
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, 'newemail@carreb.com.au')

        tfa.refresh_from_db()
        self.assertFalse(tfa.is_enabled)
        self.assertIsNone(tfa.totp_secret)
        self.assertIsNone(tfa.enrolled_at)

    def test_social_login_2fa_challenge(self):
        # Enable 2FA first
        secret = pyotp.random_base32()
        TwoFactorAuth.objects.create(
            user=self.user,
            method=TwoFactorMethod.AUTHENTICATOR_APP,
            totp_secret=secret,
            is_enabled=True,
            enrolled_at=timezone.now()
        )

        # Call social-login endpoint. The Firebase id_token is now verified server-side and
        # the identity comes from the VERIFIED token, so we mock verify_id_token.
        from unittest.mock import patch
        url = reverse('login-social')
        with patch('firebase_admin.auth.verify_id_token') as mock_verify:
            mock_verify.return_value = {
                'uid': 'google-id-123', 'email': self.user.email, 'name': 'Test User',
                'firebase': {'sign_in_provider': 'google.com'},
            }
            response = self.client.post(url, {'id_token': 'valid-token'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Verify it returns a 2FA challenge response
        self.assertEqual(response.data['type'], 'challenge')
        self.assertIn('challenge_token', response.data)
