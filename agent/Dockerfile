# dist-vpn Agent
#
# Packages the control-plane gRPC client (Agent<->Platform dial-out channel)
# together with real local Xray-core management (xray_client.py,
# docker_manager.py -- TZ-4 SS3.2/SS5.4). The Agent process itself still runs
# directly on the host's Docker context (docker.from_env(), see
# docker_manager.py's module docstring for why) -- this image is the Agent
# process, not something that additionally wraps/replaces the Docker socket
# decision made there.
#
# IMPORTANT: build context is the *repository root*, not agent/, because this
# build needs read access to contracts/proto/agent_control.proto (the Agent
# never vendors/commits generated stubs -- see contracts/README.md and
# agent/scripts/gen_proto.sh). Docker cannot COPY from outside its context,
# so a subdirectory-only context won't work here.
#
# Build (run from the repository root):
#   docker build -f agent/Dockerfile -t dist-vpn-agent:dev .
#
# Run (dev, insecure channel -- never for production, see TZ-4 SS5.2):
#   docker run --rm \
#     -e AGENT_PLATFORM_ADDRESS=platform.example:50051 \
#     -e AGENT_ENROLLMENT_TOKEN=... \
#     -e AGENT_INSECURE_DEV_MODE=true \
#     dist-vpn-agent:dev
#
# Run (production, mTLS): mount cert/key/CA and point the AGENT_MTLS_*_PATH
# vars at them -- see agent/README.md. Also needs access to the local Docker
# Engine API (docker_manager.py) and to the local Xray-core control API
# (xray_client.py, xray_api_address) -- both loopback/local-trust-boundary
# concerns documented in those modules, not solved by this Dockerfile.

FROM python:3.12-slim AS builder

WORKDIR /build

# Mirror the real repo layout under /build (agent/ next to contracts/) rather
# than the ad hoc /tmp copy the control-plane-only slice used -- gen_proto.sh
# resolves contracts/proto/ relative to its own location
# (agent/scripts/../../contracts/proto), so this layout is what lets the same
# script that's used (and tested) locally run unmodified at build time
# instead of re-implementing/duplicating its protoc invocations here.
COPY agent/pyproject.toml ./agent/pyproject.toml
COPY agent/README.md ./agent/README.md
COPY agent/src ./agent/src
COPY agent/scripts ./agent/scripts
COPY agent/vendor ./agent/vendor
COPY contracts/proto/agent_control.proto ./contracts/proto/agent_control.proto

WORKDIR /build/agent

# Regenerate gRPC stubs (both agent_control.proto and the vendored
# vendor/xray-proto/ tree) at build time so the image never depends on
# committed generated code (nothing generated is tracked in git -- see
# scripts/gen_proto.sh and vendor/xray-proto/README.md).
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir grpcio-tools \
 && bash scripts/gen_proto.sh \
 && pip wheel --no-cache-dir --wheel-dir /build/wheels .

FROM python:3.12-slim

# Non-root user for the Agent process (TZ-4 SS5.3: private keys/certs must
# not be readable by root inside the container).
RUN groupadd --system agent && useradd --system --gid agent --create-home agent

WORKDIR /app

COPY --from=builder /build/wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

# AgentSettings.state_dir (identity.py, see agent/README.md's "mTLS-
# идентичность" section) defaults to /var/lib/dist-vpn-agent -- a real
# writable directory the *host* running this Agent needs, not something
# provisioned by pip. /var/lib is root-owned by default; without this, the
# first successful bootstrap enrollment would fail to persist its issued
# identity to disk (PermissionError) even though enrollment itself
# succeeded server-side -- found and fixed in the same slice that
# introduced AGENT_STATE_DIR, not left as a landmine for later.
RUN mkdir -p /var/lib/dist-vpn-agent && chown agent:agent /var/lib/dist-vpn-agent

USER agent

ENV AGENT_LOG_LEVEL=INFO

ENTRYPOINT ["dist-vpn-agent"]
