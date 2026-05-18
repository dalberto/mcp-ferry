"""Idempotent Cloudflare provisioning for one tunnel + DNS + Access app.

The result of `run_setup` is everything a fresh laptop needs to expose a local
stdio MCP at `https://<hostname>` behind Google sign-in:

  1. Named Cloudflare Tunnel (credentials JSON written under ~/.cloudflared/).
  2. CNAME `<hostname>` → `<tunnel-id>.cfargotunnel.com`.
  3. Cloudflare Access identity provider (Google OAuth).
  4. Cloudflare Access application of type `mcp` with Managed OAuth turned on
     (PKCE + RFC 7591 dynamic client registration — the whole point of this
     wizard; see https://blog.cloudflare.com/managed-oauth-for-access/).
  5. Access policy allowing one email.

Every `ensure_*` helper looks up by identifying attribute (name / hostname),
creates if missing, and patches if drifted.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from cloudflare import Cloudflare
    from cloudflare.types.zero_trust.access import application_create_params as _app_params

CLOUDFLARED_DIR = Path.home() / ".cloudflared"
IDP_NAME = "Google (mcp-ferry)"
# Stable name (not per-email) so re-running setup reconciles the one allow-list
# policy instead of accumulating one policy per email.
POLICY_NAME = "mcp-ferry allow-list"
# Default Access session lifetime before a client must re-auth. 24h meant
# claude.ai forced a fresh OAuth flow every morning; 1 week is the chosen
# convenience/security balance. Overridable per-deployment via config.toml's
# [cloudflare] session_duration (threaded through SetupInputs).
APP_SESSION_DURATION = "168h"
ACCESS_TOKEN_LIFETIME = "1h"
# Public (non-localhost) redirect URIs allowed for dynamically-registered OAuth
# clients. Without this, Managed OAuth rejects any hosted client's callback
# ("Redirect URI not allowed by application configuration").
#
# Only *hosted* clients need listing here. CLI/editor clients — Claude Code,
# Codex CLI, Cursor, VS Code, MCP Inspector — use loopback redirect URIs
# (http://localhost:<ephemeral>/… or 127.0.0.1) already covered by the
# allow_any_on_localhost / allow_any_on_loopback flags below. That's why
# Inspector worked before this fix and hosted Claude/ChatGPT did not.
#
# ChatGPT note: newer ChatGPT generates a *per-connector* callback URL; the
# entry below is the documented static base and works for many setups, but if
# ChatGPT shows a different "Redirect" value, add it via --allowed-redirect-uri.
DEFAULT_ALLOWED_REDIRECT_URIS: tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://chatgpt.com/connector_platform_oauth_redirect",
)


@dataclass(frozen=True, slots=True)
class SetupInputs:
    api_token: str
    account_id: str
    zone_id: str
    hostname: str
    tunnel_name: str
    google_client_id: str
    google_client_secret: str
    allowed_emails: tuple[str, ...]
    allowed_redirect_uris: tuple[str, ...] = DEFAULT_ALLOWED_REDIRECT_URIS
    session_duration: str = APP_SESSION_DURATION


@dataclass(frozen=True, slots=True)
class SetupResult:
    tunnel_id: str
    tunnel_credentials_path: Path
    dns_record_id: str
    idp_id: str
    application_id: str
    policy_id: str


@dataclass(frozen=True, slots=True)
class _TunnelOutcome:
    tunnel_id: str
    credentials_path: Path
    # None when we adopted an existing tunnel whose secret we don't have.
    created: bool


def _credentials_path(tunnel_id: str) -> Path:
    return CLOUDFLARED_DIR / f"{tunnel_id}.json"


def _write_credentials_file(
    *, account_id: str, tunnel_id: str, tunnel_name: str, tunnel_secret: str
) -> Path:
    creds = {
        "AccountTag": account_id,
        "TunnelID": tunnel_id,
        "TunnelName": tunnel_name,
        "TunnelSecret": tunnel_secret,
    }
    path = _credentials_path(tunnel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(creds))
    path.chmod(0o600)  # contains secret material; the tunnel secret is only available at creation
    return path


def ensure_tunnel(
    client: Cloudflare, *, account_id: str, tunnel_name: str
) -> _TunnelOutcome:
    """Look up tunnel by name; create with a fresh secret if missing."""
    for existing in client.zero_trust.tunnels.cloudflared.list(
        account_id=account_id, name=tunnel_name, is_deleted=False
    ):
        if getattr(existing, "name", None) != tunnel_name:
            continue
        tunnel_id = getattr(existing, "id", None)
        if not isinstance(tunnel_id, str):
            continue
        return _TunnelOutcome(
            tunnel_id=tunnel_id,
            credentials_path=_credentials_path(tunnel_id),
            created=False,
        )

    secret = _generate_tunnel_secret()
    created = client.zero_trust.tunnels.cloudflared.create(
        account_id=account_id,
        name=tunnel_name,
        config_src="cloudflare",
        tunnel_secret=secret,
    )
    tunnel_id = getattr(created, "id", None)
    if not isinstance(tunnel_id, str):
        raise RuntimeError("Cloudflare did not return a tunnel id on create")
    path = _write_credentials_file(
        account_id=account_id,
        tunnel_id=tunnel_id,
        tunnel_name=tunnel_name,
        tunnel_secret=secret,
    )
    return _TunnelOutcome(tunnel_id=tunnel_id, credentials_path=path, created=True)


def _generate_tunnel_secret() -> str:
    """32 random bytes, base64-encoded — the format cloudflared expects."""
    import base64
    import secrets

    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def ensure_dns_record(
    client: Cloudflare, *, zone_id: str, hostname: str, tunnel_id: str
) -> str:
    """CNAME hostname → <tunnel-id>.cfargotunnel.com (proxied)."""
    target = f"{tunnel_id}.cfargotunnel.com"
    existing = list(
        client.dns.records.list(zone_id=zone_id, name={"exact": hostname}, type="CNAME")
    )
    for record in existing:
        record_id = getattr(record, "id", None)
        if not isinstance(record_id, str):
            continue
        if (
            getattr(record, "content", None) == target
            and getattr(record, "proxied", None) is True
        ):
            return record_id
        client.dns.records.update(
            record_id,
            zone_id=zone_id,
            type="CNAME",
            name=hostname,
            content=target,
            ttl=1,  # 1 = automatic for Cloudflare proxied records
            proxied=True,
        )
        return record_id

    created = client.dns.records.create(
        zone_id=zone_id,
        type="CNAME",
        name=hostname,
        content=target,
        ttl=1,
        proxied=True,
        comment="mcp-ferry: tunnel target",
    )
    record_id = getattr(created, "id", None)
    if not isinstance(record_id, str):
        raise RuntimeError("Cloudflare did not return a DNS record id on create")
    return record_id


def ensure_google_idp(
    client: Cloudflare,
    *,
    account_id: str,
    google_client_id: str,
    google_client_secret: str,
) -> str:
    """Create the Google IdP if absent; otherwise always re-push credentials.

    Google never returns the client secret over the API, so we can't diff it.
    Always reconciling means a corrected secret actually takes effect on re-run
    (the flags are the source of truth, same model as the allow-list policy).
    """
    for provider in client.zero_trust.identity_providers.list(account_id=account_id):
        if getattr(provider, "name", None) != IDP_NAME:
            continue
        provider_id = getattr(provider, "id", None)
        if not isinstance(provider_id, str):
            continue
        client.zero_trust.identity_providers.update(
            provider_id,
            account_id=account_id,
            type="google",
            name=IDP_NAME,
            config={
                "client_id": google_client_id,
                "client_secret": google_client_secret,
            },
        )
        return provider_id

    created = client.zero_trust.identity_providers.create(
        account_id=account_id,
        type="google",
        name=IDP_NAME,
        config={
            "client_id": google_client_id,
            "client_secret": google_client_secret,
        },
    )
    provider_id = getattr(created, "id", None)
    if not isinstance(provider_id, str):
        raise RuntimeError("Cloudflare did not return an IdP id on create")
    return provider_id


def _managed_oauth_config(
    allowed_redirect_uris: tuple[str, ...], session_duration: str
) -> dict[str, Any]:
    """Managed OAuth: Cloudflare acts as the authorization server for MCP clients.

    Dynamic client registration (RFC 7591) is what lets a remote MCP client
    connect without a pre-registered client ID/secret. `allowed_uris` is
    required for non-localhost clients — without it Cloudflare rejects every
    cloud-hosted client's callback ("Redirect URI not allowed by application
    configuration"); localhost/loopback flags only cover dev tools.
    """
    return {
        "enabled": True,
        "dynamic_client_registration": {
            "enabled": True,
            "allow_any_on_localhost": True,
            "allow_any_on_loopback": True,
            "allowed_uris": list(allowed_redirect_uris),
        },
        "grant": {
            "access_token_lifetime": ACCESS_TOKEN_LIFETIME,
            "session_duration": session_duration,
        },
    }


def ensure_access_application(
    client: Cloudflare,
    *,
    account_id: str,
    hostname: str,
    idp_id: str,
    allowed_redirect_uris: tuple[str, ...],
    session_duration: str = APP_SESSION_DURATION,
) -> str:
    """Create an mcp-type Access app with Managed OAuth + the Google IdP.

    An existing app is always re-pushed (idp, session, oauth config) rather than
    diffed: Cloudflare doesn't return the full oauth config to compare, and a
    declarative re-push is what makes a re-run actually fix an app provisioned
    with a stale allowed_uris list.
    """
    name = f"mcp-ferry ({hostname})"
    oauth = cast(
        "_app_params.McpServerApplicationOAuthConfiguration",
        _managed_oauth_config(allowed_redirect_uris, session_duration),
    )
    destinations = cast(
        "list[_app_params.McpServerApplicationDestinationPublicDestination]",
        [{"type": "public", "uri": hostname}],
    )

    for app in client.zero_trust.access.applications.list(account_id=account_id, name=name):
        if getattr(app, "name", None) != name:
            continue
        app_id = getattr(app, "id", None)
        if not isinstance(app_id, str):
            continue
        client.zero_trust.access.applications.update(
            app_id,
            account_id=account_id,
            type="mcp",
            name=name,
            destinations=destinations,
            allowed_idps=[idp_id],
            session_duration=session_duration,
            oauth_configuration=oauth,
            auto_redirect_to_identity=True,
        )
        return app_id

    created = client.zero_trust.access.applications.create(
        account_id=account_id,
        type="mcp",
        name=name,
        destinations=destinations,
        allowed_idps=[idp_id],
        session_duration=APP_SESSION_DURATION,
        oauth_configuration=oauth,
        auto_redirect_to_identity=True,
    )
    app_id = getattr(created, "id", None)  # pyright: ignore[reportUnknownArgumentType]
    if not isinstance(app_id, str):
        raise RuntimeError("Cloudflare did not return an application id on create")
    return app_id


def _get(obj: object, key: str) -> object:
    """Read `key` from a dict or an attribute on an SDK model — uniformly."""
    if isinstance(obj, dict):
        return cast("dict[str, object]", obj).get(key)
    return getattr(obj, key, None)


def _policy_emails(policy: object) -> set[str]:
    """Extract the set of allowed emails from a policy's include rules."""
    emails: set[str] = set()
    include = _get(policy, "include")
    if not isinstance(include, list):
        return emails
    for rule in cast("list[object]", include):
        addr = _get(_get(rule, "email"), "email")
        if isinstance(addr, str):
            emails.add(addr)
    return emails


def ensure_access_policy(
    client: Cloudflare,
    *,
    account_id: str,
    application_id: str,
    allowed_emails: tuple[str, ...],
) -> str:
    """Single reusable allow-list policy reconciled to exactly `allowed_emails`.

    The flags/config are the source of truth: a re-run rewrites the include list
    to match, so dashboard-side edits to this policy are overwritten.
    """
    from cloudflare.types.zero_trust.access.applications.email_rule_param import (
        EmailRuleParam,
    )

    desired = set(allowed_emails)
    include_rule: list[EmailRuleParam] = [{"email": {"email": e}} for e in allowed_emails]

    for policy in client.zero_trust.access.policies.list(account_id=account_id):
        if getattr(policy, "name", None) != POLICY_NAME:
            continue
        policy_id = getattr(policy, "id", None)
        if not isinstance(policy_id, str):
            continue
        if _policy_emails(policy) != desired:
            client.zero_trust.access.policies.update(
                policy_id,
                account_id=account_id,
                name=POLICY_NAME,
                decision="allow",
                include=include_rule,
            )
        _ensure_policy_attached_to_app(
            client,
            account_id=account_id,
            application_id=application_id,
            policy_id=policy_id,
        )
        return policy_id

    created = client.zero_trust.access.policies.create(
        account_id=account_id,
        name=POLICY_NAME,
        decision="allow",
        include=include_rule,
    )
    policy_id = getattr(created, "id", None)
    if not isinstance(policy_id, str):
        raise RuntimeError("Cloudflare did not return a policy id on create")
    _ensure_policy_attached_to_app(
        client,
        account_id=account_id,
        application_id=application_id,
        policy_id=policy_id,
    )
    return policy_id


def _ensure_policy_attached_to_app(
    client: Cloudflare, *, account_id: str, application_id: str, policy_id: str
) -> None:
    for existing in client.zero_trust.access.applications.policies.list(
        application_id, account_id=account_id
    ):
        if getattr(existing, "id", None) == policy_id:
            return
    # Re-fetch the app and PATCH its `policies` array. We use extra_body because the
    # SDK's typed update() signature varies per app type and we only want to touch
    # the `policies` field; everything else round-trips by virtue of being absent.
    app = client.zero_trust.access.applications.get(application_id, account_id=account_id)
    current_policy_ids: list[str] = []
    for entry in getattr(app, "policies", []) or []:
        entry_id = getattr(entry, "id", None)
        if isinstance(entry_id, str) and entry_id != policy_id:
            current_policy_ids.append(entry_id)
    app_type = cast(Any, getattr(app, "type", "mcp"))
    app_name = cast(str, getattr(app, "name", ""))
    destinations = cast(Any, list(getattr(app, "destinations", []) or []))
    client.zero_trust.access.applications.update(
        application_id,
        account_id=account_id,
        type=app_type,
        name=app_name,
        destinations=destinations,
        extra_body={"policies": [*current_policy_ids, policy_id]},
    )


def run_setup(inputs: SetupInputs) -> SetupResult:
    """Idempotently provision tunnel + DNS + IdP + Access app + policy."""
    from cloudflare import Cloudflare  # local import keeps module-import cheap

    client = Cloudflare(api_token=inputs.api_token)

    tunnel = ensure_tunnel(
        client, account_id=inputs.account_id, tunnel_name=inputs.tunnel_name
    )
    dns_record_id = ensure_dns_record(
        client,
        zone_id=inputs.zone_id,
        hostname=inputs.hostname,
        tunnel_id=tunnel.tunnel_id,
    )
    idp_id = ensure_google_idp(
        client,
        account_id=inputs.account_id,
        google_client_id=inputs.google_client_id,
        google_client_secret=inputs.google_client_secret,
    )
    application_id = ensure_access_application(
        client,
        account_id=inputs.account_id,
        hostname=inputs.hostname,
        idp_id=idp_id,
        allowed_redirect_uris=inputs.allowed_redirect_uris,
        session_duration=inputs.session_duration,
    )
    policy_id = ensure_access_policy(
        client,
        account_id=inputs.account_id,
        application_id=application_id,
        allowed_emails=inputs.allowed_emails,
    )

    return SetupResult(
        tunnel_id=tunnel.tunnel_id,
        tunnel_credentials_path=tunnel.credentials_path,
        dns_record_id=dns_record_id,
        idp_id=idp_id,
        application_id=application_id,
        policy_id=policy_id,
    )


__all__ = [
    "CLOUDFLARED_DIR",
    "SetupInputs",
    "SetupResult",
    "ensure_access_application",
    "ensure_access_policy",
    "ensure_dns_record",
    "ensure_google_idp",
    "ensure_tunnel",
    "run_setup",
]
