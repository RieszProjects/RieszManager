from django.urls import path

from .consumers import EventsConsumer
from .live_transcribe import TranscribeConsumer

websocket_urlpatterns = [
    path("ws/events", EventsConsumer.as_asgi()),
    path("ws/transcribe", TranscribeConsumer.as_asgi()),
]
