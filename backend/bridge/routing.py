from django.urls import path

from .consumers import BrowserConsumer, EventsConsumer, NodeConsumer
from .live_transcribe import TranscribeConsumer

websocket_urlpatterns = [
    path("ws/events", EventsConsumer.as_asgi()),
    path("ws/transcribe", TranscribeConsumer.as_asgi()),
    path("ws/browser", BrowserConsumer.as_asgi()),
]

node_websocket_urlpatterns = [
    path("ws/node", NodeConsumer.as_asgi()),
]
