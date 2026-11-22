import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "riesz.settings")
django_asgi_app = get_asgi_application()

from channels.auth import AuthMiddlewareStack  # noqa: E402
from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402

from bridge.routing import node_websocket_urlpatterns, websocket_urlpatterns  # noqa: E402

from push.notifier import notifier  # noqa: E402

_browser_ws = AllowedHostsOriginValidator(AuthMiddlewareStack(URLRouter(websocket_urlpatterns)))
_node_ws = URLRouter(node_websocket_urlpatterns)


async def _websocket_router(scope, receive, send):
    # `/ws/node` is a non-browser CLI tunnel authenticated via Ed25519 challenge-response
    # (no cookies), so it must not require a browser `Origin` header.
    if scope.get("path") == "/ws/node":
        await _node_ws(scope, receive, send)
    else:
        await _browser_ws(scope, receive, send)


router = ProtocolTypeRouter({
    "http": django_asgi_app,
    "websocket": _websocket_router,
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
