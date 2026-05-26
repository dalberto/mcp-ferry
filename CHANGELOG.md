# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] - 2026-05-25

### Changed

- LaunchAgent integration now uses the [`launchy`](https://github.com/dalberto/launchy)
  library (>= 0.1.1) instead of hand-rolled `plistlib` + `launchctl` plumbing.
  The `mcp_ferry.launchd` module is gone; `ferry install/uninstall/status` live
  directly in the CLI.
- LaunchAgent label renamed `dev.ascention.mcp-ferry` → `io.github.dalberto.mcp-ferry`.
  Existing installs must `launchctl bootout gui/$UID/dev.ascention.mcp-ferry`
  and remove `~/Library/LaunchAgents/dev.ascention.mcp-ferry.plist` once, then
  run `ferry install` to register the new agent.

### Fixed

- `ferry install` against an already-loaded agent no longer races with the
  prior incarnation's graceful shutdown. (Fixed upstream in launchy 0.1.1 via
  PID-poll between `bootout` and `bootstrap`; the dep pin enforces it.)

## [0.1.1] - 2026-05-17

### Fixed

- Tunnel death is no longer fatal: `cloudflared` exiting (overnight network
  drop, laptop sleep/wake) now restarts in-process with exponential backoff
  instead of unwinding the whole bridge. The named tunnel keeps a stable
  hostname across the restart, so no client reconfiguration is needed.
- The supervisor now exits non-zero on any non-signal unwind (HTTP server
  death) so the LaunchAgent's `KeepAlive(SuccessfulExit=false)` actually
  restarts it. Previously it exited 0 and launchd left the bridge dead.
- Dropped the no-op `NetworkState` key from the LaunchAgent plist.
- `/healthz` now reports tunnel health, and tunnel health tracks live edge
  connections instead of latching `True` on first connect. Previously
  `/healthz` (and `ferry status`) reported "ok" while the bridge was
  unreachable, because nothing observed the tunnel.

### Changed

- Logs now rotate in-process (`~/Library/Logs/mcp-ferry/ferry.log`, 10 MiB ×
  5). The chatty `cloudflared` stream no longer grows the LaunchAgent
  `.err.log` unbounded; that file now holds only pre-init tracebacks.
  `ferry logs` tails the rotated log by default (`--stream out|err` for the
  boot files).
- Default Access session duration raised from 24h to 1 week (`168h`) so
  clients (e.g. claude.ai) no longer re-authenticate every morning. Override
  per-deployment via `[cloudflare] session_duration` in `config.toml`
  (applied on the next `ferry setup`).

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

[Unreleased]: https://github.com/dalberto/mcp-ferry/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/dalberto/mcp-ferry/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/dalberto/mcp-ferry/releases/tag/v0.1.0
