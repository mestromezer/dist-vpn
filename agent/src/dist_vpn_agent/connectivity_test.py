"""``RunTest`` (TZ-4 SS3.4): ephemeral-client connectivity probing.

Implements the ``Command.run_test`` operation control_client.py dispatches
specially (not through ``_COMMAND_DISPATCH``, see that module -- this op's
result is a ``TestResult``, not a ``CommandResult``, see below):

1. Start an ephemeral Xray-core container (same image as the long-lived one
   this Agent manages, ``settings.xray_docker_image`` -- TZ-4 SS3.4: "тот же
   образ, что и основной Xray"), built around the caller-supplied preset/
   outbound settings, with its own local SOCKS5 inbound. Always removed on
   exit, success or failure (``AutoRemove`` + a defensive explicit
   ``remove(force=True)`` in ``finally``) -- this is a short-lived, one-shot
   probe, never a container this Agent keeps around or reuses.
2. Once its SOCKS5 port is reachable, issue an HTTP GET to ``probe_url``
   *through* that proxy (``requests`` + PySocks, ``socks5h://`` so DNS
   resolution also happens through the tunnel -- TZ-4 SS3.4 wants to test the
   whole channel, not just this host's own DNS).
3. Map whatever happened to a ``TestOutcome`` (see ``_run_probe`` below for
   the exact decision tree and, importantly, which of the five outcomes are
   precisely determined vs. best-effort).

--- ``preset_client_config_json`` shape: a design decision, not fixed by the
    contract (flagged for Platform-side reconciliation, see agent/README.md
    "Замеченные вопросы к контракту") ---

``agent_control.proto``'s comment on ``RunTest.preset_client_config_json``
only says "тестовый клиентский конфиг, см. TZ-4 §3.4"; TZ-4 SS3.4 itself
says the ephemeral client is raised "с параметрами конкретного пресета/узла"
(with the parameters of a specific preset/node) -- i.e. the *preset*, not a
complete Xray config file. This module reads ``preset_client_config_json`` as
exactly that: one Xray *outbound* object in Xray's native static-config-file
JSON dialect (the same ``{"protocol": ..., "settings": {...}, "streamSettings":
{...}}`` shape as e.g. a real client's ``outbounds[0]``, and incidentally the
same key names xray_client.py's own ``AddOutbound.outbound_json`` envelope
uses for the *live-management* path -- see that module's docstring for why
that dialect was chosen there; the resemblance here is deliberate, for
consistency, even though this is a genuinely different code path writing a
static config *file* for a fresh ``xray run``, not a protobuf RPC to a
running instance, so no protobuf JSON translation is needed here at all).
This module wraps that one outbound with a local SOCKS5 inbound and a single
"send everything to it" routing rule to build the complete ephemeral config
file -- see ``_build_ephemeral_config``.

The alternative reading (``preset_client_config_json`` is the *entire*
already-complete Xray client config file, inbound and all) was considered and
rejected: it would require the caller (Platform) to already know/choose a
free local SOCKS port and this module to parse arbitrary inbound JSON back
out to find it, for no real benefit -- the "which local port is the SOCKS
proxy on" question is this module's own implementation detail (answered via
Docker's dynamic port mapping, see ``_wait_for_container_port``), not
something a caller should need to predict.

--- Outcome-mapping precision (TZ-4 SS3.4 / SS8 wants "минимум" these five,
    distinguishing *where* the channel breaks) ---

- ``TEST_OUTCOME_SUCCESS`` / ``TEST_OUTCOME_CONTAINER_START_FAILED`` /
  ``TEST_OUTCOME_TIMEOUT`` are precise: container-start failure is Docker
  reporting the container never reached ``running`` (or exited immediately)
  within a bounded grace period, and timeout is exactly
  ``requests`` raising ``requests.exceptions.Timeout`` -- i.e. the probe
  genuinely did not complete within ``timeout_seconds``, for whatever reason
  (see ``_run_probe``'s docstring for why "no route" and "genuinely slow"
  often can't be told apart from a black-box SOCKS client's point of view,
  and TIMEOUT is the honest bucket for that ambiguity, not
  PROXY_NO_RESPONSE).
- ``TEST_OUTCOME_TLS_HANDSHAKE_FAILED`` is explicitly best-effort, and
  *every* connection failure gets the same treatment -- including
  ``requests.exceptions.SSLError``, which is deliberately NOT given an
  automatic pass to this outcome (see ``_probe_through_socks``'s comment for
  why: real testing during this slice found ``SSLError`` can also fire when
  the SOCKS tunnel is torn down mid-handshake for a reason that has nothing
  to do with TLS at all, e.g. the ephemeral client's own outbound
  ``redirect``ing to a refused address -- Xray accepts the SOCKS CONNECT
  optimistically, ``requests`` starts its (client<->origin) TLS handshake
  into the tunnel, and only *then* does Xray discover its own outbound
  target refuses the connection and kill the tunnel, which ``requests``
  reports as ``SSLError(SSLEOFError(...))`` even though no certificate or
  TLS parameter was ever actually involved). A SOCKS5 proxy's wire protocol
  has no "TLS handshake failed" reply code either way (RFC 1928 SS6 only has
  generic failure/refused/unreachable/etc.), so when the ephemeral client's
  *own* outbound TLS/REALITY handshake to the target node fails, the only
  reliable-enough signal available to this black-box test runner is Xray's
  own log output. Real (see final report for this slice, and this module's
  tests): Xray *does* log the real reason (e.g. ``"tls: first record does
  not look like a TLS handshake"``, ``"REALITY"``, certificate/x509 errors)
  to its own stdout, which this Agent already captures (``container.logs()``)
  for the failure detail regardless -- **at ``log.loglevel: "info"``, not
  Xray's default/quieter ``"warning"``**, see ``_build_ephemeral_config``'s
  docstring for why that specific, narrow exception to how quiet this
  Agent otherwise keeps Xray's logs was necessary (found the hard way: at
  ``"warning"``, the exact line this classifier needs is silently dropped
  and every real handshake failure degraded to ``PROXY_NO_RESPONSE``).
  ``_looks_like_tls_failure`` greps that text for a small set of TLS/
  REALITY-related markers and, if found, reclassifies what would otherwise
  be a generic ``PROXY_NO_RESPONSE`` as ``TLS_HANDSHAKE_FAILED``.
  This is a heuristic on Xray's own log wording, not a protocol-level
  signal -- a real Xray version that rewords or localizes that message would
  silently fall back to ``PROXY_NO_RESPONSE`` (a defensible fallback, not a
  crash, but a reclassification a maintainer updating Xray's pinned version
  should be aware could regress quietly). One known, accepted gap this
  leaves: a genuine client<->origin certificate failure (e.g. probe_url's
  own HTTPS cert is expired/wrong-host) on a *non-TLS* outbound (e.g.
  freedom) would raise ``SSLError`` with no TLS-related text in Xray's own
  logs at all (Xray is just relaying bytes, unaware of the client's TLS) --
  this module classifies that as ``PROXY_NO_RESPONSE``, not
  ``TLS_HANDSHAKE_FAILED``, which is defensible (a TLS handshake *did* fail)
  but imprecise about *which* handshake. Not resolved in this slice; flagged
  here rather than silently accepted.
- ``TEST_OUTCOME_PROXY_NO_RESPONSE`` is the residual bucket: a definitive,
  fast, non-timeout connection failure from the SOCKS layer that
  ``_looks_like_tls_failure`` didn't recognize as TLS-related (e.g. Xray's
  own dispatcher immediately refusing because the target/routing is
  unreachable, or the local proxy port itself never came up in time -- see
  ``_wait_for_socks_port_reachable``).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import socket
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import docker
import requests
import requests.exceptions
from docker.errors import DockerException, NotFound

from dist_vpn_agent.config import AgentSettings
from dist_vpn_agent.docker_manager import DockerManagerError
from dist_vpn_agent.generated import agent_control_pb2 as pb2

logger = logging.getLogger(__name__)

# Path inside the ephemeral container the entrypoint is invoked with -- same
# convention as docker_manager.py's long-lived container (see
# _CONTAINER_CONFIG_PATH there), kept as an independent constant rather than
# a shared import because the two are conceptually unrelated containers that
# happen to agree on a path by convention, not by coupling.
_CONTAINER_CONFIG_PATH = "/etc/xray/config.json"

# The ephemeral client's own local SOCKS5 inbound, inside the container.
# Fixed (only the *host* side port is dynamic, see _wait_for_container_port)
# since this port is never reachable from outside the container's own
# network namespace/published-port mapping.
_SOCKS_CONTAINER_PORT = 1080

# Name prefix for ephemeral test containers -- distinct from
# settings.xray_container_name (the one long-lived container docker_manager.py
# owns) so there is never any risk of these two code paths colliding on a
# name or accidentally touching each other's container. A short random
# suffix is appended per run (see _run_probe) so concurrent RunTest commands
# (plausible: Platform re-testing several nodes/presets around the same
# time) never collide with each other either.
_CONTAINER_NAME_PREFIX = "dist-vpn-agent-test-"

# Bounded grace periods, *not* counted against the caller's
# ``timeout_seconds`` -- TZ-4 SS4 is explicit that the test-request budget is
# for "network RTT", with container start-up called out as legitimate
# overhead *beyond* that budget ("не больше нескольких секунд сверх времени,
# необходимого на сетевой RTT и поднятие эфемерного контейнера"). Only the
# actual HTTP probe (_run_probe's requests.get call) is bounded by
# timeout_seconds.
_CONTAINER_START_GRACE_SECONDS = 15.0
_SOCKS_READY_GRACE_SECONDS = 10.0

# Best-effort classifier for TLS_HANDSHAKE_FAILED -- see module docstring.
# Verified against a real Xray-core container's actual log wording for a
# real TLS handshake failure (vless+tls outbound pointed at a plain-HTTP
# endpoint): "app/proxyman/outbound: ... failed to find an available
# destination > common/retry: [tls: first record does not look like a TLS
# handshake]". Deliberately broad/lowercase substrings, not an exact-message
# match, so close variants (REALITY failures, x509/certificate errors) are
# also caught without hardcoding every possible Xray log string.
_TLS_FAILURE_LOG_MARKERS = ("tls:", "tls error", "reality", "certificate", "x509", "handshake")

# Xray container names must match Docker's own naming rules; test_id is
# caller-supplied (Platform), not trusted to already be a valid Docker name
# component -- sanitize defensively rather than let a stray character 500
# the whole probe before it even starts.
_UNSAFE_NAME_CHARS = re.compile(r"[^a-zA-Z0-9_.-]")


def _sanitize_for_container_name(value: str) -> str:
    cleaned = _UNSAFE_NAME_CHARS.sub("-", value).strip("-")
    return cleaned[:40] if cleaned else "test"


def _looks_like_tls_failure(log_text: str) -> bool:
    lowered = log_text.lower()
    return any(marker in lowered for marker in _TLS_FAILURE_LOG_MARKERS)


def _build_ephemeral_config(preset_outbound: dict[str, Any]) -> dict[str, Any]:
    """Wrap the caller's outbound/preset object with a local SOCKS5 inbound
    and a single catch-all routing rule -- see module docstring for why
    ``preset_outbound`` is read as just the outbound, not a whole config.

    ``log.access: "none"`` is set unconditionally and without exception
    (TZ-4 SS3.5 privacy default -- "обязательное поле в каждом конфиге,
    который Agent генерирует, без исключений" -- applies just as much to
    this short-lived test config as to the long-lived one): this disables
    Xray's structured per-connection access log entirely, regardless of
    what else this function does.

    ``log.loglevel: "info"`` (**not** ``"warning"``, unlike a real client
    preset would reasonably use) is a deliberate, narrow exception to how
    quiet this Agent otherwise keeps Xray's logs, found necessary during
    this slice's real testing: Xray logs the one line
    ``_looks_like_tls_failure`` depends on (e.g. ``"tls: first record does
    not look like a TLS handshake"``) at ``[Info]`` level, inside its
    general/error log stream -- **not** the access log ``log.access``
    already disables. At ``"warning"``, that line (and everything else
    needed to distinguish *why* a probe failed) is silently dropped, and
    every real handshake failure this module tested degraded to
    ``PROXY_NO_RESPONSE`` even though the real cause was determinable. This
    is judged an acceptable, narrow exception to the "no destination
    addresses in logs" principle specifically here, not a general
    relaxation of it: (1) this container exists for exactly one
    operator/Platform-initiated diagnostic probe, never real end-user
    traffic; (2) its whole stdout disappears with it the moment
    ``_teardown`` runs -- nothing is written to a persistent log file or
    exposed outside this Agent process; (3) the only destination these
    ``[Info]`` lines can possibly reveal is ``probe_url`` itself, which
    Platform already supplied in the ``RunTest`` command -- this reveals no
    information Platform didn't already have, unlike the real end-user
    traffic destinations SS3.5 is actually protecting. If a reviewer
    disagrees with this trade-off, the fix is narrow: keep ``"warning"``
    and accept that TLS_HANDSHAKE_FAILED then degrades to always being
    PROXY_NO_RESPONSE (a real capability loss, not just a cosmetic one).
    """
    outbound = dict(preset_outbound)
    outbound["tag"] = "test-out"  # wins over anything the caller set -- routing below depends on it
    return {
        "log": {"loglevel": "info", "access": "none"},
        "inbounds": [
            {
                "listen": "0.0.0.0",
                "port": _SOCKS_CONTAINER_PORT,
                "protocol": "socks",
                "settings": {"udp": False},
                "tag": "test-in",
            }
        ],
        "outbounds": [outbound],
        "routing": {"rules": [{"type": "field", "inboundTag": ["test-in"], "outboundTag": "test-out"}]},
    }


def _tail_logs(container: Any, tail: int = 40) -> str:
    try:
        return container.logs(tail=tail).decode("utf-8", errors="replace").strip()
    except DockerException:
        return "<could not read ephemeral test container logs>"


class ConnectivityTester:
    """Owns the ephemeral-container lifecycle and HTTP-through-SOCKS probe
    for ``RunTest``. Deliberately separate from ``DockerManager``: that class
    owns exactly *one* long-lived, named container and its docstring is
    explicit that it "never touches any other container" -- ephemeral,
    short-lived, uniquely-named-per-probe test containers are a genuinely
    different lifecycle (create, probe, always tear down) and mixing the two
    into one class would blur that invariant. Mirrors DockerManager's own
    testability pattern instead (injectable ``client``, lazy
    ``docker.from_env()``) for consistency.
    """

    def __init__(self, settings: AgentSettings, client: docker.DockerClient | None = None) -> None:
        self._settings = settings
        self._client = client

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.from_env()
            except DockerException as err:
                raise DockerManagerError(f"cannot reach Docker Engine API: {err}") from err
        return self._client

    # -- public API --------------------------------------------------------------

    def run_test(self, run_test: pb2.RunTest) -> pb2.TestResult:
        """Synchronous by design (docker-py and ``requests`` both are) --
        control_client.py calls this via ``asyncio.to_thread`` so a slow or
        wedged probe can't stall the control-plane read/heartbeat loops, the
        same reasoning docker_manager.py's ``health()`` already uses for
        heartbeats.
        """
        outcome, latency_ms, detail = self._run_probe(
            test_id=run_test.test_id,
            preset_client_config_json=run_test.preset_client_config_json,
            probe_url=run_test.probe_url,
            timeout_seconds=run_test.timeout_seconds,
        )
        return pb2.TestResult(
            test_id=run_test.test_id, outcome=outcome, latency_ms=latency_ms, detail=detail
        )

    # -- implementation -----------------------------------------------------------

    def _run_probe(
        self, *, test_id: str, preset_client_config_json: str, probe_url: str, timeout_seconds: int
    ) -> tuple[int, int, str]:
        """Returns ``(TestOutcome, latency_ms, detail)``. Never raises --
        every failure mode this method knows about (bad input JSON, Docker
        unreachable, container refusing to start, proxy never coming up, the
        HTTP probe itself failing in any of the ways ``requests`` can fail)
        is caught here and turned into a real, specific outcome; only a
        genuinely unexpected internal error is left to propagate, and
        control_client.py has its own last-resort guard for that (mirroring
        how it already handles the nine other ``Command`` ops).
        """
        try:
            preset_outbound = json.loads(preset_client_config_json)
            if not isinstance(preset_outbound, dict) or "protocol" not in preset_outbound:
                raise ValueError(
                    "preset_client_config_json must be a JSON object with at least a "
                    "'protocol' key -- it is read as one Xray outbound (the preset/node "
                    "under test), not a full Xray config file, see connectivity_test.py "
                    "module docstring"
                )
        except (json.JSONDecodeError, ValueError) as err:
            # Never even reached Docker -- the ephemeral client, in the only
            # sense that matters to the caller, never started. No more
            # precise TestOutcome exists for "the input was unusable" than
            # this bucket; the detail says exactly why.
            return (
                pb2.TEST_OUTCOME_CONTAINER_START_FAILED,
                0,
                f"invalid preset_client_config_json, ephemeral test client was never started: {err}",
            )

        try:
            client = self._get_client()
        except DockerManagerError as err:
            return (pb2.TEST_OUTCOME_CONTAINER_START_FAILED, 0, str(err))

        config_dict = _build_ephemeral_config(preset_outbound)
        container_name = (
            f"{_CONTAINER_NAME_PREFIX}{_sanitize_for_container_name(test_id)}-{uuid.uuid4().hex[:8]}"
        )

        with tempfile.TemporaryDirectory(prefix="dist-vpn-agent-runtest-") as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            config_path.write_text(json.dumps(config_dict), encoding="utf-8")

            try:
                container = client.containers.run(
                    self._settings.xray_docker_image,
                    name=container_name,
                    command=["run", "-config", _CONTAINER_CONFIG_PATH],
                    volumes={str(config_path): {"bind": _CONTAINER_CONFIG_PATH, "mode": "ro"}},
                    ports={f"{_SOCKS_CONTAINER_PORT}/tcp": ("127.0.0.1", None)},
                    detach=True,
                    remove=True,  # AutoRemove -- belt-and-braces with the explicit remove() in finally below
                )
            except DockerException as err:
                return (
                    pb2.TEST_OUTCOME_CONTAINER_START_FAILED,
                    0,
                    f"failed to create/start ephemeral test container (image={self._settings.xray_docker_image}): {err}",
                )

            try:
                host_port, start_failure_detail = self._wait_for_container_port(container)
                if host_port is None:
                    return (pb2.TEST_OUTCOME_CONTAINER_START_FAILED, 0, start_failure_detail)

                ready, ready_detail = self._wait_for_socks_port_reachable(host_port)
                if not ready:
                    return (pb2.TEST_OUTCOME_PROXY_NO_RESPONSE, 0, ready_detail)

                return self._probe_through_socks(container, host_port, probe_url, timeout_seconds)
            finally:
                self._teardown(container)

    def _wait_for_container_port(self, container: Any) -> tuple[int | None, str]:
        """Poll (bounded by ``_CONTAINER_START_GRACE_SECONDS``) until the
        container is ``running`` *and* Docker has published a host port for
        its SOCKS inbound, or until it exits/disappears first. Returns
        ``(host_port, "")`` on success, ``(None, detail)`` otherwise.
        """
        deadline = time.monotonic() + _CONTAINER_START_GRACE_SECONDS
        last_status = "unknown"
        while time.monotonic() < deadline:
            try:
                container.reload()
            except NotFound:
                return (
                    None,
                    "ephemeral test container disappeared before its SOCKS port came up "
                    "(likely crashed immediately after start)",
                )
            except DockerException as err:
                return None, f"failed to inspect ephemeral test container while waiting for it to start: {err}"

            last_status = container.status
            if last_status == "running":
                ports = (container.attrs.get("NetworkSettings") or {}).get("Ports") or {}
                mapped = ports.get(f"{_SOCKS_CONTAINER_PORT}/tcp")
                if mapped:
                    return int(mapped[0]["HostPort"]), ""
            elif last_status in ("exited", "dead"):
                exit_code = (container.attrs.get("State") or {}).get("ExitCode")
                return (
                    None,
                    f"ephemeral test container exited (status={last_status}, exit_code={exit_code}) "
                    f"before its SOCKS port came up -- logs: {_tail_logs(container)}",
                )
            time.sleep(0.25)

        return (
            None,
            f"ephemeral test container did not reach 'running' with a published SOCKS port "
            f"within {_CONTAINER_START_GRACE_SECONDS:.0f}s (last status={last_status}) -- "
            f"logs: {_tail_logs(container)}",
        )

    @staticmethod
    def _wait_for_socks_port_reachable(host_port: int) -> tuple[bool, str]:
        """Docker reporting the container 'running' with a published port
        doesn't guarantee Xray's own SOCKS listener inside it has bound yet
        (the same "not immediately ready" gap tests/test_xray_client.py's
        real-Xray tier already documents for the long-lived container, via
        its own ``_wait_until_ready`` helper) -- poll a plain TCP connect
        rather than assuming.
        """
        deadline = time.monotonic() + _SOCKS_READY_GRACE_SECONDS
        last_err: OSError | None = None
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", host_port), timeout=1.0):
                    return True, ""
            except OSError as err:
                last_err = err
                time.sleep(0.2)
        return (
            False,
            f"local SOCKS proxy at 127.0.0.1:{host_port} never accepted a TCP connection "
            f"within {_SOCKS_READY_GRACE_SECONDS:.0f}s: {last_err}",
        )

    @staticmethod
    def _probe_through_socks(
        container: Any, host_port: int, probe_url: str, timeout_seconds: int
    ) -> tuple[int, int, str]:
        # socks5h (not socks5): resolve probe_url's hostname *through* the
        # tunnel, not locally -- TZ-4 SS3.4 is testing the whole channel,
        # this Agent's own local DNS resolver is not part of what's under
        # test.
        proxy_url = f"socks5h://127.0.0.1:{host_port}"
        proxies = {"http": proxy_url, "https": proxy_url}

        start = time.monotonic()
        try:
            response = requests.get(probe_url, proxies=proxies, timeout=timeout_seconds)
        except requests.exceptions.Timeout as err:
            latency_ms = int((time.monotonic() - start) * 1000)
            return (
                pb2.TEST_OUTCOME_TIMEOUT,
                latency_ms,
                f"probe to {probe_url} did not complete within {timeout_seconds}s: {err}",
            )
        except requests.exceptions.RequestException as err:
            # Includes requests.exceptions.SSLError -- deliberately NOT a
            # separate, automatically-precise branch. It's tempting to
            # assume SSLError always means "the real, end-to-end TLS between
            # this process and probe_url's origin failed" (a different,
            # client-side TLS leg from the tunnel's own outbound TLS/REALITY
            # handshake, but just as legitimately "TLS handshake failed").
            # Real testing during this slice's development (see the final
            # report and connectivity_test.py's tests) found that
            # assumption doesn't hold: when the SOCKS proxy accepts a CONNECT
            # optimistically and only *then* discovers its own outbound
            # target refuses the connection (e.g. a freedom outbound's own
            # ``redirect`` pointed at a dead address), Xray tears the tunnel
            # down while requests' own TLS handshake (for an https://
            # probe_url) happens to be in flight -- surfacing to this
            # process as requests.exceptions.SSLError(SSLEOFError(...)),
            # even though the root cause has nothing to do with TLS/
            # certificates at all. There is no reliable way from this
            # black-box vantage point to tell that case apart from a
            # genuine origin-TLS failure by exception type alone -- so
            # SSLError gets the exact same best-effort log-grep treatment as
            # every other connection failure here, not an automatic pass.
            latency_ms = int((time.monotonic() - start) * 1000)
            logs = _tail_logs(container)
            if _looks_like_tls_failure(logs):
                return (
                    pb2.TEST_OUTCOME_TLS_HANDSHAKE_FAILED,
                    latency_ms,
                    f"proxy connection failed and the ephemeral client's own logs indicate a "
                    f"TLS/REALITY handshake problem (best-effort log match, see "
                    f"connectivity_test.py module docstring): {err} -- logs: {logs}",
                )
            return (
                pb2.TEST_OUTCOME_PROXY_NO_RESPONSE,
                latency_ms,
                f"proxy did not return a usable response for {probe_url}: {err} -- logs: {logs}",
            )
        else:
            latency_ms = int((time.monotonic() - start) * 1000)
            return (
                pb2.TEST_OUTCOME_SUCCESS,
                latency_ms,
                f"HTTP {response.status_code} from {probe_url} in {latency_ms}ms",
            )

    @staticmethod
    def _teardown(container: Any) -> None:
        """Always tear down the ephemeral container, success or failure
        (module docstring point 4 / TZ-4 SS3.4) -- ``remove=True``
        (AutoRemove) at creation already handles the common case once the
        container stops, this is the explicit belt-and-braces path for
        whatever AutoRemove doesn't cover (e.g. the container never left
        ``created``, or a stop() races with AutoRemove's own cleanup --
        either way, a 404 here means it's already gone, which is success,
        not a failure to report).
        """
        with contextlib.suppress(DockerException):
            container.reload()
            if container.status == "running":
                container.stop(timeout=3)
        with contextlib.suppress(DockerException, NotFound):
            container.remove(force=True)
