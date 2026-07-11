# dist-vpn Agent
#
# This slice packages only the control-plane gRPC client (Agent<->Platform
# dial-out channel). Xray/Docker management lands in a follow-up slice and
# will extend this image (e.g. mounting/using a Docker socket per TZ-4 SS5.4 --
# that decision is deliberately not made yet).
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
# vars at them -- see agent/README.md.

FROM python:3.12-slim AS builder

WORKDIR /build

COPY agent/pyproject.toml ./agent/pyproject.toml
COPY agent/README.md ./agent/README.md
COPY agent/src ./agent/src
COPY contracts/proto/agent_control.proto /tmp/contracts_proto/agent_control.proto

WORKDIR /build/agent

# Regenerate gRPC stubs from the contract at build time so the image never
# depends on committed generated code (nothing generated is tracked in git).
RUN pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir grpcio-tools \
 && mkdir -p src/dist_vpn_agent/generated \
 && python -m grpc_tools.protoc \
      --proto_path=/tmp/contracts_proto \
      --python_out=src/dist_vpn_agent/generated \
      --grpc_python_out=src/dist_vpn_agent/generated \
      agent_control.proto \
 && touch src/dist_vpn_agent/generated/__init__.py \
 && sed -i -E 's/^import ([A-Za-z_]+_pb2) as (.*)$/from . import \1 as \2/' \
      src/dist_vpn_agent/generated/agent_control_pb2_grpc.py \
 && pip wheel --no-cache-dir --wheel-dir /build/wheels .

FROM python:3.12-slim

# Non-root user for the Agent process (TZ-4 SS5.3: private keys/certs must
# not be readable by root inside the container).
RUN groupadd --system agent && useradd --system --gid agent --create-home agent

WORKDIR /app

COPY --from=builder /build/wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

USER agent

ENV AGENT_LOG_LEVEL=INFO

ENTRYPOINT ["dist-vpn-agent"]
