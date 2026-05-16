# Security

## Reporting a vulnerability

Report privately via GitHub Security Advisories
(<https://github.com/dalberto/mcp-ferry/security/advisories/new>). Please don't
open a public issue for a vulnerability. Expect an initial response within a
few days.

## Trust model

mcp-ferry exposes a local stdio MCP server on the public internet. Treat that
seriously:

- The bridge is a transparent proxy. It does not authenticate requests itself;
  authentication is entirely Cloudflare Access + Managed OAuth at the edge. If
  the Access app or its allow-list policy is misconfigured, the MCP behind it
  is exposed. Verify with the probes in the README's "Verifying the server".
- The Cloudflare API token used by `ferry setup` is provisioning-only and is
  never persisted by the tool. Don't put it in a tracked dotfile; prefer a
  secret manager or the interactive prompt.
- The tunnel credentials file (`~/.cloudflared/<id>.json`) contains the tunnel
  secret. `ferry setup` writes it `0600`. Keep it out of version control.
- Anyone whose email is in the `mcp-ferry allow-list` policy can reach every
  MCP behind the hostname. Scope `--email` accordingly.
- Quick Tunnels are deliberately unsupported because they can't carry Access;
  there is no flag that exposes an MCP without authentication.
