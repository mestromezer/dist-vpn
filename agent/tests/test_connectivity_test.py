"""Tests for connectivity_test.py (``RunTest`` / TZ-4 SS3.4 ephemeral-client
connectivity probing).

Two tiers, mirroring this project's established convention (see
tests/test_docker_manager.py / tests/test_xray_client.py's own "mock vs
real" split, and the final report for this slice for the same summary in
prose):

1. Pure unit tests (no Docker) for logic that doesn't need it: the
   TLS-failure log-grep heuristic (``_looks_like_tls_failure``), and the
   "invalid preset JSON" input-validation path (never reaches Docker at
   all, by design -- see ``ConnectivityTester._run_probe``).

2. A real Docker Engine + genuine ``ghcr.io/xtls/xray-core`` container tier
   (skipped with a clear reason if Docker is unreachable, same pattern as
   the other two real-infrastructure test modules in this project). **All
   five** ``TestOutcome`` values are exercised for real here, against a
   real Xray-core process actually failing/succeeding in the way each test
   claims -- none of the five are simulated by mocking ``requests`` or
   docker-py. Every scenario below was independently reproduced against
   real Docker during this slice's development before being written up as
   a test (see each test's docstring for the specific real mechanism, and
   the final report for this slice for the raw command output that first
   demonstrated each one).

   Caveat that is NOT covered by the Docker-availability skip: the
   SUCCESS/TIMEOUT/PROXY_NO_RESPONSE/TLS_HANDSHAKE_FAILED tests below need
   real outbound internet access from wherever this suite runs (they talk
   to example.com/neverssl.com, or rely on a real network-level timeout to
   a non-routable address). There is no clean, fast way to detect "Docker
   is up but this host has no internet" ahead of time the way "Docker is
   unreachable" can be detected -- if that specific combination occurs,
   these tests fail outright rather than skip. See agent/README.md.
"""

from __future__ import annotations

import json

import docker
import pytest

from dist_vpn_agent.config import AgentSettings
from dist_vpn_agent.connectivity_test import ConnectivityTester, _looks_like_tls_failure
from dist_vpn_agent.generated import agent_control_pb2 as pb2


def make_settings(**overrides) -> AgentSettings:
    defaults = dict(insecure_dev_mode=True, xray_docker_image="ghcr.io/xtls/xray-core:latest")
    defaults.update(overrides)
    return AgentSettings(**defaults)


FREEDOM_PRESET = json.dumps({"protocol": "freedom"})


# --- Unit tests (no Docker) --------------------------------------------------------


def test_looks_like_tls_failure_matches_real_xray_log_wording():
    # Real log text captured from a genuine Xray-core TLS handshake failure
    # during this slice's development (see
    # test_real_tls_handshake_failed_vless_tls_to_plain_http_endpoint below
    # for the live reproduction) -- not invented/guessed wording.
    real_log = (
        "app/proxyman/outbound: app/proxyman/outbound: failed to process outbound "
        "traffic > proxy/vless/outbound: failed to find an available destination > "
        "common/retry: [tls: first record does not look like a TLS handshake]"
    )
    assert _looks_like_tls_failure(real_log)


def test_looks_like_tls_failure_false_for_unrelated_logs():
    # Real log text from test_real_proxy_no_response_via_refused_redirect
    # below -- a genuine non-TLS proxy failure must NOT be misclassified.
    assert not _looks_like_tls_failure(
        "Xray 26.3.27 (Xray, Penetrates Everything.) Custom (go1.26.1 linux/amd64)\n"
        "core: Xray started"
    )


def test_run_test_invalid_preset_json_never_touches_docker():
    # No Docker fixture used here at all -- if this test can pass without
    # a real (or even mocked) Docker client, that itself demonstrates the
    # input-validation short-circuit happens strictly before any Docker
    # call, exactly as ConnectivityTester._run_probe's docstring claims.
    tester = ConnectivityTester(make_settings())
    result = tester.run_test(
        pb2.RunTest(
            test_id="t1", preset_client_config_json="not json", probe_url="http://example.com", timeout_seconds=5
        )
    )
    assert result.outcome == pb2.TEST_OUTCOME_CONTAINER_START_FAILED
    assert "invalid preset_client_config_json" in result.detail


def test_run_test_preset_json_missing_protocol_key_never_touches_docker():
    tester = ConnectivityTester(make_settings())
    result = tester.run_test(
        pb2.RunTest(
            test_id="t1",
            preset_client_config_json=json.dumps({"settings": {}}),
            probe_url="http://example.com",
            timeout_seconds=5,
        )
    )
    assert result.outcome == pb2.TEST_OUTCOME_CONTAINER_START_FAILED
    assert "protocol" in result.detail


# --- Real Docker + real Xray-core tier ----------------------------------------------


def _real_docker_client_or_skip() -> docker.DockerClient:
    try:
        client = docker.from_env()
        client.ping()
    except Exception as err:  # noqa: BLE001 - any failure here means "skip", not "fail"
        pytest.skip(f"no real Docker Engine available in this environment: {err}")
    return client


@pytest.fixture
def real_tester():
    client = _real_docker_client_or_skip()
    return ConnectivityTester(make_settings(), client=client)


def test_real_success_via_freedom_outbound_to_example_com(real_tester, container_egress):
    """Real, full path: a real ephemeral Docker container running real
    Xray-core with a freedom (direct-egress) outbound, a real SOCKS5 proxy,
    and a real HTTP GET through it to https://example.com. This is the
    "happy path" TZ-4 SS3.4 describes -- verified for real, not simulated.
    Needs real outbound internet access from this test environment (see
    module docstring).
    """
    result = real_tester.run_test(
        pb2.RunTest(
            test_id="t-success",
            preset_client_config_json=FREEDOM_PRESET,
            probe_url="https://example.com",
            timeout_seconds=10,
        )
    )
    assert result.outcome == pb2.TEST_OUTCOME_SUCCESS
    assert result.latency_ms > 0
    assert "200" in result.detail


def test_real_container_start_failed_on_broken_preset_protocol(real_tester, container_egress):
    """Real: a preset naming a protocol Xray's config loader doesn't
    recognize makes the real container exit almost immediately (verified
    during this slice's development: real exit code 23, Xray's own log
    literally says '... unknown config id: not-a-real-protocol'). Asserted
    here as a black-box outcome only (not the exact exit code or log
    wording, which could change between Xray versions) -- see
    connectivity_test.py's ``_wait_for_container_port`` for the real status/
    exit-code polling this outcome depends on.

    Needs container_egress too, despite not being "about" reaching
    example.com: the outcome-classification path still runs the actual
    probe_url attempt, so a container-network-egress failure here gets
    misclassified as PROXY_NO_RESPONSE instead of CONTAINER_START_FAILED -
    confirmed by the CI run that first surfaced this (see the fixture's own
    docstring).
    """
    broken_preset = json.dumps({"protocol": "not-a-real-protocol", "settings": {}})
    result = real_tester.run_test(
        pb2.RunTest(
            test_id="t-badstart",
            preset_client_config_json=broken_preset,
            probe_url="https://example.com",
            timeout_seconds=10,
        )
    )
    assert result.outcome == pb2.TEST_OUTCOME_CONTAINER_START_FAILED


def test_real_tls_handshake_failed_vless_tls_to_plain_http_endpoint(real_tester, container_egress):
    """Real, best-effort-*classified* (see connectivity_test.py module
    docstring for why TLS_HANDSHAKE_FAILED can only ever be best-effort
    from a black-box SOCKS client): a vless+tls outbound pointed at a
    plain-HTTP endpoint (example.com:80, no TLS listener there at all)
    makes Xray's own TLS ClientHello get back non-TLS response bytes and
    fail its handshake. What actually reaches this Agent's Python process
    is a generic ``requests.exceptions.ConnectionError`` (SOCKS5 has no
    "TLS handshake failed" reply code, see module docstring) -- it's
    ``_looks_like_tls_failure`` grepping the ephemeral container's own real
    Xray logs (which do say "tls: first record does not look like a TLS
    handshake", captured live during this slice's development) that
    reclassifies it correctly. Needs real outbound internet access (see
    module docstring).
    """
    tls_preset = json.dumps(
        {
            "protocol": "vless",
            "settings": {
                "vnext": [
                    {
                        "address": "example.com",
                        "port": 80,
                        "users": [{"id": "00000000-0000-0000-0000-000000000000", "encryption": "none"}],
                    }
                ]
            },
            "streamSettings": {
                "network": "tcp",
                "security": "tls",
                "tlsSettings": {"serverName": "example.com"},
            },
        }
    )
    result = real_tester.run_test(
        pb2.RunTest(
            test_id="t-tls", preset_client_config_json=tls_preset, probe_url="http://neverssl.com/", timeout_seconds=10
        )
    )
    assert result.outcome == pb2.TEST_OUTCOME_TLS_HANDSHAKE_FAILED


def test_real_proxy_no_response_via_refused_redirect(real_tester):
    """Real: a freedom outbound with Xray's own ``redirect`` setting forces
    every connection to a fixed ``127.0.0.1:1`` inside the ephemeral
    container -- nothing listens there, so the OS gives a real, immediate
    connection-refused. Reachable entirely through the normal one-outbound
    preset contract (no need to hand-craft routing/config outside
    ``ConnectivityTester``'s own builder) -- this was the real reproduction
    used during this slice's development to verify PROXY_NO_RESPONSE is a
    fast ``ConnectionError`` distinct from TIMEOUT, and that its log content
    (no TLS-related text) correctly does NOT get reclassified as
    TLS_HANDSHAKE_FAILED by the best-effort heuristic.
    """
    redirect_preset = json.dumps({"protocol": "freedom", "settings": {"redirect": "127.0.0.1:1"}})
    result = real_tester.run_test(
        pb2.RunTest(
            test_id="t-noresp",
            preset_client_config_json=redirect_preset,
            probe_url="https://example.com",
            timeout_seconds=10,
        )
    )
    assert result.outcome == pb2.TEST_OUTCOME_PROXY_NO_RESPONSE


def test_real_timeout_via_blackhole_target(real_tester, container_egress):
    """Real: freedom outbound (no redirect -- goes wherever the probe asks),
    probing a well-known non-routable-in-practice address
    (``10.255.255.1``) with a short ``timeout_seconds``. Real Xray-core
    genuinely never gets a definitive answer (no route, no RST) within the
    budget, so ``requests`` itself raises ``requests.exceptions.Timeout`` --
    the precise (not best-effort) branch of the outcome mapping, see
    connectivity_test.py module docstring.
    """
    result = real_tester.run_test(
        pb2.RunTest(
            test_id="t-timeout",
            preset_client_config_json=FREEDOM_PRESET,
            probe_url="http://10.255.255.1/",
            timeout_seconds=2,
        )
    )
    assert result.outcome == pb2.TEST_OUTCOME_TIMEOUT
    assert result.latency_ms >= 2000


def test_real_ephemeral_container_is_always_removed_after_a_probe(real_tester):
    """TZ-4 SS3.4 / connectivity_test.py module docstring point 4: the
    ephemeral container must always be torn down, success or failure.
    Verified for real: list containers matching this module's naming
    prefix after a (successful) probe and assert none remain. Docker's own
    AutoRemove (``remove=True`` at creation) completes asynchronously after
    ``stop()`` returns -- a short poll (not an immediate single check)
    avoids a real, observed race against that.

    ``docker-py``'s own ``containers.list()`` has a real, separately-
    observed race of its own: internally it lists matching container IDs
    then calls ``.get(id)`` on each to build model objects, and if
    AutoRemove wins that specific race the ``.get()`` 404s as
    ``docker.errors.NotFound`` instead of the list simply coming back
    empty. Treated the same as "no leftovers" here -- it's evidence
    cleanup already happened, not a real failure of this test.
    """
    import time  # noqa: PLC0415 - test-local

    from docker.errors import NotFound  # noqa: PLC0415 - test-local

    from dist_vpn_agent.connectivity_test import _CONTAINER_NAME_PREFIX  # noqa: PLC0415 - test-local

    real_tester.run_test(
        pb2.RunTest(
            test_id="t-cleanup",
            preset_client_config_json=FREEDOM_PRESET,
            probe_url="https://example.com",
            timeout_seconds=10,
        )
    )

    def _list_matching() -> list:
        try:
            return client.containers.list(all=True, filters={"name": _CONTAINER_NAME_PREFIX})
        except NotFound:
            return []

    client = real_tester._get_client()
    deadline = time.monotonic() + 10
    leftover = _list_matching()
    while leftover and time.monotonic() < deadline:
        time.sleep(0.5)
        leftover = _list_matching()
    assert leftover == []
