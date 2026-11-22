from django.urls import path, re_path

from . import transcribe, viewer, views

urlpatterns = [
    path("config", views.app_config),
    path("files/view-token", viewer.view_token),
    re_path(r"^view/(?P<token>[^/]+)/(?P<path>.*)$", viewer.serve),
    path("uploads", views.upload),
    path("transcribe", transcribe.transcribe),
    path("nodes/register", views.node_register),
    re_path(r"^nodes/transfer/(?P<token>[A-Za-z0-9_-]{16,128})$", views.node_transfer),
    re_path(r"^(?P<path>sessions/[0-9a-f-]{36}/image|files/raw|uploads/[0-9a-f]{32})$", views.stream),
    re_path(r"^(?P<path>.+)$", views.proxy),
]
