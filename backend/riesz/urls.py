from django.http import JsonResponse
from django.urls import include, path


def healthz(request):
    return JsonResponse({"ok": True})


urlpatterns = [
    path("api/healthz", healthz),
    path("api/", include("accounts.urls")),
    path("api/", include("organize.urls")),
    path("api/", include("bridge.urls")),
]
