from django.urls import path

from . import views

urlpatterns = [
    path("auth/state", views.auth_state),
    path("auth/login", views.auth_login),
    path("auth/totp", views.auth_totp),
    path("auth/logout", views.auth_logout),
    path("account/security", views.security_overview),
    path("account/totp/setup", views.totp_setup),
    path("account/totp/confirm", views.totp_confirm),
    path("account/totp/disable", views.totp_disable),
    path("account/recovery/regenerate", views.recovery_regenerate),
    path("account/sessions/revoke", views.revoke_sessions),
]
