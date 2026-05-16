from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from mcp_ferry import cli as cli_mod
from mcp_ferry import cloudflare_setup
from mcp_ferry.cli import app


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_init_writes_starter_config(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    result = runner.invoke(app, ["init", "--config", str(target)])
    assert result.exit_code == 0, result.output
    assert target.exists()
    body = target.read_text()
    assert "[bridge]" in body
    assert "[[mcps]]" in body
    assert "tunnel_name" in body


def test_init_refuses_overwrite_without_force(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("# existing")
    result = runner.invoke(app, ["init", "--config", str(target)])
    assert result.exit_code == 1
    assert target.read_text() == "# existing"


def test_init_force_overwrites(runner: CliRunner, tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_text("# existing")
    result = runner.invoke(app, ["init", "--config", str(target), "--force"])
    assert result.exit_code == 0
    assert "[bridge]" in target.read_text()


def test_logs_missing_file(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("mcp_ferry.launchd.LOG_DIR", tmp_path / "nope")
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 1
    assert "no log file" in result.output


def _write_starter_config(path: Path) -> None:
    path.write_text(
        """[bridge]
hostname = "bridge.example.com"
local_port = 8765

[cloudflare]
tunnel_name = "mcp-ferry"
# credentials_file = "..."

[[mcps]]
name = "bear"
path = "/bear"
command = "bearcli mcp-server"
"""
    )


def test_setup_invokes_run_setup_and_patches_config(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    _write_starter_config(config_path)

    captured: dict[str, Any] = {}

    def fake_cloudflare(*, api_token: str) -> SimpleNamespace:
        captured["api_token"] = api_token

        def list_accounts(**_kw: Any) -> list[SimpleNamespace]:
            return [SimpleNamespace(id="ACC-ID", name="My Account")]

        def list_zones(**_kw: Any) -> list[SimpleNamespace]:
            return [SimpleNamespace(id="ZONE-ID", name="example.com")]

        return SimpleNamespace(
            accounts=SimpleNamespace(list=list_accounts),
            zones=SimpleNamespace(list=list_zones),
        )

    import cloudflare as cf_mod

    monkeypatch.setattr(cf_mod, "Cloudflare", fake_cloudflare)

    creds_path = tmp_path / ".cloudflared" / "TID.json"

    def fake_run_setup(inputs: cloudflare_setup.SetupInputs) -> cloudflare_setup.SetupResult:
        captured["inputs"] = inputs
        return cloudflare_setup.SetupResult(
            tunnel_id="TID",
            tunnel_credentials_path=creds_path,
            dns_record_id="DNS",
            idp_id="IDP",
            application_id="APP",
            policy_id="POL",
        )

    monkeypatch.setattr(cli_mod.cloudflare_setup, "run_setup", fake_run_setup)

    result = runner.invoke(
        app,
        [
            "setup",
            "--config",
            str(config_path),
            "--token",
            "TOKEN",
            "--email",
            "me@example.com",
        ],
        input="GCID\nGSEC\n",  # google client id, client secret prompts
    )

    assert result.exit_code == 0, result.output
    assert captured["api_token"] == "TOKEN"
    inputs = captured["inputs"]
    assert isinstance(inputs, cloudflare_setup.SetupInputs)
    assert inputs.account_id == "ACC-ID"
    assert inputs.zone_id == "ZONE-ID"
    assert inputs.hostname == "bridge.example.com"
    assert inputs.tunnel_name == "mcp-ferry"
    assert inputs.allowed_emails == ("me@example.com",)
    assert inputs.google_client_id == "GCID"
    assert inputs.google_client_secret == "GSEC"

    # config.toml should now declare the credentials_file under [cloudflare].
    updated = config_path.read_text()
    assert f'credentials_file = "{creds_path}"' in updated
    assert "Done" in result.output


def test_setup_explicit_ids_skip_sdk_discovery(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    _write_starter_config(config_path)

    def boom(**_kw: Any) -> list[SimpleNamespace]:
        raise AssertionError("SDK discovery should be skipped when ids are explicit")

    def fake_cloudflare(*, api_token: str) -> SimpleNamespace:
        del api_token
        return SimpleNamespace(
            accounts=SimpleNamespace(list=boom),
            zones=SimpleNamespace(list=boom),
        )

    import cloudflare as cf_mod

    monkeypatch.setattr(cf_mod, "Cloudflare", fake_cloudflare)

    captured: dict[str, Any] = {}

    def fake_run_setup(inputs: cloudflare_setup.SetupInputs) -> cloudflare_setup.SetupResult:
        captured["inputs"] = inputs
        return cloudflare_setup.SetupResult(
            tunnel_id="T",
            tunnel_credentials_path=tmp_path / "T.json",
            dns_record_id="D",
            idp_id="I",
            application_id="A",
            policy_id="P",
        )

    monkeypatch.setattr(cli_mod.cloudflare_setup, "run_setup", fake_run_setup)

    result = runner.invoke(
        app,
        [
            "setup",
            "--config",
            str(config_path),
            "--token",
            "TOKEN",
            "--email",
            "me@example.com",
            "--account-id",
            "ACC-EXPLICIT",
            "--zone-id",
            "ZONE-EXPLICIT",
        ],
        input="GCID\nGSEC\n",
    )

    assert result.exit_code == 0, result.output
    assert captured["inputs"].account_id == "ACC-EXPLICIT"
    assert captured["inputs"].zone_id == "ZONE-EXPLICIT"


def test_setup_google_creds_via_flags_no_prompt(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Secret passed as a flag must reach run_setup verbatim, with zero prompts
    consumed — closing stdin proves nothing reads from a prompt/getpass."""
    config_path = tmp_path / "config.toml"
    _write_starter_config(config_path)

    def fake_cloudflare(*, api_token: str) -> SimpleNamespace:
        del api_token
        return SimpleNamespace(
            accounts=SimpleNamespace(list=lambda **_k: [SimpleNamespace(id="ACC", name="A")]),
            zones=SimpleNamespace(
                list=lambda **_k: [SimpleNamespace(id="ZONE", name="example.com")]
            ),
        )

    import cloudflare as cf_mod

    monkeypatch.setattr(cf_mod, "Cloudflare", fake_cloudflare)

    captured: dict[str, Any] = {}

    def fake_run_setup(inputs: cloudflare_setup.SetupInputs) -> cloudflare_setup.SetupResult:
        captured["inputs"] = inputs
        return cloudflare_setup.SetupResult(
            tunnel_id="T",
            tunnel_credentials_path=tmp_path / "T.json",
            dns_record_id="D",
            idp_id="I",
            application_id="A",
            policy_id="P",
        )

    monkeypatch.setattr(cli_mod.cloudflare_setup, "run_setup", fake_run_setup)

    secret = "GOCSPX-abcDEF1234567890_full-length-secret"
    result = runner.invoke(
        app,
        [
            "setup",
            "--config",
            str(config_path),
            "--token",
            "TOKEN",
            "--email",
            "me@example.com",
            "--google-client-id",
            "1234.apps.googleusercontent.com",
            "--google-client-secret",
            secret,
        ],
        input="",  # no stdin: any prompt would error, proving none fire
    )

    assert result.exit_code == 0, result.output
    assert captured["inputs"].google_client_id == "1234.apps.googleusercontent.com"
    assert captured["inputs"].google_client_secret == secret


def test_setup_collects_multiple_emails(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    _write_starter_config(config_path)

    def fake_cloudflare(*, api_token: str) -> SimpleNamespace:
        del api_token
        return SimpleNamespace(
            accounts=SimpleNamespace(list=lambda **_k: [SimpleNamespace(id="ACC", name="A")]),
            zones=SimpleNamespace(
                list=lambda **_k: [SimpleNamespace(id="ZONE", name="example.com")]
            ),
        )

    import cloudflare as cf_mod

    monkeypatch.setattr(cf_mod, "Cloudflare", fake_cloudflare)

    captured: dict[str, Any] = {}

    def fake_run_setup(inputs: cloudflare_setup.SetupInputs) -> cloudflare_setup.SetupResult:
        captured["inputs"] = inputs
        return cloudflare_setup.SetupResult(
            tunnel_id="T",
            tunnel_credentials_path=tmp_path / "T.json",
            dns_record_id="D",
            idp_id="I",
            application_id="A",
            policy_id="P",
        )

    monkeypatch.setattr(cli_mod.cloudflare_setup, "run_setup", fake_run_setup)

    result = runner.invoke(
        app,
        [
            "setup",
            "--config",
            str(config_path),
            "--token",
            "TOKEN",
            "--email",
            "a@example.com",
            "--email",
            "b@example.com, c@example.com",
        ],
        input="GCID\nGSEC\n",
    )

    assert result.exit_code == 0, result.output
    assert captured["inputs"].allowed_emails == (
        "a@example.com",
        "b@example.com",
        "c@example.com",
    )


def test_setup_fails_when_hostname_has_no_zone(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config.toml"
    _write_starter_config(config_path)

    def fake_cloudflare(*, api_token: str) -> SimpleNamespace:
        del api_token

        def list_accounts(**_kw: Any) -> list[SimpleNamespace]:
            return [SimpleNamespace(id="ACC-ID", name="My Account")]

        def list_zones(**_kw: Any) -> list[SimpleNamespace]:
            return [SimpleNamespace(id="ZONE-ID", name="other.com")]

        return SimpleNamespace(
            accounts=SimpleNamespace(list=list_accounts),
            zones=SimpleNamespace(list=list_zones),
        )

    import cloudflare as cf_mod

    monkeypatch.setattr(cf_mod, "Cloudflare", fake_cloudflare)

    result = runner.invoke(
        app,
        [
            "setup",
            "--config",
            str(config_path),
            "--token",
            "TOKEN",
            "--email",
            "me@example.com",
        ],
    )

    assert result.exit_code == 1
    assert "no zone" in result.output
