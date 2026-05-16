# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.1.0]

Initial release.

- Bridge any number of local stdio MCP servers to one public HTTPS hostname.
- Streamable HTTP transport (one route per MCP), hardened stdio supervision:
  reader-death restart, bounded request timeout, 16 MiB line buffer, graceful
  stop.
- `cloudflared` Named Tunnel lifecycle via pycloudflared.
- `ferry setup` idempotent wizard: tunnel, DNS, Google IdP, Access app with
  Managed OAuth (hosted-client redirect allowlist incl. Claude + ChatGPT),
  email allow-list policy. Account/zone IDs and Google creds settable by flag,
  env, or config for a minimal-permission token and non-interactive runs.
- `ferry install` LaunchAgent for auto-start at login with restart-on-crash.
- CLI: `init`, `run`, `setup`, `install`, `uninstall`, `status`, `logs`.

[Unreleased]: https://github.com/dalberto/mcp-ferry/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/dalberto/mcp-ferry/releases/tag/v0.1.0
