"""Tests for xray_bootstrap.py - the previously-missing Xray base config generator."""
from __future__ import annotations

import json

import pytest

from dist_vpn_agent.config import AgentSettings
from dist_vpn_agent.xray_bootstrap import ensure_base_config


def make_settings(tmp_path, **overrides) -> AgentSettings:
    defaults = dict(
        insecure_dev_mode=True,
        xray_config_host_path=tmp_path / "xray-config.json",
        xray_api_address="127.0.0.1:10086",
    )
    defaults.update(overrides)
    return AgentSettings(**defaults)


def test_generates_config_with_api_stats_and_policy(tmp_path):
    settings = make_settings(tmp_path)

    ensure_base_config(settings)

    assert settings.xray_config_host_path.exists()
    config = json.loads(settings.xray_config_host_path.read_text(encoding="utf-8"))

    assert config["api"]["services"] == ["HandlerService", "RoutingService", "StatsService"]
    assert config["stats"] == {}
    level0 = config["policy"]["levels"]["0"]
    assert level0["statsUserUplink"] is True
    assert level0["statsUserDownlink"] is True
    assert level0["statsUserOnline"] is True

    inbound = config["inbounds"][0]
    assert inbound["port"] == 10086
    assert inbound["listen"] == "0.0.0.0"  # in-container bind, not the host publish address
    assert inbound["protocol"] == "dokodemo-door"
    assert inbound["tag"] == "api"

    assert config["routing"]["rules"][0]["inboundTag"] == ["api"]
    assert config["routing"]["rules"][0]["outboundTag"] == "api"

    # log.access must never be enabled by this generator - the project's privacy
    # stance (TZ-4 SS3.5) - "loglevel" alone doesn't imply an "access" key exists.
    assert "access" not in config["log"]


def test_derives_port_from_xray_api_address(tmp_path):
    settings = make_settings(tmp_path, xray_api_address="127.0.0.1:54321")

    ensure_base_config(settings)

    config = json.loads(settings.xray_config_host_path.read_text(encoding="utf-8"))
    assert config["inbounds"][0]["port"] == 54321


def test_never_overwrites_an_existing_config(tmp_path):
    settings = make_settings(tmp_path)
    settings.xray_config_host_path.parent.mkdir(parents=True, exist_ok=True)
    settings.xray_config_host_path.write_text('{"hand": "tuned"}', encoding="utf-8")

    ensure_base_config(settings)

    assert json.loads(settings.xray_config_host_path.read_text(encoding="utf-8")) == {"hand": "tuned"}


def test_creates_parent_directories(tmp_path):
    settings = make_settings(tmp_path, xray_config_host_path=tmp_path / "nested" / "dir" / "xray-config.json")

    ensure_base_config(settings)

    assert settings.xray_config_host_path.exists()


def test_rejects_malformed_api_address(tmp_path):
    # Trailing colon, no port - rpartition(":") gives an empty port_str, the case
    # the explicit check (not just int()'s own error) exists to catch with a
    # friendlier message than "invalid literal for int()".
    settings = make_settings(tmp_path, xray_api_address="127.0.0.1:")

    with pytest.raises(ValueError, match="host:port"):
        ensure_base_config(settings)
