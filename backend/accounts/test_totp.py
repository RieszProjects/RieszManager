"""Binding an authenticator while one is bound already is refused (it would replace it without a code)."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from unittest import mock

from django.test import RequestFactory, SimpleTestCase

from accounts import views


class TotpRebindTest(SimpleTestCase):
    def _request(self, path: str, body: dict | None = None):
        req = RequestFactory().post(path, data=json.dumps(body or {}), content_type="application/json")
        req.user = SimpleNamespace(is_authenticated=True, username="dev")
        req.session = {"totp_setup": {"secret": "JBSWY3DPEHPK3PXP", "t": time.time()}}
        req._dont_enforce_csrf_checks = True
        return req

    def test_setup_and_confirm_refused_when_already_enabled(self) -> None:
        with mock.patch("riesz.http._auth_problem", return_value=None), \
                mock.patch.object(views.services, "totp_device", return_value=object()), \
                mock.patch.object(views.services, "enable_totp") as enable:
            setup = views.totp_setup(self._request("/api/account/totp/setup"))
            confirm = views.totp_confirm(self._request("/api/account/totp/confirm", {"code": "123456"}))
        self.assertEqual(setup.status_code, 409)
        self.assertEqual(confirm.status_code, 409)
        enable.assert_not_called()

    def test_setup_allowed_when_not_enabled(self) -> None:
        with mock.patch("riesz.http._auth_problem", return_value=None), \
                mock.patch.object(views.services, "totp_device", return_value=None):
            resp = views.totp_setup(self._request("/api/account/totp/setup"))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("secret", json.loads(resp.content))

    def test_repeated_login_typos_never_lock_out(self) -> None:
        with mock.patch.object(views.time, "sleep"), \
                mock.patch.object(views.services, "record_attempt"), \
                mock.patch.object(views.services, "check_credentials", return_value=False):
            for _ in range(10):
                req = RequestFactory().post(
                    "/api/auth/login",
                    data=json.dumps({"username": "dev", "password": "wrong"}),
                    content_type="application/json",
                )
                req.user = SimpleNamespace(is_authenticated=False)
                req.session = {}
                req._dont_enforce_csrf_checks = True
                resp = views.auth_login(req)
                self.assertEqual(resp.status_code, 400)
