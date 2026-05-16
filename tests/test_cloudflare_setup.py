"""Tests for cloudflare_setup.py — the Cloudflare SDK is fully mocked."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from mcp_ferry import cloudflare_setup
from mcp_ferry.cloudflare_setup import (
    SetupInputs,
    ensure_access_application,
    ensure_access_policy,
    ensure_dns_record,
    ensure_google_idp,
    ensure_tunnel,
    run_setup,
)

# ---------- fakes ----------------------------------------------------------


@dataclass
class _Calls:
    create: list[dict[str, Any]]
    update: list[tuple[str, dict[str, Any]]]
    list_args: list[dict[str, Any]]


def _ns(**kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(**kwargs)


class _FakeTunnels:
    def __init__(self, existing: list[SimpleNamespace] | None = None) -> None:
        self._existing = existing or []
        self.calls = _Calls([], [], [])

    def list(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.list_args.append(kwargs)
        return self._existing

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.create.append(kwargs)
        return _ns(id="TUNNEL_NEW_ID", name=kwargs["name"])


class _FakeDNS:
    def __init__(self, existing: list[SimpleNamespace] | None = None) -> None:
        self._existing = existing or []
        self.calls = _Calls([], [], [])

    def list(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.list_args.append(kwargs)
        return self._existing

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.create.append(kwargs)
        return _ns(id="DNS_NEW_ID", **kwargs)

    def update(self, record_id: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.update.append((record_id, kwargs))
        return _ns(id=record_id, **kwargs)


class _FakeIdPs:
    def __init__(self, existing: list[SimpleNamespace] | None = None) -> None:
        self._existing = existing or []
        self.calls = _Calls([], [], [])

    def list(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.list_args.append(kwargs)
        return self._existing

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.create.append(kwargs)
        return _ns(id="IDP_NEW_ID", name=kwargs["name"], config=_ns(**kwargs["config"]))

    def update(self, idp_id: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.update.append((idp_id, kwargs))
        return _ns(id=idp_id, **kwargs)


class _FakeAppPolicies:
    def __init__(self, existing: list[SimpleNamespace] | None = None) -> None:
        self._existing = existing or []
        self.calls = _Calls([], [], [])

    def list(self, _app_id: str, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.list_args.append({"app_id": _app_id, **kwargs})
        return self._existing


class _FakeAccessApps:
    def __init__(
        self,
        existing: list[SimpleNamespace] | None = None,
        attached_policies: list[SimpleNamespace] | None = None,
    ) -> None:
        self._existing = existing or []
        self.calls = _Calls([], [], [])
        self.policies = _FakeAppPolicies(attached_policies)
        self.get_response: SimpleNamespace | None = None

    def list(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.list_args.append(kwargs)
        return self._existing

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.create.append(kwargs)
        return _ns(id="APP_NEW_ID", **kwargs)

    def update(self, app_id: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.update.append((app_id, kwargs))
        return _ns(id=app_id, **kwargs)

    def get(self, app_id: str, **_kwargs: Any) -> SimpleNamespace:
        return self.get_response or _ns(
            id=app_id, name="app", type="mcp", destinations=[], policies=[]
        )


class _FakeAccessPolicies:
    def __init__(self, existing: list[SimpleNamespace] | None = None) -> None:
        self._existing = existing or []
        self.calls = _Calls([], [], [])

    def list(self, **kwargs: Any) -> list[SimpleNamespace]:
        self.calls.list_args.append(kwargs)
        return self._existing

    def create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.create.append(kwargs)
        return _ns(id="POLICY_NEW_ID", **kwargs)

    def update(self, policy_id: str, **kwargs: Any) -> SimpleNamespace:
        self.calls.update.append((policy_id, kwargs))
        return _ns(id=policy_id, **kwargs)


class _FakeClient:
    def __init__(
        self,
        *,
        tunnels: _FakeTunnels | None = None,
        dns: _FakeDNS | None = None,
        idps: _FakeIdPs | None = None,
        access_apps: _FakeAccessApps | None = None,
        access_policies: _FakeAccessPolicies | None = None,
    ) -> None:
        self._tunnels = tunnels or _FakeTunnels()
        self.dns = SimpleNamespace(records=dns or _FakeDNS())
        self.access_apps = access_apps or _FakeAccessApps()
        self.access_policies = access_policies or _FakeAccessPolicies()
        self.identity_providers = idps or _FakeIdPs()

        self.zero_trust = SimpleNamespace(
            tunnels=SimpleNamespace(cloudflared=self._tunnels),
            identity_providers=self.identity_providers,
            access=SimpleNamespace(
                applications=self.access_apps,
                policies=self.access_policies,
            ),
        )


# ---------- ensure_tunnel ---------------------------------------------------


def test_ensure_tunnel_creates_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cloudflare_setup, "CLOUDFLARED_DIR", tmp_path)
    client = _FakeClient()

    outcome = ensure_tunnel(client, account_id="ACC", tunnel_name="mcp-ferry")  # type: ignore[arg-type]

    assert outcome.tunnel_id == "TUNNEL_NEW_ID"
    assert outcome.created is True
    assert outcome.credentials_path == tmp_path / "TUNNEL_NEW_ID.json"
    assert outcome.credentials_path.exists()
    payload = json.loads(outcome.credentials_path.read_text())
    assert payload["AccountTag"] == "ACC"
    assert payload["TunnelID"] == "TUNNEL_NEW_ID"
    assert payload["TunnelName"] == "mcp-ferry"
    assert payload["TunnelSecret"]
    # File should be 0600.
    assert oct(outcome.credentials_path.stat().st_mode & 0o777) == "0o600"


def test_ensure_tunnel_returns_existing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cloudflare_setup, "CLOUDFLARED_DIR", tmp_path)
    existing = [_ns(id="OLD_ID", name="mcp-ferry")]
    tunnels = _FakeTunnels(existing=existing)
    client = _FakeClient(tunnels=tunnels)

    outcome = ensure_tunnel(client, account_id="ACC", tunnel_name="mcp-ferry")  # type: ignore[arg-type]

    assert outcome.tunnel_id == "OLD_ID"
    assert outcome.created is False
    assert tunnels.calls.create == []


# ---------- ensure_dns_record ----------------------------------------------


def test_ensure_dns_record_creates_when_missing() -> None:
    client = _FakeClient()

    record_id = ensure_dns_record(
        client,  # type: ignore[arg-type]
        zone_id="ZONE",
        hostname="bridge.example.com",
        tunnel_id="TID",
    )

    assert record_id == "DNS_NEW_ID"
    call = client.dns.records.calls.create[0]
    assert call["type"] == "CNAME"
    assert call["name"] == "bridge.example.com"
    assert call["content"] == "TID.cfargotunnel.com"
    assert call["proxied"] is True


def test_ensure_dns_record_returns_existing_when_correct() -> None:
    existing = [
        _ns(id="REC_ID", content="TID.cfargotunnel.com", proxied=True, type="CNAME"),
    ]
    client = _FakeClient(dns=_FakeDNS(existing=existing))

    record_id = ensure_dns_record(
        client,  # type: ignore[arg-type]
        zone_id="ZONE",
        hostname="bridge.example.com",
        tunnel_id="TID",
    )

    assert record_id == "REC_ID"
    assert client.dns.records.calls.create == []
    assert client.dns.records.calls.update == []


def test_ensure_dns_record_updates_when_pointing_elsewhere() -> None:
    existing = [
        _ns(id="REC_ID", content="elsewhere.example.com", proxied=True, type="CNAME"),
    ]
    client = _FakeClient(dns=_FakeDNS(existing=existing))

    record_id = ensure_dns_record(
        client,  # type: ignore[arg-type]
        zone_id="ZONE",
        hostname="bridge.example.com",
        tunnel_id="TID",
    )

    assert record_id == "REC_ID"
    update_id, update_kwargs = client.dns.records.calls.update[0]
    assert update_id == "REC_ID"
    assert update_kwargs["content"] == "TID.cfargotunnel.com"
    assert update_kwargs["proxied"] is True


# ---------- ensure_google_idp ----------------------------------------------


def test_ensure_google_idp_creates_when_missing() -> None:
    client = _FakeClient()

    idp_id = ensure_google_idp(
        client,  # type: ignore[arg-type]
        account_id="ACC",
        google_client_id="GCID",
        google_client_secret="GSEC",
    )

    assert idp_id == "IDP_NEW_ID"
    call = client.identity_providers.calls.create[0]
    assert call["type"] == "google"
    assert call["config"] == {"client_id": "GCID", "client_secret": "GSEC"}


def test_ensure_google_idp_always_repushes_credentials() -> None:
    # Even when the client id is unchanged, the secret must be re-pushed: Google
    # never returns it, so reconciling is the only way a corrected secret takes.
    existing = [
        _ns(
            id="OLD_IDP",
            name=cloudflare_setup.IDP_NAME,
            config=_ns(client_id="GCID"),
        )
    ]
    client = _FakeClient(idps=_FakeIdPs(existing=existing))

    idp_id = ensure_google_idp(
        client,  # type: ignore[arg-type]
        account_id="ACC",
        google_client_id="GCID",
        google_client_secret="CORRECTED_SECRET",
    )
    assert idp_id == "OLD_IDP"
    assert client.identity_providers.calls.create == []
    _, update_kwargs = client.identity_providers.calls.update[0]
    assert update_kwargs["config"] == {
        "client_id": "GCID",
        "client_secret": "CORRECTED_SECRET",
    }


def test_ensure_google_idp_patches_when_client_id_drifts() -> None:
    existing = [
        _ns(
            id="OLD_IDP",
            name=cloudflare_setup.IDP_NAME,
            config=_ns(client_id="OLD_GCID"),
        )
    ]
    client = _FakeClient(idps=_FakeIdPs(existing=existing))

    idp_id = ensure_google_idp(
        client,  # type: ignore[arg-type]
        account_id="ACC",
        google_client_id="NEW_GCID",
        google_client_secret="NEW_SEC",
    )

    assert idp_id == "OLD_IDP"
    _, update_kwargs = client.identity_providers.calls.update[0]
    assert update_kwargs["config"]["client_id"] == "NEW_GCID"


# ---------- ensure_access_application --------------------------------------


def test_ensure_access_application_creates_with_managed_oauth() -> None:
    client = _FakeClient()

    app_id = ensure_access_application(
        client,  # type: ignore[arg-type]
        account_id="ACC",
        hostname="bridge.example.com",
        idp_id="IDP_ID",
        allowed_redirect_uris=("https://claude.ai/api/mcp/auth_callback",),
    )

    assert app_id == "APP_NEW_ID"
    call = client.access_apps.calls.create[0]
    assert call["type"] == "mcp"
    assert call["allowed_idps"] == ["IDP_ID"]
    assert call["destinations"] == [{"type": "public", "uri": "bridge.example.com"}]
    oauth = call["oauth_configuration"]
    assert oauth["enabled"] is True
    dcr = oauth["dynamic_client_registration"]
    assert dcr["enabled"] is True
    assert dcr["allow_any_on_localhost"] is True
    assert dcr["allowed_uris"] == ["https://claude.ai/api/mcp/auth_callback"]


def test_ensure_access_application_always_repushes_existing() -> None:
    # Existing app is always reconciled (idp + oauth incl. allowed_uris), even
    # when idp/session look unchanged — Cloudflare doesn't return the oauth
    # config to diff, so a re-run is the only way a stale allowed_uris is fixed.
    existing = [
        _ns(
            id="OLD_APP",
            name="mcp-ferry (bridge.example.com)",
            allowed_idps=["IDP_ID"],
            session_duration=cloudflare_setup.APP_SESSION_DURATION,
        )
    ]
    client = _FakeClient(access_apps=_FakeAccessApps(existing=existing))

    app_id = ensure_access_application(
        client,  # type: ignore[arg-type]
        account_id="ACC",
        hostname="bridge.example.com",
        idp_id="IDP_ID",
        allowed_redirect_uris=("https://chatgpt.com/connector_platform_oauth_redirect",),
    )

    assert app_id == "OLD_APP"
    assert client.access_apps.calls.create == []
    _, update_kwargs = client.access_apps.calls.update[0]
    assert update_kwargs["allowed_idps"] == ["IDP_ID"]
    dcr = update_kwargs["oauth_configuration"]["dynamic_client_registration"]
    assert dcr["allowed_uris"] == [
        "https://chatgpt.com/connector_platform_oauth_redirect"
    ]


# ---------- ensure_access_policy -------------------------------------------


def test_ensure_access_policy_creates_with_email_rule() -> None:
    # The reusable policy needs to be attached to the app; emulate the app having
    # no attached policies yet so the helper takes the attach path.
    app_policies = _FakeAppPolicies(existing=[])
    apps = _FakeAccessApps(attached_policies=[])
    apps.policies = app_policies
    apps.get_response = _ns(
        id="APP_ID", name="app", type="mcp", destinations=[], policies=[]
    )
    client = _FakeClient(access_apps=apps)

    policy_id = ensure_access_policy(
        client,  # type: ignore[arg-type]
        account_id="ACC",
        application_id="APP_ID",
        allowed_emails=("me@example.com",),
    )

    assert policy_id == "POLICY_NEW_ID"
    call = client.access_policies.calls.create[0]
    assert call["decision"] == "allow"
    assert call["name"] == cloudflare_setup.POLICY_NAME
    assert call["include"] == [{"email": {"email": "me@example.com"}}]
    # Attach pass: app should have been updated with the new policy id via extra_body.
    assert len(client.access_apps.calls.update) == 1
    _, update_kwargs = client.access_apps.calls.update[0]
    assert update_kwargs["extra_body"] == {"policies": ["POLICY_NEW_ID"]}


def test_ensure_access_policy_creates_with_multiple_emails() -> None:
    apps = _FakeAccessApps(attached_policies=[])
    apps.get_response = _ns(
        id="APP_ID", name="app", type="mcp", destinations=[], policies=[]
    )
    client = _FakeClient(access_apps=apps)

    ensure_access_policy(
        client,  # type: ignore[arg-type]
        account_id="ACC",
        application_id="APP_ID",
        allowed_emails=("a@example.com", "b@example.com"),
    )

    call = client.access_policies.calls.create[0]
    assert call["include"] == [
        {"email": {"email": "a@example.com"}},
        {"email": {"email": "b@example.com"}},
    ]


def test_ensure_access_policy_returns_existing_when_unchanged() -> None:
    existing_policy = _ns(
        id="OLD_POLICY",
        name=cloudflare_setup.POLICY_NAME,
        include=[{"email": {"email": "me@example.com"}}],
    )
    attached = [_ns(id="OLD_POLICY")]
    apps = _FakeAccessApps(attached_policies=attached)
    client = _FakeClient(
        access_policies=_FakeAccessPolicies(existing=[existing_policy]),
        access_apps=apps,
    )

    policy_id = ensure_access_policy(
        client,  # type: ignore[arg-type]
        account_id="ACC",
        application_id="APP_ID",
        allowed_emails=("me@example.com",),
    )

    assert policy_id == "OLD_POLICY"
    assert client.access_policies.calls.create == []
    assert client.access_policies.calls.update == []
    assert client.access_apps.calls.update == []


def test_ensure_access_policy_updates_when_emails_drift() -> None:
    existing_policy = _ns(
        id="OLD_POLICY",
        name=cloudflare_setup.POLICY_NAME,
        include=[{"email": {"email": "old@example.com"}}],
    )
    attached = [_ns(id="OLD_POLICY")]
    apps = _FakeAccessApps(attached_policies=attached)
    client = _FakeClient(
        access_policies=_FakeAccessPolicies(existing=[existing_policy]),
        access_apps=apps,
    )

    policy_id = ensure_access_policy(
        client,  # type: ignore[arg-type]
        account_id="ACC",
        application_id="APP_ID",
        allowed_emails=("new@example.com", "extra@example.com"),
    )

    assert policy_id == "OLD_POLICY"
    assert client.access_policies.calls.create == []
    update_id, update_kwargs = client.access_policies.calls.update[0]
    assert update_id == "OLD_POLICY"
    assert update_kwargs["include"] == [
        {"email": {"email": "new@example.com"}},
        {"email": {"email": "extra@example.com"}},
    ]


# ---------- run_setup composes & idempotency -------------------------------


def _make_inputs() -> SetupInputs:
    return SetupInputs(
        api_token="TOKEN",
        account_id="ACC",
        zone_id="ZONE",
        hostname="bridge.example.com",
        tunnel_name="mcp-ferry",
        google_client_id="GCID",
        google_client_secret="GSEC",
        allowed_emails=("me@example.com",),
    )


class _State:
    def __init__(self) -> None:
        self.tunnels: list[SimpleNamespace] = []
        self.dns_records: list[SimpleNamespace] = []
        self.idps: list[SimpleNamespace] = []
        self.apps: list[SimpleNamespace] = []
        self.policies: list[SimpleNamespace] = []
        self.app_attached_policies: dict[str, list[SimpleNamespace]] = {}
        self.creates: list[str] = []  # tags

    def cloudflare_factory(self, *, api_token: str) -> MagicMock:
        assert api_token == "TOKEN"
        client = MagicMock()
        state = self

        # tunnels
        def tunnels_list(**_kw: Any) -> list[SimpleNamespace]:
            return list(state.tunnels)

        def tunnels_create(**kw: Any) -> SimpleNamespace:
            new = _ns(id="TID-1", name=kw["name"])
            state.tunnels.append(new)
            state.creates.append("tunnel")
            return new

        client.zero_trust.tunnels.cloudflared.list.side_effect = tunnels_list
        client.zero_trust.tunnels.cloudflared.create.side_effect = tunnels_create

        # dns
        def dns_list(**_kw: Any) -> list[SimpleNamespace]:
            return list(state.dns_records)

        def dns_create(**kw: Any) -> SimpleNamespace:
            new = _ns(id="DNS-1", **kw)
            state.dns_records.append(new)
            state.creates.append("dns")
            return new

        def dns_update(rid: str, **kw: Any) -> SimpleNamespace:
            for i, r in enumerate(state.dns_records):
                if r.id == rid:
                    state.dns_records[i] = _ns(id=rid, **kw)
                    break
            return _ns(id=rid, **kw)

        client.dns.records.list.side_effect = dns_list
        client.dns.records.create.side_effect = dns_create
        client.dns.records.update.side_effect = dns_update

        # idps
        def idp_list(**_kw: Any) -> list[SimpleNamespace]:
            return list(state.idps)

        def idp_create(**kw: Any) -> SimpleNamespace:
            new = _ns(
                id="IDP-1",
                name=kw["name"],
                config=_ns(**kw["config"]),
            )
            state.idps.append(new)
            state.creates.append("idp")
            return new

        client.zero_trust.identity_providers.list.side_effect = idp_list
        client.zero_trust.identity_providers.create.side_effect = idp_create

        # apps
        def app_list(**_kw: Any) -> list[SimpleNamespace]:
            return list(state.apps)

        def app_create(**kw: Any) -> SimpleNamespace:
            new = _ns(id="APP-1", **kw)
            state.apps.append(new)
            state.creates.append("app")
            return new

        def app_update(aid: str, **kw: Any) -> SimpleNamespace:
            for i, a in enumerate(state.apps):
                if a.id == aid:
                    merged = {**a.__dict__, **kw, "id": aid}
                    state.apps[i] = _ns(**merged)
                    break
            return _ns(id=aid, **kw)

        def app_get(aid: str, **_kw: Any) -> SimpleNamespace:
            for a in state.apps:
                if a.id == aid:
                    return a
            return _ns(id=aid, name="app", type="mcp", destinations=[], policies=[])

        def app_policies_list(app_id: str, **_kw: Any) -> list[SimpleNamespace]:
            return list(state.app_attached_policies.get(app_id, []))

        client.zero_trust.access.applications.list.side_effect = app_list
        client.zero_trust.access.applications.create.side_effect = app_create
        client.zero_trust.access.applications.update.side_effect = app_update
        client.zero_trust.access.applications.get.side_effect = app_get
        client.zero_trust.access.applications.policies.list.side_effect = (
            app_policies_list
        )

        # policies
        def policies_list(**_kw: Any) -> list[SimpleNamespace]:
            return list(state.policies)

        def policies_create(**kw: Any) -> SimpleNamespace:
            new = _ns(id="POL-1", **kw)
            state.policies.append(new)
            state.creates.append("policy")
            # The policy needs to be attached: simulate the update step's effect.
            state.app_attached_policies.setdefault("APP-1", []).append(_ns(id="POL-1"))
            return new

        client.zero_trust.access.policies.list.side_effect = policies_list
        client.zero_trust.access.policies.create.side_effect = policies_create

        return client


def test_run_setup_composes_and_writes_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cloudflare_setup, "CLOUDFLARED_DIR", tmp_path)
    state = _State()

    import cloudflare as cf_mod

    monkeypatch.setattr(cf_mod, "Cloudflare", state.cloudflare_factory)

    result = run_setup(_make_inputs())

    assert result.tunnel_id == "TID-1"
    assert result.dns_record_id == "DNS-1"
    assert result.idp_id == "IDP-1"
    assert result.application_id == "APP-1"
    assert result.policy_id == "POL-1"
    creds = tmp_path / "TID-1.json"
    assert creds.exists()
    assert oct(creds.stat().st_mode & 0o777) == "0o600"
    assert state.creates == ["tunnel", "dns", "idp", "app", "policy"]


def test_run_setup_is_idempotent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cloudflare_setup, "CLOUDFLARED_DIR", tmp_path)
    state = _State()

    import cloudflare as cf_mod

    monkeypatch.setattr(cf_mod, "Cloudflare", state.cloudflare_factory)

    first = run_setup(_make_inputs())
    state.creates.clear()
    second = run_setup(_make_inputs())

    assert state.creates == []
    assert first == second
