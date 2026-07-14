"""Docker lifecycle management for the local Xray-core container (TZ-4 SS5.4).

Uses ``docker-py`` to talk to the Docker Engine API and make sure exactly
one thing is true: the Xray-core container this Agent manages (identified
by name, ``xray_container_name`` in config.py -- never any other container)
exists and is running. Three cases, handled idempotently by
:meth:`DockerManager.ensure_running`:

- missing entirely -> pull the image if needed and create+start a new
  container (mounting ``xray_config_host_path`` read-only at
  ``/etc/xray/config.json``, the path the container's entrypoint is run
  with, see :meth:`_create_container`);
- exists but stopped -> start it;
- exists and running -> no-op.

:meth:`is_running` / :meth:`health` are what feed the real
``Heartbeat.health.xray_container_running`` field in control_client.py
(previously hardcoded ``False`` -- see that module's changelog/commit
history for this slice).

--- Docker socket access decision for *this* slice (TZ-4 SS5.4) ---

TZ-4 SS5.4 explicitly leaves open whether the Agent process runs directly on
the host (with native Docker Engine API access) or itself inside a
container (needing scoped access to the host's Docker socket) --
"эта развилка -- предмет решения на этапе детальной реализации, с явным
протоколированием выбора и его обоснования". This slice's answer, made
explicit here rather than left implicit: the Agent runs directly on the
host (``docker.from_env()``, i.e. the default local Docker context/socket),
*not* inside a container itself. This is the simpler and safer of the two
options TZ-4 names -- it avoids mounting the host's Docker socket into a
container at all, which is the specific risk TZ-4 SS5.4 calls out ("тот
приём был приемлем для одноразового bash-оркестратора в прототипе, но для
production-агента предпочтительно" host-direct execution). If the Agent is
later containerized too, this decision needs revisiting (bind-mounting a
scoped Docker socket, or moving to the Docker Engine API over TCP with its
own auth) -- flagged here, not solved.

The Xray-core container's control-API port (``xray_api_address`` in
config.py) is published to the *host's* loopback interface only
(``127.0.0.1:<port>``, never ``0.0.0.0``), consistent with xray_client.py's
own "loopback-only, local trust boundary" reasoning.
"""

from __future__ import annotations

import dataclasses
import logging

import docker
from docker.errors import DockerException, ImageNotFound, NotFound

from dist_vpn_agent.config import AgentSettings

logger = logging.getLogger(__name__)

# Path inside the container the entrypoint is invoked with (see
# _create_container's command=). Only the host side of the bind mount is
# configurable (xray_config_host_path in config.py) -- the in-container path
# is a fixed implementation detail of how this Agent starts Xray-core.
_CONTAINER_CONFIG_PATH = "/etc/xray/config.json"


class DockerManagerError(RuntimeError):
    """Raised for any Docker Engine API failure (daemon unreachable, image
    pull failure, ...). Never swallowed -- see control_client.py/main.py for
    where this is caught, logged, and turned into an honest health signal
    rather than a silent hang (TZ-4 SS7).
    """


@dataclasses.dataclass(frozen=True)
class XrayContainerHealth:
    """Result of :meth:`DockerManager.health`. ``status`` is one of a
    container's real Docker statuses (``running``, ``exited``, ``created``,
    ...) or one of this module's own sentinels (``not_found``,
    ``docker_unavailable``) when there's no container to report a Docker
    status for at all.
    """

    running: bool
    status: str
    detail: str = ""


class DockerManager:
    """Owns Docker Engine API access for exactly one container: the local
    Xray-core instance this Agent manages (``settings.xray_container_name``).
    Never touches any other container.
    """

    def __init__(self, settings: AgentSettings, client: docker.DockerClient | None = None) -> None:
        self._settings = settings
        # Overridable for tests (inject a fake/mock docker.DockerClient);
        # None means "connect lazily to the real local Docker Engine API".
        self._client = client

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.from_env()
            except DockerException as err:
                raise DockerManagerError(f"cannot reach Docker Engine API: {err}") from err
        return self._client

    # -- health -----------------------------------------------------------------

    def health(self) -> XrayContainerHealth:
        """Never raises -- any failure to determine health *is* the health
        result (``running=False``), because "I can't tell if Xray is up" is
        itself a real, reportable node-health fact (TZ-4 SS3.5/SS7), not an
        error to propagate to callers like the heartbeat loop.
        """
        try:
            client = self._get_client()
        except DockerManagerError as err:
            return XrayContainerHealth(running=False, status="docker_unavailable", detail=str(err))

        try:
            container = client.containers.get(self._settings.xray_container_name)
        except NotFound:
            return XrayContainerHealth(running=False, status="not_found")
        except DockerException as err:
            return XrayContainerHealth(running=False, status="error", detail=str(err))

        container.reload()
        return XrayContainerHealth(running=container.status == "running", status=container.status)

    def is_running(self) -> bool:
        return self.health().running

    # -- lifecycle ----------------------------------------------------------------

    def ensure_running(self) -> XrayContainerHealth:
        """Idempotently make the managed Xray-core container exist and run.
        Raises :class:`DockerManagerError` on failure (Docker unreachable,
        image pull failure, container refuses to start, ...) -- unlike
        :meth:`health`, callers here *do* want to know when the thing they
        just asked for didn't happen.
        """
        client = self._get_client()
        name = self._settings.xray_container_name

        try:
            container = client.containers.get(name)
        except NotFound:
            logger.info(
                "Xray-core container %s not found, creating it (image=%s)",
                name,
                self._settings.xray_docker_image,
            )
            container = self._create_container(client)
        except DockerException as err:
            raise DockerManagerError(f"failed to look up container {name}: {err}") from err

        try:
            container.reload()
            if container.status != "running":
                logger.info(
                    "starting Xray-core container %s (was: %s)", name, container.status
                )
                container.start()
                container.reload()
            else:
                logger.debug("Xray-core container %s already running", name)
        except DockerException as err:
            raise DockerManagerError(f"failed to start container {name}: {err}") from err

        return XrayContainerHealth(running=container.status == "running", status=container.status)

    def _create_container(self, client: docker.DockerClient):
        image = self._settings.xray_docker_image
        try:
            client.images.get(image)
        except ImageNotFound:
            logger.info("pulling Xray-core image %s (not present locally)", image)
            try:
                client.images.pull(image)
            except DockerException as err:
                raise DockerManagerError(f"failed to pull image {image}: {err}") from err
        except DockerException as err:
            raise DockerManagerError(f"failed to inspect image {image}: {err}") from err

        host, _, port = self._settings.xray_api_address.rpartition(":")
        host = host or "127.0.0.1"

        try:
            return client.containers.create(
                image,
                name=self._settings.xray_container_name,
                command=["run", "-config", _CONTAINER_CONFIG_PATH],
                volumes={
                    str(self._settings.xray_config_host_path): {
                        "bind": _CONTAINER_CONFIG_PATH,
                        "mode": "ro",
                    }
                },
                # Loopback-only publish, matching xray_client.py's "local
                # trust boundary" reasoning -- see module docstring.
                ports={f"{port}/tcp": (host, int(port))},
                restart_policy={"Name": "unless-stopped"},
                detach=True,
            )
        except DockerException as err:
            raise DockerManagerError(f"failed to create container from image {image}: {err}") from err
