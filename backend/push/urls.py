from django.urls import path

from . import views

urlpatterns = [
    path("push/config", views.config),
    path("push/subscribe", views.subscribe),
    path("push/unsubscribe", views.unsubscribe),
    path("push/test", views.test),
]
