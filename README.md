# mcp-ferry

[![CI](https://github.com/dalberto/mcp-ferry/actions/workflows/ci.yml/badge.svg)](https://github.com/dalberto/mcp-ferry/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/mcp-ferry)](https://pypi.org/project/mcp-ferry/)
[![Python](https://img.shields.io/pypi/pyversions/mcp-ferry)](https://pypi.org/project/mcp-ferry/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

Some MCP servers only speak stdio. They run as a subprocess on your machine
because they read local data: your notes, your messages, your files. That's
fine when the MCP client runs on the same machine. It falls apart the moment
you want that tool from your phone, a browser, or an assistant running
somewhere else. The server has to sit next to the data; the client usually
doesn't.

mcp-ferry is the bridge between them. It puts a local stdio MCP server behind
a public, authenticated HTTPS URL, so any remote client can reach it while the
server keeps running right next to your data. One Cloudflare tunnel fronts as
many MCPs as you want, and adding another is one config block: no new tunnel,
no new sign-in.

## How it works

```
MCP client ──HTTPS──► Cloudflare Edge ──Tunnel──► your machine
                          │                          │
                    Managed OAuth +              mcp-ferry HTTP server
                    Google sign-in              ├── /bear ──► bearcli mcp-server (stdio)
                                                ├── /things ► things-mcp       (stdio)
                                                └── /…
```

- `mcp-ferry` runs an HTTP server (Streamable HTTP transport) on localhost.
- Each `/<path>` proxies JSON-RPC frames to one long-lived stdio MCP subprocess.
- A `cloudflared` Named Tunnel exposes that local server at `https://<hostname>`.
- Cloudflare Access protects the hostname with [Managed OAuth][1]: Cloudflare
  acts as a full OAuth 2.1 authorization server (PKCE + RFC 7591 dynamic client
  registration), so a remote MCP client can discover and authenticate on its own,
  with no manually-registered client credentials.
- Google is the identity provider behind Access; only the emails you allow can
  sign in.

Any MCP client that supports remote servers over HTTP with OAuth works. Nothing
here is client-specific.

[1]: https://blog.cloudflare.com/managed-oauth-for-access/

## Alternatives considered

mcp-ferry didn't start from scratch. The first plan was to take an existing
stdio-to-HTTP proxy and bolt the rest on by hand. Three projects are worth
knowing, and each is the better choice for a job mcp-ferry isn't trying to do:

- [mcp-proxy](https://github.com/sparfenyuk/mcp-proxy) (Python) is a clean,
  focused stdio↔Streamable HTTP/SSE bridge. If you want just the transport
  shim and you'll handle exposure and auth yourself, it's the leanest option.
- [supergateway](https://github.com/supercorp-ai/supergateway) (Node) does the
  same job in the Node ecosystem and adds WebSocket transport. Reach for it if
  your tooling is Node or you need WS.
- [mcp-remote](https://github.com/geelen/mcp-remote) goes the other direction:
  it lets a stdio-only client talk to an already-remote server. Different
  problem, and the right answer when that's the one you have.

All three are good at the transport. None of them do the rest: a public
hostname, Cloudflare Access with Managed OAuth so remote clients authenticate
without a pre-registered client, several MCPs behind one tunnel, a launchd
service so it survives a reboot, and a wizard that provisions the Cloudflare
side idempotently. mcp-ferry is those pieces assembled and hardened, not a
faster proxy. If you only need stdio↔HTTP, use one of the above. If you want
"expose a local MCP to the internet, behind Google sign-in, in a few
commands," that's the gap this fills.

## Install

Requires Python 3.14+.

```shell
uv tool install mcp-ferry          # or: pipx install mcp-ferry
```

From source (for development):

```shell
git clone https://github.com/dalberto/mcp-ferry
cd mcp-ferry
uv tool install .                  # or: pipx install .
```

## Setup

**Prerequisite:** the domain you'll use must already be an **active Cloudflare
zone** in the account your API token belongs to: the registrable apex
(e.g. `example.com`, covering `bridge.example.com`) added to Cloudflare with its
nameservers delegated and the zone Active. `ferry setup` creates a DNS record
*inside* an existing zone; it does not add a site to Cloudflare or change
registrar nameservers. If your domain isn't on Cloudflare yet: add the site in
the Cloudflare dashboard → point your registrar's nameservers at Cloudflare →
wait for the zone to go Active, then run the wizard. There is no
Cloudflare-assigned-domain fallback that preserves authentication. See
[Why a domain is required](#why-a-domain-is-required).

Four steps: scaffold the config, get two credentials manually, then run the
wizard. Everything else is provisioned by `ferry setup`.

### 1. Create and edit the config

```shell
ferry init                      # writes ~/.config/mcp-ferry/config.toml
$EDITOR ~/.config/mcp-ferry/config.toml
```

Set `bridge.hostname`, `cloudflare.tunnel_name`, and at least one `[[mcps]]`
block. The hostname you pick here is referenced in step 3.

### 2. Cloudflare API token

1. Open https://dash.cloudflare.com/profile/api-tokens.
2. Click **Create Token** → **Create Custom Token**.
3. Give it a name like `mcp-ferry`.
4. Add these permissions:
   - `Account` ▸ `Cloudflare Tunnel` ▸ **Edit**
   - `Account` ▸ `Access: Apps and Policies` ▸ **Edit**
   - `Account` ▸ `Access: Identity Providers` ▸ **Edit**
   - `Zone` ▸ `DNS` ▸ **Edit**
   - `Account` ▸ `Account Settings` ▸ **Read**: only needed if you let the
     wizard auto-discover your account. Skip it if you pass `--account-id`
     (see below); without it **and** without `--account-id`, setup fails with
     "no Cloudflare accounts visible to this token".
5. Account resources: include your account.
6. Zone resources: include the specific zone hosting your hostname.
7. Create the token and copy it. Treat it like a password.

To run with the **minimal** token (just the four Edit permissions, no
`Account Settings: Read`), tell the wizard the IDs explicitly instead of having
it discover them. Get them from the Cloudflare dashboard: the account id is in
the dashboard URL; the zone id is on the zone's **Overview** page. Then either
put them in `config.toml`:

```toml
[cloudflare]
tunnel_name = "mcp-ferry"
account_id = "<account-id>"
zone_id = "<zone-id>"
```

or pass them per-run (also honored via `CLOUDFLARE_ACCOUNT_ID` /
`CLOUDFLARE_ZONE_ID`):

```shell
ferry setup --account-id <account-id> --zone-id <zone-id> --email you@example.com
```

Precedence is flag/env → `config.toml` → SDK discovery, so the auto-discovery
path still works unchanged if you'd rather not bother.

### 3. Google OAuth client

The Access identity provider needs a Google OAuth client ID + secret.

1. Open https://console.cloud.google.com/ and create a project (or reuse one).
2. **APIs & Services** ▸ **OAuth consent screen**:
   - User type: **External**.
   - Fill in app name, support email, developer contact email.
   - You can leave the scopes / test-users sections at their defaults.
3. **APIs & Services** ▸ **Credentials** ▸ **Create Credentials** ▸ **OAuth client ID**:
   - Application type: **Web application**.
   - Name: `mcp-ferry` (or anything).
   - **Authorized redirect URIs**: add exactly
     `https://<your-team>.cloudflareaccess.com/cdn-cgi/access/callback`.

   Find `<your-team>` in the Cloudflare Zero Trust dashboard at
   **Settings** ▸ **Custom Pages** ▸ team domain. (If you've never used Zero
   Trust on this account, the dashboard will prompt you to pick the team slug.)
4. Click **Create** and copy the **Client ID** and **Client secret**.

### 4. Run the wizard

```shell
ferry setup --email you@example.com
```

Allow more than one person by repeating `--email` or comma-separating:

```shell
ferry setup --email you@example.com --email teammate@example.com
ferry setup --email "you@example.com, teammate@example.com"
```

The wizard:
- prompts for the Cloudflare API token (or reads `CLOUDFLARE_API_TOKEN`)
- prompts for the Google client ID + secret
- creates the tunnel, writes credentials to `~/.cloudflared/<tunnel-id>.json`
- creates a CNAME for your hostname pointing at the tunnel
- creates the Google identity provider in Cloudflare Access
- creates the Access application with Managed OAuth enabled
- creates a single allow-list policy for the emails you passed
- updates `config.toml` to point `cloudflare.credentials_file` at the new file

The wizard is idempotent. Re-running with the same inputs is a no-op.

**The allow-list is declarative.** There is one policy (`mcp-ferry allow-list`)
and the `--email` flags are the source of truth: each `ferry setup` run rewrites
its include rules to exactly the emails you pass. Add or remove someone by
re-running with the new flag set. Do **not** hand-edit this policy in the
Cloudflare dashboard, because the next run overwrites it. (Other policies on the
app are left untouched; only `mcp-ferry allow-list` is managed.)

## Connect a client

The bridge must be **running** first (`ferry run`, or `ferry install` for a
LaunchAgent). Cloudflare Access + OAuth live at the edge, so sign-in can
*appear* to succeed even when the bridge is down, but the MCP session then
fails because there's no origin behind the tunnel. Confirm it's up with the
checks in [Verifying the server](#verifying-the-server) before debugging the
client.

In your MCP client, add a remote/HTTP MCP server pointing at the **MCP path**,
not the bare host:

```
https://<your-hostname>/<mcp-path>      e.g. https://mcp-ferry.example.com/bear
```

The host root has no route and 404s; only the configured `[[mcps]]` paths and
`/healthz` exist. Pointing a client at the bare hostname breaks the connection
in confusing ways. The first connection redirects to Cloudflare Access → Google;
after that the session is reused. OAuth-capable clients self-register via dynamic
client registration, so there's no client ID to paste. That's what Managed OAuth
is for.

After any re-run of `ferry setup` (it reconciles the Access app + IdP), **remove
and re-add the connector** in your client so it re-discovers and re-registers.
A registration cached against an earlier provisioning is the usual cause of
"auth succeeds, then the session errors" (e.g. `code: Field required`).

### Which clients work out of the box

| Client | Callback type | Covered by |
|---|---|---|
| Claude (web / desktop / mobile) | hosted `https://claude.ai/...` | default allowlist |
| ChatGPT (developer mode) | hosted `https://chatgpt.com/...` | default allowlist |
| Claude Code, Codex CLI, Cursor, VS Code, MCP Inspector | loopback `http://localhost:<port>` / `127.0.0.1` | localhost/loopback flags (automatic) |

**Hosted** clients send a fixed public callback URL, which Managed OAuth only
permits if it's in the app's allowed-redirect-URI list. `ferry setup` provisions
Claude and ChatGPT by default. **CLI/editor** clients use an ephemeral loopback
redirect, which the wizard always allows via the
`allow_any_on_localhost`/`allow_any_on_loopback` flags, so there's no per-client
config. That's why MCP Inspector works with zero setup.

If a hosted client's callback isn't in the list, Cloudflare rejects the
authorization with `Redirect URI not allowed by application configuration`
(and the client then reports a downstream `code: Field required`). Add it:

```shell
ferry setup --allowed-redirect-uri https://claude.ai/api/mcp/auth_callback \
            --allowed-redirect-uri https://some-other-host/oauth/callback
```

`--allowed-redirect-uri` is repeatable and **replaces** the default list (so
include the ones you still want). It can also live in `config.toml`:

```toml
[cloudflare]
allowed_redirect_uris = [
  "https://claude.ai/api/mcp/auth_callback",
  "https://chatgpt.com/connector_platform_oauth_redirect",
]
```

Precedence: `--allowed-redirect-uri` → `config.toml` → built-in defaults.

Note: newer ChatGPT generates a **per-connector** callback URL. The default
entry works for many setups, but if ChatGPT's connector screen shows a
different "Redirect" value, add that exact URL with `--allowed-redirect-uri`
and re-run `ferry setup` (the Access app is reconciled, so the new list takes
effect), then re-add the connector.

## Verifying the server

Before blaming the client, prove the server itself is correct. These three
unauthenticated probes need no browser and pinpoint exactly where a break is:

```shell
H=https://<your-hostname>

# 1. MCP path must challenge with 401 + WWW-Authenticate (not 200, not 404):
curl -sS -i -X POST -H 'content-type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' "$H/<mcp-path>"

# 2. Protected-resource metadata (also names your real auth server):
curl -sS "$H/.well-known/oauth-protected-resource"

# 3. Authorization-server metadata (endpoints + DCR + PKCE support):
curl -sS "$H/.well-known/oauth-authorization-server" | python3 -m json.tool
```

Expected:

- Probe 1 → `HTTP/2 401` with `www-authenticate: Bearer ... resource_metadata=...`.
  `200` means Access isn't protecting the host; `404` means wrong path or the
  bridge is down.
- Probe 2 → `200` JSON like
  `{"resource":"https://...","authorization_servers":["https://<TEAM>.cloudflareaccess.com"]}`.
  **That `<TEAM>` value is the source of truth for your Google redirect URI.**
  It must be `https://<TEAM>.cloudflareaccess.com/cdn-cgi/access/callback`
  exactly. Read it here; do not guess the team slug.
- Probe 3 → `200` JSON containing `authorization_endpoint`, `token_endpoint`,
  `registration_endpoint`, `response_types_supported: ["code"]`, and
  `code_challenge_methods_supported`. If `registration_endpoint` is absent,
  Managed OAuth isn't enabled on the Access app.

### End-to-end with MCP Inspector

The probes prove the server config; **MCP Inspector** proves the whole
authenticated path: dynamic client registration, the browser OAuth round trip,
and an actual `initialize` + `tools/list`. Crucially it's a *fresh* client with
its own registration, so it isolates server problems from a stale registration
cached in your real client (Claude, etc.).

```shell
npx @modelcontextprotocol/inspector
```

In the Inspector UI: set **Transport** to `Streamable HTTP`, **URL** to
`https://<your-hostname>/<mcp-path>` (include the path), then **Connect**. It
opens a browser for the Cloudflare Access → Google sign-in, completes the
code + PKCE exchange, and lists the MCP's tools.

Interpreting the result:

- **Inspector connects and lists tools** → the server is fully correct. Any
  failure in your real client is client-side: remove and re-add the connector so
  it re-runs discovery + registration against the current server.
- **Inspector fails the same way** → the break is server/Cloudflare side and now
  reproducible locally. Inspector shows each OAuth step (discovery, registration,
  authorize, token) and the exact error. Debug from whichever step fails.

This is the fastest way to answer "is it the server or the client?" Start
here whenever a client connects but the session misbehaves.

## Auto-start at login

```shell
ferry install                   # installs and loads a LaunchAgent
ferry status                    # check it's running
ferry logs -f                   # tail the bridge log
```

Logs live at `~/Library/Logs/mcp-ferry/`. To remove: `ferry uninstall`.

## Adding more MCPs

Edit `config.toml` and append another `[[mcps]]` block:

```toml
[[mcps]]
name = "things"
path = "/things"
command = "uvx things-mcp"
```

Restart the bridge (`launchctl kickstart -k gui/$UID/dev.ascention.mcp-ferry`
or just `ferry uninstall && ferry install`). The new MCP appears at
`https://<hostname>/things`. No new tunnel, no new Access app needed.

## Changing your hostname

If you edit `bridge.hostname` in `config.toml` and re-run `ferry setup`, the
tunnel is reused (it's matched by name) but **the DNS record and the Access
application are matched by the old hostname**, so the wizard creates a *new*
CNAME and a *new* Access app and leaves the old ones behind. Nothing breaks, but
you should clean up the orphans manually:

1. Cloudflare dashboard → DNS → delete the old `CNAME` for the previous hostname.
2. Zero Trust → Access → Applications → delete the old `mcp-ferry (<old-host>)`
   application.

The tunnel, IdP, and `mcp-ferry allow-list` policy are unaffected and don't need
recreating.

## Why a domain is required

mcp-ferry's whole point is an *authenticated* public URL. Authentication comes
from Cloudflare Access + Managed OAuth, and Access can only be attached to a
hostname on a Cloudflare zone you control. There is no first-party
Cloudflare-assigned domain that supports this:

- **Quick Tunnels** (`*.trycloudflare.com`) need no domain or account, but they
  cannot carry Cloudflare Access: the URL is unauthenticated and anyone with it
  reaches your MCP. They're also ephemeral (a new random hostname every
  restart). That defeats the security model, so mcp-ferry doesn't use them.
- `*.cfargotunnel.com` is the tunnel's internal target, not a routable public
  hostname; you can't serve or protect an app on it.
- Cloudflare doesn't hand out free Access-capable subdomains of its own domains.

So the floor is: a domain you own, on Cloudflare's free tier. The cheapest path
is a ~$10/yr registration (any registrar, or Cloudflare Registrar at cost) added
as a zone. If you genuinely want an unauthenticated, throwaway tunnel for local
testing, run `cloudflared tunnel --url http://localhost:<port>` directly. That
is explicitly *not* what this tool is for, and there's deliberately no `--quick`
flag that would make it easy to expose your data with no auth.

## Troubleshooting

- `ferry status`: LaunchAgent state + per-MCP health from `/healthz`.
- `ferry logs -f`: tail `stdout`. Pass `--stream err` for `stderr`.
- `cloudflared` not finding the tunnel: confirm `cloudflare.credentials_file`
  in `config.toml` points at the JSON file the wizard wrote.
- Access redirect loop: verify the Google authorized redirect URI is exactly
  `https://<team>.cloudflareaccess.com/cdn-cgi/access/callback` and that the
  Access app's allowed IdP is the one the wizard created.
- Someone can't get in: confirm their email is in the `--email` set you last ran
  `ferry setup` with; the allow-list is rewritten from those flags each run.
- `Redirect URI not allowed by application configuration` (in the OAuth callback
  URL), surfacing to the client as a downstream `code: Field required`: the
  hosted client's callback isn't in the app's allowed-redirect-URI list. This
  affects hosted clients only (Claude/ChatGPT/etc.); loopback clients like MCP
  Inspector are unaffected, so "Inspector works but Claude doesn't" is the
  signature. Fix: `ferry setup --allowed-redirect-uri <the exact callback>`
  (see [Which clients work out of the box](#which-clients-work-out-of-the-box)),
  then re-add the connector.
- `code: Field required` with no `Redirect URI not allowed` in the callback: the
  bridge is almost certainly **not running**, or the connector points at the
  bare host instead of the `/<mcp-path>`. Run the
  [verification probes](#verifying-the-server); if they pass, remove and re-add
  the connector to clear a stale registration.
- Google `redirect_uri_mismatch`: the Google client's authorized redirect URI
  does not match. Get the exact value from probe 2 above
  (`https://<TEAM>.cloudflareaccess.com/cdn-cgi/access/callback`), not your app
  hostname, and not a guessed slug. Google can take a few minutes to honor a
  newly added URI.
- Google `invalid_client` / "client secret is invalid": the secret in Cloudflare
  doesn't match Google. Verify no truncation/whitespace and that it's the
  secret, not the client ID. Pass it via `--google-client-secret` (or
  `GOOGLE_CLIENT_SECRET`) rather than the prompt; the hidden prompt is the most
  common source of a one-character paste truncation. Re-running `ferry setup`
  re-pushes it (the IdP is reconciled declaratively).
- Edited the source but the CLI doesn't reflect it (e.g. `No such option`):
  `uv tool install` snapshots the code. Reinstall, or install once with
  `uv tool install --force --editable .` so local edits are picked up live.
