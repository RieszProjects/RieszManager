from django.urls import path

from . import views

urlpatterns = [
    path("organize", views.state),
    path("organize/archive", views.archive),
    path("organize/order", views.order),
    path("phrases", views.phrases),
]
