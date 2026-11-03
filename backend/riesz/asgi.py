import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "riesz.settings")
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from bridge.routing import websocket_urlpatterns  # noqa: E402

from push.notifier import notifier  # noqa: E402

router = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns))),
})


async def application(scope, receive, send):
    """The Channels router, plus the ASGI lifespan that runs the push notifier."""
    if scope["type"] != "lifespan":
        await router(scope, receive, send)
        return
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            notifier.start()
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            await notifier.stop()
            await send({"type": "lifespan.shutdown.complete"})
            return
