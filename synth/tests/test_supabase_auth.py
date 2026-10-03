import json
import os
import unittest
from unittest.mock import patch, MagicMock
from uuid import UUID

from fastapi import HTTPException
from synth.server.supabase_auth import allowed_domains, permitted_email, validate_user, configuration, verified_user, authenticate_supabase
from synth.server.db import authenticate


USER = {"id": "34bf7e30-90bc-486a-92d4-a71cb99dcce6", "email": "alex@example.com",
        "email_confirmed_at": "2025-01-01T00:00:00Z", "is_anonymous": False}


class SupabaseAuthTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"AUTH_MODE": "supabase", "ALLOWED_DOMAINS": "example.com",
            "SUPABASE_URL": "https://exampleproject.supabase.co", "SUPABASE_PUBLISHABLE_KEY": "sb_publishable_test"})
        self.env.start(); self.addCleanup(self.env.stop)

    def status(self, code, function, *args):
        with self.assertRaises(HTTPException) as caught:
            function(*args)
        self.assertEqual(caught.exception.status_code, code)

    def test_exact_domain_policy_rejects_suffixes_subdomains_and_malformed_email(self):
        for email in ["a@example.com.evil.com", "a@evil-example.com", "a@sub.example.com", "a@evil.com", "a@b@example.com", " a@example.com", "a\n@example.com", "@example.com"]:
            self.assertFalse(permitted_email(email), email)
        self.assertTrue(permitted_email("Alex@EXAMPLE.COM"))

    def test_configurable_domains_and_empty_policy_fail_closed(self):
        with patch.dict(os.environ, {"ALLOWED_DOMAINS": "example.com, example.org"}):
            self.assertTrue(permitted_email("a@example.org"))
        for domains in ["", "*", "@example.com", "example.com/evil", "a..com"]:
            with patch.dict(os.environ, {"ALLOWED_DOMAINS": domains}):
                self.status(503, allowed_domains)

    def test_requires_verified_active_nonanonymous_identity(self):
        self.assertEqual(validate_user(USER)["id"], UUID(USER["id"]))
        for changes in [{"email_confirmed_at": None}, {"email_confirmed_at": "not-a-date"},
            {"email_confirmed_at": "2999-01-01T00:00:00Z"}, {"is_anonymous": True},
            {"is_anonymous": None}, {"id": "invalid"}, {"deleted_at": "2025-01-01"},
            {"banned_until": "2999-01-01T00:00:00Z"}]:
            self.status(401, validate_user, {**USER, **changes})
        self.status(403, validate_user, {**USER, "email": "a@other.org", "user_metadata": {"email": "a@example.com"}})

    def test_configuration_never_exposes_a_service_secret_or_arbitrary_destination(self):
        for key in ["sb_secret_test", "", "invalid"]:
            with patch.dict(os.environ, {"SUPABASE_PUBLISHABLE_KEY": key}):
                self.status(503, configuration)
        for url in ["http://exampleproject.supabase.co", "https://evil.com", "https://exampleproject.supabase.co@evil.com"]:
            with patch.dict(os.environ, {"SUPABASE_URL": url}):
                self.status(503, configuration)

    def test_verification_uses_supabase_user_endpoint_and_never_decodes_untrusted_claims(self):
        response = MagicMock(); response.read.return_value = json.dumps(USER).encode()
        opener = MagicMock(); opener.open.return_value.__enter__.return_value = response
        with patch("synth.server.supabase_auth.urllib.request.build_opener", return_value=opener):
            self.assertEqual(verified_user("untrusted.token.payload")["email"], "alex@example.com")
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "https://exampleproject.supabase.co/auth/v1/user")
        self.assertEqual(request.headers["Authorization"], "Bearer untrusted.token.payload")

    def test_auth_outage_never_falls_back_to_legacy_human_keys(self):
        db = MagicMock(); db.execute.return_value.fetchone.return_value = {"kind": "user"}
        with patch("synth.server.db.authenticate_supabase") as supabase:
            self.status(401, authenticate, db, "legacy-human-key")
            supabase.assert_not_called()
        db.execute.return_value.fetchone.return_value = None
        with patch("synth.server.db.authenticate_supabase", side_effect=HTTPException(503, "offline")):
            self.status(503, authenticate, db, "auth-token")

    def test_machine_keys_keep_their_existing_folder_scopes(self):
        db = MagicMock(); actor = {"kind": "machine", "key_id": "k"}
        db.execute.return_value.fetchone.return_value = actor
        self.assertEqual(authenticate(db, "integration-key"), actor)

    def test_preserves_explicit_mapping_and_honors_local_revocation(self):
        db = MagicMock(); actor = {"id": UUID("6b45c3e5-19bb-435c-91a2-0501fab6d3e7"), "name": "Alex", "kind": "user", "revoked_at": None}
        db.execute.return_value.fetchone.return_value = actor
        with patch("synth.server.supabase_auth.verified_user", return_value={"id": UUID(USER["id"]), "email": USER["email"]}):
            self.assertEqual(authenticate_supabase(db, "token")["id"], actor["id"])
            actor["revoked_at"] = "2025-01-01"
            self.status(403, authenticate_supabase, db, "token")


if __name__ == "__main__":
    unittest.main()
