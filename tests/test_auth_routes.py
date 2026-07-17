import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import backend.api.routes.auth as auth
import backend.infra.auth as infra_auth
from backend.auth.access import create_access_token
from backend.infra.auth import get_current_user, resolve_role, verify_password
from fastapi import HTTPException


class AuthRouteConcurrencyTests(unittest.TestCase):
    def test_sync_storage_handlers_are_dispatched_to_fastapi_threadpool(self):
        endpoints = {route.path: route.endpoint for route in auth.router.routes}

        for path in (
            "/auth/register",
            "/auth/login",
            "/auth/refresh",
            "/auth/logout",
            "/auth/logout-all",
        ):
            with self.subTest(path=path):
                self.assertFalse(
                    inspect.iscoroutinefunction(endpoints[path]),
                    "sync SQLAlchemy and PBKDF2 must not run on the event loop",
                )

    def test_hmac_access_token_round_trips_without_ecdsa_dependency(self):
        user = SimpleNamespace(username="alice", role="user")
        db = Mock()
        db.query.return_value.filter.return_value.first.return_value = user

        token = create_access_token("alice", "user")

        self.assertIs(get_current_user(token, db), user)

        with self.assertRaises(HTTPException) as raised:
            get_current_user(f"{token}tampered", db)
        self.assertEqual(401, raised.exception.status_code)

    def test_legacy_bcrypt_hashes_remain_verifiable_with_bcrypt_five(self):
        fixtures = (
            "$2b$04$abcdefghijklmnopqrstuuHQRMHradWrjjbPcbpK37RVvfSYCXoLy",
            "$bcrypt-sha256$2b,4$abcdefghijklmnopqrstuu$"
            "FQ2IbYX6zn7VyXVLeDueHXUwEtfuttq",
            "$bcrypt-sha256$v=2,t=2b,r=4$abcdefghijklmnopqrstuu$"
            "Py7aKyeEZmxD.5u4.QZnUu6X5r6LlMS",
        )

        for password_hash in fixtures:
            with self.subTest(password_hash=password_hash[:24]):
                self.assertTrue(verify_password("legacy-password", password_hash))
                self.assertFalse(verify_password("wrong-password", password_hash))

    def test_admin_registration_is_disabled_without_a_strong_matching_invite(self):
        with patch.object(infra_auth, "ADMIN_INVITE_CODE", ""):
            with self.assertRaises(HTTPException):
                resolve_role("admin", "public-placeholder")

        with patch.object(infra_auth, "ADMIN_INVITE_CODE", "a" * 40):
            self.assertEqual("admin", resolve_role("admin", "a" * 40))
            with self.assertRaises(HTTPException):
                resolve_role("admin", "b" * 40)
