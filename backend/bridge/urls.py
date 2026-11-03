from django.urls import path, re_path

from . import transcribe, views

urlpatterns = [
    path("config", views.app_config),
    path("uploads", views.upload),
    path("transcribe", transcribe.transcribe),
    re_path(r"^(?P<path>sessions/[0-9a-f-]{36}/image|files/raw|uploads/[0-9a-f]{32})$", views.stream),
    re_path(r"^(?P<path>.+)$", views.proxy),
]
