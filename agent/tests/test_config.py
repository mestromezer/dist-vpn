"""Tests for AgentSettings mTLS validation, in particular that the very
first bootstrap enrollment (no node-specific client cert yet, see TZ-4 SS3.1
/SS5.2) is a representable, valid configuration: CA path only, no client
cert/key.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from dist_vpn_agent.config import AgentSettings


def test_insecure_dev_mode_needs_no_tls_material():
    settings = AgentSettings(insecure_dev_mode=True)
    assert settings.insecure_dev_mode is True


def test_secure_mode_requires_ca_path():
    with pytest.raises(ValidationError, match="mtls_ca_cert_path"):
        AgentSettings(insecure_dev_mode=False)


def test_secure_mode_with_only_ca_is_valid_bootstrap_configuration(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("fake-ca")
    settings = AgentSettings(insecure_dev_mode=False, mtls_ca_cert_path=ca)
    assert settings.mtls_client_cert_path is None
    assert settings.mtls_client_key_path is None


def test_secure_mode_with_mismatched_client_cert_key_is_rejected(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("fake-ca")
    cert = tmp_path / "client.pem"
    cert.write_text("fake-cert")
    with pytest.raises(ValidationError, match="mtls_client_cert_path"):
        AgentSettings(insecure_dev_mode=False, mtls_ca_cert_path=ca, mtls_client_cert_path=cert)


def test_secure_mode_with_full_client_identity_is_valid(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("fake-ca")
    cert = tmp_path / "client.pem"
    cert.write_text("fake-cert")
    key = tmp_path / "client.key"
    key.write_text("fake-key")
    settings = AgentSettings(
        insecure_dev_mode=False,
        mtls_ca_cert_path=ca,
        mtls_client_cert_path=cert,
        mtls_client_key_path=key,
    )
    assert settings.mtls_client_cert_path == cert
    assert settings.mtls_client_key_path == key
