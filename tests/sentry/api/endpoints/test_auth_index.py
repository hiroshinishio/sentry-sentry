from datetime import datetime, timedelta, timezone
from unittest import mock
from urllib.parse import urlencode

from sentry.models.authenticator import Authenticator
from sentry.models.authidentity import AuthIdentity
from sentry.models.authprovider import AuthProvider
from sentry.testutils.cases import APITestCase, AuthProviderTestCase
from sentry.testutils.helpers import with_feature
from sentry.testutils.silo import control_silo_test
from sentry.utils.auth import SSO_EXPIRY_TIME, SsoSession


def create_authenticator(user) -> None:
    Authenticator.objects.create(
        type=3,  # u2f
        user=user,
        config={
            "devices": [
                {
                    "binding": {
                        "publicKey": "aowekroawker",
                        "keyHandle": "devicekeyhandle",
                        "appId": "https://testserver/auth/2fa/u2fappid.json",
                    },
                    "name": "Amused Beetle",
                    "ts": 1512505334,
                },
                {
                    "binding": {
                        "publicKey": "publickey",
                        "keyHandle": "aowerkoweraowerkkro",
                        "appId": "https://testserver/auth/2fa/u2fappid.json",
                    },
                    "name": "Sentry",
                    "ts": 1512505334,
                },
            ]
        },
    )


@control_silo_test
class AuthDetailsEndpointTest(APITestCase):
    path = "/api/0/auth/"

    def test_logged_in(self):
        user = self.create_user("foo@example.com")
        self.login_as(user)
        response = self.client.get(self.path)
        assert response.status_code == 200
        assert response.data["id"] == str(user.id)

    def test_logged_out(self):
        response = self.client.get(self.path)
        assert response.status_code == 400


@control_silo_test
class AuthLoginEndpointTest(APITestCase):
    path = "/api/0/auth/"

    def test_valid_password(self):
        user = self.create_user("foo@example.com")
        response = self.client.post(
            self.path,
            HTTP_AUTHORIZATION=self.create_basic_auth_header(user.username, "admin"),
        )
        assert response.status_code == 200
        assert response.data["id"] == str(user.id)

    def test_invalid_password(self):
        user = self.create_user("foo@example.com")
        response = self.client.post(
            self.path,
            HTTP_AUTHORIZATION=self.create_basic_auth_header(user.username, "foobar"),
        )
        assert response.status_code == 401


@control_silo_test
class AuthVerifyEndpointTest(APITestCase):
    path = "/api/0/auth/"

    @mock.patch("sentry.api.endpoints.auth_index.metrics")
    def test_valid_password(self, mock_metrics):
        user = self.create_user("foo@example.com")
        self.login_as(user)
        response = self.client.put(self.path, data={"password": "admin"})
        assert response.status_code == 200
        assert response.data["id"] == str(user.id)
        mock_metrics.incr.assert_any_call(
            "auth.password.success", sample_rate=1.0, skip_internal=False
        )

    @mock.patch("sentry.api.endpoints.auth_index.metrics")
    def test_invalid_password(self, mock_metrics):
        user = self.create_user("foo@example.com")
        self.login_as(user)
        response = self.client.put(self.path, data={"password": "foobar"})
        assert response.status_code == 403
        assert (
            mock.call("auth.password.success", sample_rate=1.0, skip_internal=False)
            not in mock_metrics.incr.call_args_list
        )

    def test_no_password_no_u2f(self):
        user = self.create_user("foo@example.com")
        self.login_as(user)
        response = self.client.put(self.path, data={})
        assert response.status_code == 400

    @mock.patch("sentry.api.endpoints.auth_index.metrics")
    @mock.patch("sentry.auth.authenticators.U2fInterface.is_available", return_value=True)
    @mock.patch("sentry.auth.authenticators.U2fInterface.validate_response", return_value=True)
    def test_valid_password_u2f(self, validate_response, is_available, mock_metrics):
        user = self.create_user("foo@example.com")
        self.org = self.create_organization(owner=user, name="foo")
        self.login_as(user)
        create_authenticator(user)
        response = self.client.put(
            self.path,
            user=user,
            data={
                "password": "admin",
                "challenge": """{"challenge":"challenge"}""",
                "response": """{"response":"response"}""",
            },
        )
        assert response.status_code == 200
        assert validate_response.call_count == 1
        assert {"challenge": "challenge"} in validate_response.call_args[0]
        assert {"response": "response"} in validate_response.call_args[0]
        mock_metrics.incr.assert_any_call("auth.2fa.success", sample_rate=1.0, skip_internal=False)

    @mock.patch("sentry.api.endpoints.auth_index.metrics")
    @mock.patch("sentry.auth.authenticators.U2fInterface.is_available", return_value=True)
    @mock.patch("sentry.auth.authenticators.U2fInterface.validate_response", return_value=False)
    def test_invalid_password_u2f(self, validate_response, is_available, mock_metrics):
        user = self.create_user("foo@example.com")
        self.org = self.create_organization(owner=user, name="foo")
        self.login_as(user)
        create_authenticator(user)
        response = self.client.put(
            self.path,
            user=user,
            data={
                "password": "admin",
                "challenge": """{"challenge":"challenge"}""",
                "response": """{"response":"response"}""",
            },
        )
        assert response.status_code == 403
        assert validate_response.call_count == 1
        assert {"challenge": "challenge"} in validate_response.call_args[0]
        assert {"response": "response"} in validate_response.call_args[0]
        assert (
            mock.call("auth.2fa.success", sample_rate=1.0, skip_internal=False)
            not in mock_metrics.incr.call_args_list
        )


@control_silo_test
class AuthVerifyEndpointSuperuserTest(AuthProviderTestCase, APITestCase):
    path = "/api/0/auth/"

    @with_feature("organizations:u2f-superuser-form")
    @mock.patch("sentry.auth.authenticators.U2fInterface.is_available", return_value=True)
    @mock.patch("sentry.auth.authenticators.U2fInterface.validate_response", return_value=True)
    def test_superuser_sso_user_no_password_saas_product(self, validate_response, is_available):
        from sentry.auth.superuser import COOKIE_NAME, Superuser

        with self.settings(SENTRY_SELF_HOSTED=False):
            org_provider = AuthProvider.objects.create(
                organization_id=self.organization.id, provider="dummy"
            )

            user = self.create_user("foo@example.com", is_superuser=True)

            create_authenticator(user)

            user.update(password="")

            AuthIdentity.objects.create(user=user, auth_provider=org_provider)

            with mock.patch.object(Superuser, "org_id", self.organization.id):
                with self.settings(SENTRY_SELF_HOSTED=False):
                    self.login_as(user, organization_id=self.organization.id)
                    response = self.client.put(
                        self.path,
                        data={
                            "isSuperuserModal": True,
                            "challenge": """{"challenge":"challenge"}""",
                            "response": """{"response":"response"}""",
                            "superuserAccessCategory": "for_unit_test",
                            "superuserReason": "for testing",
                        },
                    )
                    assert response.status_code == 200
                    assert COOKIE_NAME in response.cookies

    @with_feature("organizations:u2f-superuser-form")
    @mock.patch("sentry.auth.authenticators.U2fInterface.is_available", return_value=True)
    @mock.patch("sentry.auth.authenticators.U2fInterface.validate_response", return_value=False)
    def test_superuser_expired_sso_user_no_password_saas_product(
        self, validate_response, is_available
    ):
        from sentry.auth.superuser import COOKIE_NAME, Superuser

        with self.settings(SENTRY_SELF_HOSTED=False):
            org_provider = AuthProvider.objects.create(
                organization_id=self.organization.id, provider="dummy"
            )

            user = self.create_user("foo@example.com", is_superuser=True)

            create_authenticator(user)

            user.update(password="")

            AuthIdentity.objects.create(user=user, auth_provider=org_provider)

            with mock.patch.object(Superuser, "org_id", self.organization.id):
                with self.settings(SENTRY_SELF_HOSTED=False):
                    self.login_as(user, organization_id=self.organization.id)

                    sso_session_expired = SsoSession(
                        self.organization.id,
                        datetime.now(tz=timezone.utc) - SSO_EXPIRY_TIME - timedelta(hours=1),
                    )
                    self.session[sso_session_expired.session_key] = sso_session_expired.to_dict()
                    self.save_session()

                    response = self.client.put(
                        self.path,
                        data={
                            "isSuperuserModal": True,
                            "challenge": """{"challenge":"challenge"}""",
                            "response": """{"response":"response"}""",
                            "superuserAccessCategory": "for_unit_test",
                            "superuserReason": "for testing",
                        },
                    )
                    # status code of 401 means invalid SSO session
                    assert response.status_code == 401
                    assert response.data == {
                        "detail": {
                            "code": "sso-required",
                            "extra": {"loginUrl": f"/auth/login/{self.organization.slug}/"},
                            "message": "Must login via SSO",
                        }
                    }
                    assert COOKIE_NAME not in response.cookies

    @with_feature("organizations:u2f-superuser-form")
    @mock.patch("sentry.auth.authenticators.U2fInterface.is_available", return_value=True)
    @mock.patch("sentry.auth.authenticators.U2fInterface.validate_response", return_value=False)
    def test_superuser_expired_sso_user_no_password_saas_product_customer_domain(
        self, validate_response, is_available
    ):
        from sentry.auth.superuser import COOKIE_NAME, Superuser

        with self.settings(SENTRY_SELF_HOSTED=False):
            # An organization that a superuser is not a member of, but will try to access.
            other_org = self.create_organization(name="other_org")

            org_provider = AuthProvider.objects.create(
                organization_id=self.organization.id, provider="dummy"
            )

            user = self.create_user("foo@example.com", is_superuser=True)

            create_authenticator(user)

            user.update(password="")

            AuthIdentity.objects.create(user=user, auth_provider=org_provider)

            with mock.patch.object(Superuser, "org_id", self.organization.id):
                with self.settings(SENTRY_SELF_HOSTED=False):
                    self.login_as(user, organization_id=self.organization.id)

                    sso_session_expired = SsoSession(
                        self.organization.id,
                        datetime.now(tz=timezone.utc) - SSO_EXPIRY_TIME - timedelta(hours=1),
                    )
                    self.session[sso_session_expired.session_key] = sso_session_expired.to_dict()
                    self.save_session()

                    referrer = f"http://{other_org.slug}.testserver/issues/"

                    response = self.client.put(
                        self.path,
                        data={
                            "isSuperuserModal": True,
                            "challenge": """{"challenge":"challenge"}""",
                            "response": """{"response":"response"}""",
                            "superuserAccessCategory": "for_unit_test",
                            "superuserReason": "for testing",
                        },
                        SERVER_NAME=f"{other_org.slug}.testserver",
                        HTTP_REFERER=referrer,
                    )
                    # status code of 401 means invalid SSO session
                    assert response.status_code == 401
                    query_string = urlencode({"next": referrer})
                    assert response.data == {
                        "detail": {
                            "code": "sso-required",
                            "extra": {
                                "loginUrl": f"http://{self.organization.slug}.testserver/auth/login/{self.organization.slug}/?{query_string}"
                            },
                            "message": "Must login via SSO",
                        }
                    }
                    assert COOKIE_NAME not in response.cookies

    @with_feature("organizations:u2f-superuser-form")
    def test_superuser_sso_user_no_u2f_saas_product(self):
        from sentry.auth.superuser import Superuser

        with self.settings(SENTRY_SELF_HOSTED=False):
            org_provider = AuthProvider.objects.create(
                organization_id=self.organization.id, provider="dummy"
            )

            user = self.create_user("foo@example.com", is_superuser=True)

            create_authenticator(user)

            AuthIdentity.objects.create(user=user, auth_provider=org_provider)

            with mock.patch.object(Superuser, "org_id", self.organization.id):
                with self.settings(SENTRY_SELF_HOSTED=False):
                    self.login_as(user, organization_id=self.organization.id)
                    response = self.client.put(
                        self.path,
                        data={
                            "isSuperuserModal": True,
                            "superuserReason": "for testing",
                        },
                    )
                    assert response.status_code == 403

    @with_feature("organizations:u2f-superuser-form")
    @mock.patch("sentry.auth.authenticators.U2fInterface.is_available", return_value=True)
    @mock.patch("sentry.auth.authenticators.U2fInterface.validate_response", return_value=True)
    def test_superuser_sso_user_has_password_saas_product(self, validate_response, is_available):
        from sentry.auth.superuser import COOKIE_NAME, Superuser

        with self.settings(
            SENTRY_SELF_HOSTED=False, VALIDATE_SUPERUSER_ACCESS_CATEGORY_AND_REASON=True
        ):
            org_provider = AuthProvider.objects.create(
                organization_id=self.organization.id, provider="dummy"
            )

            user = self.create_user("foo@example.com", is_superuser=True)

            create_authenticator(user)

            AuthIdentity.objects.create(user=user, auth_provider=org_provider)

            with mock.patch.object(Superuser, "org_id", self.organization.id):
                with self.settings(SENTRY_SELF_HOSTED=False):
                    self.login_as(user, organization_id=self.organization.id)
                    response = self.client.put(
                        self.path,
                        data={
                            "isSuperuserModal": True,
                            "challenge": """{"challenge":"challenge"}""",
                            "response": """{"response":"response"}""",
                            "superuserAccessCategory": "for_unit_test",
                            "superuserReason": "for testing",
                        },
                    )
                    assert response.status_code == 200
                    assert COOKIE_NAME in response.cookies

    @with_feature("organizations:u2f-superuser-form")
    @mock.patch("sentry.auth.authenticators.U2fInterface.is_available", return_value=True)
    @mock.patch("sentry.auth.authenticators.U2fInterface.validate_response", return_value=True)
    def test_superuser_no_sso_user_has_password_saas_product(self, validate_response, is_available):
        from sentry.auth.superuser import Superuser

        with self.settings(
            SENTRY_SELF_HOSTED=False, VALIDATE_SUPERUSER_ACCESS_CATEGORY_AND_REASON=True
        ):
            AuthProvider.objects.create(organization_id=self.organization.id, provider="dummy")

            user = self.create_user("foo@example.com", is_superuser=True)

            create_authenticator(user)

            with mock.patch.object(Superuser, "org_id", self.organization.id):
                self.login_as(user)
                response = self.client.put(
                    self.path,
                    data={
                        "password": "admin",
                        "isSuperuserModal": True,
                        "superuserAccessCategory": "for_unit_test",
                        "superuserReason": "for testing",
                    },
                )
                assert response.status_code == 401

    @with_feature("organizations:u2f-superuser-form")
    def test_superuser_no_sso_user_has_password_self_hosted(self):
        from sentry.auth.superuser import Superuser

        AuthProvider.objects.create(organization_id=self.organization.id, provider="dummy")

        user = self.create_user("foo@example.com", is_superuser=True)

        with mock.patch.object(Superuser, "org_id", None):
            with self.settings(SENTRY_SELF_HOSTED=True):
                self.login_as(user)
                response = self.client.put(
                    self.path,
                    data={
                        "password": "admin",
                        "isSuperuserModal": True,
                    },
                )
                assert response.status_code == 200

    @with_feature("organizations:u2f-superuser-form")
    def test_superuser_no_sso_self_hosted_no_password_or_u2f(self):
        from sentry.auth.superuser import Superuser

        AuthProvider.objects.create(organization_id=self.organization.id, provider="dummy")

        user = self.create_user("foo@example.com", is_superuser=True)

        with mock.patch.object(Superuser, "org_id", None):
            with self.settings(SENTRY_SELF_HOSTED=True):
                self.login_as(user)
                response = self.client.put(
                    self.path,
                    data={
                        "isSuperuserModal": True,
                    },
                )
                assert response.status_code == 403

    @with_feature("organizations:u2f-superuser-form")
    def test_superuser_no_sso_user_has_password_su_form_off_saas(self):
        from sentry.auth.superuser import Superuser

        with self.settings(SENTRY_SELF_HOSTED=False):
            AuthProvider.objects.create(organization_id=self.organization.id, provider="dummy")

            user = self.create_user("foo@example.com", is_superuser=True)

            with mock.patch.object(Superuser, "org_id", None):
                with self.settings(SENTRY_SELF_HOSTED=True):
                    self.login_as(user)
                    response = self.client.put(
                        self.path,
                        data={
                            "password": "admin",
                            "isSuperuserModal": True,
                        },
                    )
                    assert response.status_code == 200

    @with_feature("organizations:u2f-superuser-form")
    def test_superuser_no_sso_su_form_off_no_password_or_u2f_saas(self):
        from sentry.auth.superuser import Superuser

        with self.settings(SENTRY_SELF_HOSTED=False):
            AuthProvider.objects.create(organization_id=self.organization.id, provider="dummy")

            user = self.create_user("foo@example.com", is_superuser=True)

            with mock.patch.object(Superuser, "org_id", self.organization.id):
                self.login_as(user)
                response = self.client.put(
                    self.path,
                    data={
                        "isSuperuserModal": True,
                    },
                )
                assert response.status_code == 403

    @with_feature("organizations:u2f-superuser-form")
    def test_superuser_no_sso_user_has_password_su_form_on_self_hosted(self):
        from sentry.auth.superuser import Superuser

        with self.settings(
            SENTRY_SELF_HOSTED=True, VALIDATE_SUPERUSER_ACCESS_CATEGORY_AND_REASON=True
        ):
            AuthProvider.objects.create(organization_id=self.organization.id, provider="dummy")

            user = self.create_user("foo@example.com", is_superuser=True)

            with mock.patch.object(Superuser, "org_id", None):
                with self.settings(SENTRY_SELF_HOSTED=True):
                    self.login_as(user)
                    response = self.client.put(
                        self.path,
                        data={
                            "password": "admin",
                            "isSuperuserModal": True,
                        },
                    )
                    assert response.status_code == 200

    @with_feature("organizations:u2f-superuser-form")
    def test_superuser_no_sso_su_form_on_no_password_or_u2f_self_hosted(self):
        from sentry.auth.superuser import Superuser

        with self.settings(
            SENTRY_SELF_HOSTED=True, VALIDATE_SUPERUSER_ACCESS_CATEGORY_AND_REASON=True
        ):
            AuthProvider.objects.create(organization_id=self.organization.id, provider="dummy")

            user = self.create_user("foo@example.com", is_superuser=True)

            with mock.patch.object(Superuser, "org_id", None):
                self.login_as(user)
                response = self.client.put(
                    self.path,
                    data={
                        "isSuperuserModal": True,
                    },
                )
                assert response.status_code == 403

    @with_feature("organizations:u2f-superuser-form")
    def test_superuser_no_sso_with_referrer(self):
        from sentry.auth.superuser import Superuser

        user = self.create_user("foo@example.com", is_superuser=True)

        with mock.patch.object(Superuser, "org_id", self.organization.id):
            self.login_as(user)
            response = self.client.put(
                self.path,
                HTTP_REFERER="http://testserver/bar",
                data={
                    "password": "admin",
                    "isSuperuserModal": True,
                    "superuserAccessCategory": "for_unit_test",
                    "superuserReason": "for testing",
                },
            )
            assert response.status_code == 401
            assert self.client.session["_next"] == "http://testserver/bar"

    @with_feature("organizations:u2f-superuser-form")
    def test_superuser_no_sso_with_bad_referrer(self):
        from sentry.auth.superuser import Superuser

        user = self.create_user("foo@example.com", is_superuser=True)

        with mock.patch.object(Superuser, "org_id", self.organization.id):
            self.login_as(user)
            response = self.client.put(
                self.path,
                HTTP_REFERER="http://hacktheplanet/bar",
                data={
                    "password": "admin",
                    "isSuperuserModal": True,
                    "superuserAccessCategory": "for_unit_test",
                    "superuserReason": "for testing",
                },
            )
            assert response.status_code == 401
            assert self.client.session.get("_next") is None


@control_silo_test
class AuthLogoutEndpointTest(APITestCase):
    path = "/api/0/auth/"

    def test_logged_in(self):
        user = self.create_user("foo@example.com")
        self.login_as(user)
        response = self.client.delete(self.path)
        assert response.status_code == 204
        assert list(self.client.session.keys()) == []

    def test_logged_out(self):
        user = self.create_user("foo@example.com")
        self.login_as(user)
        response = self.client.delete(self.path)
        assert response.status_code == 204
        assert list(self.client.session.keys()) == []
        updated = type(user).objects.get(pk=user.id)
        assert updated.session_nonce != user.session_nonce
