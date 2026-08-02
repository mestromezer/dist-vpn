"""Shared fixtures for the agent test suite."""
from __future__ import annotations

import os

import pytest


@pytest.fixture
def container_egress() -> None:
    """CI-only escape hatch for tests whose real Docker container needs to
    reach something outside its own network namespace - a real external
    host, or even just attempt a route to a deliberately non-routable
    "black hole" address (see test_connectivity_test.py::
    test_real_timeout_via_blackhole_target, which needs a route *attempt*,
    not a real destination, and still fails on GitHub Actions'
    ubuntu-latest runners). Confirmed empirically by running this suite
    there, not guessed: spawned Docker containers on that runner get no
    outbound network path at all, not even to a non-routable address -
    every test that tries to reach anything past the container's own
    loopback fails there (test_real_proxy_no_response_via_refused_redirect,
    which redirects to 127.0.0.1 inside the container, is unaffected -
    consistent with this being a container-egress problem, not a Docker-
    availability one).

    Set only by ci.yml (AGENT_TEST_SKIP_CONTAINER_EGRESS_TESTS=1) for the
    agent-tests job; unset (and therefore never skipped) for local
    development, where these tests keep verifying the real path they're
    named for.
    """
    if os.environ.get("AGENT_TEST_SKIP_CONTAINER_EGRESS_TESTS") == "1":
        pytest.skip("spawned Docker containers have no outbound network egress in this CI environment")
