import re
from rest_framework import serializers
from .models import DeletionReason, TwoFactorMethod, User

def validate_password_complexity(value):
    if len(value) < 8:
        raise serializers.ValidationError("Password must be at least 8 characters long.")
    if not re.search(r'\d', value):
        raise serializers.ValidationError("Password must contain at least one number.")
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', value):
        raise serializers.ValidationError("Password must contain at least one special character.")
    return value

class SignupStep1Serializer(serializers.Serializer):
    email = serializers.EmailField()

class SignupStep2Serializer(serializers.Serializer):
    email = serializers.EmailField()
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150)
    mobile_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, validators=[validate_password_complexity])
    environment = serializers.ChoiceField(choices=[('dev', 'dev'), ('production', 'production')], default='dev', required=False)

class EmailVerificationSerializer(serializers.Serializer):
    token = serializers.UUIDField()

class LoginSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)

class Verify2FASerializer(serializers.Serializer):
    challenge_token = serializers.UUIDField()
    totp_code = serializers.CharField(max_length=6, min_length=6)

class SocialLoginSerializer(serializers.Serializer):
    # SECURITY: the caller sends the provider's Firebase ID token; the server VERIFIES it
    # and derives the identity from the verified token. A client-supplied email/provider_id
    # is never trusted (that was an account-takeover bypass). Frontend already sends id_token.
    id_token = serializers.CharField()
    provider = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        from firebase_admin import auth as firebase_auth
        try:
            decoded = firebase_auth.verify_id_token(attrs["id_token"])
        except Exception:
            raise serializers.ValidationError(
                {"id_token": "Invalid or expired social login token."}
            )
        email = decoded.get("email")
        if not email:
            raise serializers.ValidationError({"id_token": "Token did not contain an email."})
        name = (decoded.get("name") or "").strip()
        first, _, last = name.partition(" ")
        # Return exactly the kwargs social_login() expects - all from the VERIFIED token.
        return {
            "provider": (decoded.get("firebase", {}) or {}).get("sign_in_provider")
                        or attrs.get("provider") or "firebase",
            "provider_id": decoded.get("uid"),
            "email": email,
            "first_name": decoded.get("given_name") or first or "",
            "last_name": decoded.get("family_name") or last or "",
        }

class Confirm2FASerializer(serializers.Serializer):
    totp_code = serializers.CharField(max_length=6, min_length=6)

class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()
    environment = serializers.ChoiceField(choices=[('dev', 'dev'), ('production', 'production')], default='dev', required=False)

class PasswordResetVerifySerializer(serializers.Serializer):
    token = serializers.UUIDField()
    new_password = serializers.CharField(write_only=True, validators=[validate_password_complexity])

class AccountDeletionRequestSerializer(serializers.Serializer):
    password = serializers.CharField(write_only=True)
    reason = serializers.ChoiceField(choices=DeletionReason.choices)

class UserProfileSerializer(serializers.ModelSerializer):
    is_2fa_enabled = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = ['id', 'first_name', 'last_name', 'email', 'mobile_number', 'profile_picture', 'profile_photo_url', 'subscription_tier', 'email_verified', 'is_2fa_enabled']
        read_only_fields = ['id', 'subscription_tier', 'email_verified', 'is_2fa_enabled']

    def get_is_2fa_enabled(self, obj):
        return hasattr(obj, 'two_factor_auth') and obj.two_factor_auth.is_enabled

class UserProfileUpdateSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email', 'mobile_number', 'profile_picture']

from .models import NotificationPreference

class NotificationPreferenceSerializer(serializers.ModelSerializer):
    class Meta:
        model = NotificationPreference
        exclude = ('id', 'user', 'created_at', 'updated_at')

from .models import FCMDevice

class FCMDeviceSerializer(serializers.ModelSerializer):
    class Meta:
        model = FCMDevice
        fields = ['device_id', 'fcm_token', 'device_type']
