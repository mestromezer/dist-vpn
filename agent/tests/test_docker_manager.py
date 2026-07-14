"""Tests for docker_manager.py.

Two tiers, per the task this slice was scoped against (be explicit about
which is which -- see the final report for this slice):

1. Unit tests against a mocked docker-py client (``unittest.mock``) --
   verify DockerManager's *decision logic* (create when missing, start when
   stopped, no-op when running, health reporting) without needing a real
   Docker Engine at all. These always run.

2. A real integration test against genuine Docker Desktop (this sandboxed
   environment happened to have it installed and running -- see the final
   report) that creates, starts, stops, and restarts an actual container
   from the real configured Xray-core image with a real minimal Xray
   config, via DockerManager's real code path (not re-implemented against
   the Docker CLI). Skipped with a clear reason if Docker is unreachable
   rather than failing the whole suite in environments without it.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import docker
import pytest
from docker.errors import DockerException, ImageNotFound, NotFound

from dist_vpn_agent.config import AgentSettings
from dist_vpn_agent.docker_manager import DockerManager, DockerManagerError


def make_settings(**overrides) -> AgentSettings:
    defaults = dict(
        insecure_dev_mode=True,  # sidesteps unrelated mTLS validation for these tests
        xray_container_name="dist-vpn-xray-core-test",
        xray_docker_image="ghcr.io/xtls/xray-core:latest",
        xray_api_address="127.0.0.1:10086",
    )
    defaults.update(overrides)
    return AgentSettings(**defaults)


def _fake_container(status: str) -> MagicMock:
    container = MagicMock()
    container.status = status

    def _reload():
        pass  # status already set on the mock; nothing to refresh

    container.reload.side_effect = _reload
    return container


# --- Unit tests (mocked docker-py client) -----------------------------------------


def test_ensure_running_creates_container_when_missing(tmp_path):
    settings = make_settings(xray_config_host_path=tmp_path / "config.json")
    client = MagicMock()
    # docker-py's real containers.create() never auto-starts a container --
    # it comes back in "created" status, not "running", until .start() is
    # called explicitly. Faking it as already "running" here would make
    # ensure_running()'s own (correct) "if not running: start()" check skip
    # calling .start() -- which is exactly the gap this test exists to
    # catch. Mirror the transition the same way
    # test_ensure_running_starts_stopped_container_without_recreating does.
    created = _fake_container("created")

    def _start():
        created.status = "running"

    created.start.side_effect = _start
    client.containers.get.side_effect = NotFound("no such container")
    client.images.get.return_value = MagicMock()  # image already present locally
    client.containers.create.return_value = created

    manager = DockerManager(settings, client=client)
    result = manager.ensure_running()

    client.containers.create.assert_called_once()
    kwargs = client.containers.create.call_args.kwargs
    assert kwargs["name"] == "dist-vpn-xray-core-test"
    assert kwargs["command"] == ["run", "-config", "/etc/xray/config.json"]
    assert kwargs["ports"] == {"10086/tcp": ("127.0.0.1", 10086)}
    created.start.assert_called_once()
    assert result.running is True


def test_ensure_running_pulls_image_when_not_present_locally(tmp_path):
    settings = make_settings(xray_config_host_path=tmp_path / "config.json")
    client = MagicMock()
    client.containers.get.side_effect = NotFound("no such container")
    client.images.get.side_effect = ImageNotFound("no such image")
    client.containers.create.return_value = _fake_container("running")

    manager = DockerManager(settings, client=client)
    manager.ensure_running()

    client.images.pull.assert_called_once_with(settings.xray_docker_image)


def test_ensure_running_starts_stopped_container_without_recreating(tmp_path):
    settings = make_settings(xray_config_host_path=tmp_path / "config.json")
    client = MagicMock()
    existing = _fake_container("exited")

    def _reload():
        existing.status = "running"  # simulate a real start() taking effect

    existing.start.side_effect = _reload
    client.containers.get.return_value = existing

    manager = DockerManager(settings, client=client)
    result = manager.ensure_running()

    client.containers.create.assert_not_called()
    existing.start.assert_called_once()
    assert result.running is True


def test_ensure_running_is_noop_when_already_running(tmp_path):
    settings = make_settings(xray_config_host_path=tmp_path / "config.json")
    client = MagicMock()
    existing = _fake_container("running")
    client.containers.get.return_value = existing

    manager = DockerManager(settings, client=client)
    result = manager.ensure_running()

    client.containers.create.assert_not_called()
    existing.start.assert_not_called()
    assert result.running is True


def test_health_reports_not_found(tmp_path):
    settings = make_settings(xray_config_host_path=tmp_path / "config.json")
    client = MagicMock()
    client.containers.get.side_effect = NotFound("no such container")

    manager = DockerManager(settings, client=client)
    health = manager.health()

    assert health.running is False
    assert health.status == "not_found"


def test_health_reports_docker_unavailable(tmp_path, monkeypatch):
    settings = make_settings(xray_config_host_path=tmp_path / "config.json")

    def _raise_from_env():
        raise DockerException("Docker Desktop is not running")

    monkeypatch.setattr(docker, "from_env", _raise_from_env)

    manager = DockerManager(settings, client=None)
    health = manager.health()

    assert health.running is False
    assert health.status == "docker_unavailable"
    assert "Docker Desktop is not running" in health.detail


def test_is_running_true_when_container_running(tmp_path):
    settings = make_settings(xray_config_host_path=tmp_path / "config.json")
    client = MagicMock()
    client.containers.get.return_value = _fake_container("running")

    manager = DockerManager(settings, client=client)
    assert manager.is_running() is True


def test_ensure_running_wraps_docker_exception(tmp_path):
    settings = make_settings(xray_config_host_path=tmp_path / "config.json")
    client = MagicMock()
    client.containers.get.side_effect = DockerException("engine is on fire")

    manager = DockerManager(settings, client=client)
    with pytest.raises(DockerManagerError, match="engine is on fire"):
        manager.ensure_running()


# --- Real Docker Desktop integration test -----------------------------------------


def _real_docker_client_or_skip() -> docker.DockerClient:
    try:
        client = docker.from_env()
        client.ping()
    except Exception as err:  # noqa: BLE001 - any failure here means "skip", not "fail"
        pytest.skip(f"no real Docker Engine available in this environment: {err}")
    return client


_TEST_CONTAINER_NAME = "dist-vpn-agent-test-xray-core"
_MINIMAL_XRAY_CONFIG = """
{
  "log": {"loglevel": "warning"},
  "inbounds": [],
  "outbounds": [{"protocol": "freedom", "tag": "direct"}]
}
"""


@pytest.fixture
def real_docker_client():
    client = _real_docker_client_or_skip()
    # Clean slate: remove any leftover container from a previous failed run.
    try:
        client.containers.get(_TEST_CONTAINER_NAME).remove(force=True)
    except NotFound:
        pass
    yield client
    try:
        client.containers.get(_TEST_CONTAINER_NAME).remove(force=True)
    except NotFound:
        pass


def test_real_docker_engine_create_start_stop_restart_cycle(tmp_path, real_docker_client):
    """End-to-end against genuine Docker Desktop: create from the real
    configured Xray-core image with a real (minimal) Xray config, verify
    it's actually running, verify ensure_running() is idempotent, then stop
    it out-of-band (simulating a crash/manual stop) and verify
    ensure_running() notices and restarts it -- all through DockerManager's
    real code, not a re-implementation of it.
    """
    config_path = tmp_path / "config.json"
    config_path.write_text(_MINIMAL_XRAY_CONFIG)

    settings = make_settings(
        insecure_dev_mode=True,
        xray_container_name=_TEST_CONTAINER_NAME,
        xray_docker_image="ghcr.io/xtls/xray-core:latest",
        xray_api_address="127.0.0.1:19999",
        xray_config_host_path=config_path,
    )

    manager = DockerManager(settings, client=real_docker_client)

    result = manager.ensure_running()
    assert result.running is True
    assert manager.is_running() is True

    # Idempotent: calling again with the container already running must not
    # error and must not recreate it.
    container_id_before = real_docker_client.containers.get(_TEST_CONTAINER_NAME).id
    manager.ensure_running()
    container_id_after = real_docker_client.containers.get(_TEST_CONTAINER_NAME).id
    assert container_id_before == container_id_after

    # Simulate the container having stopped (crash, manual `docker stop`, ...).
    real_docker_client.containers.get(_TEST_CONTAINER_NAME).stop(timeout=2)
    # A moment for Docker to settle the status transition.
    for _ in range(20):
        if not manager.is_running():
            break
        time.sleep(0.25)
    assert manager.is_running() is False

    result = manager.ensure_running()
    assert result.running is True
    assert manager.is_running() is True
