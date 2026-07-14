# Build context is the repository root (see platform/docker-compose.yml
# `build.context: ..`) so this Dockerfile can see both contracts/ and platform/ -
# required to generate the gRPC stubs from contracts/proto/agent_control.proto at
# image-build time (generated code is gitignored, never committed - see .gitignore).
FROM python:3.12-slim

WORKDIR /app

COPY contracts/proto /app/contracts/proto
COPY platform/pyproject.toml /app/platform/pyproject.toml
COPY platform/scripts /app/platform/scripts
COPY platform/src /app/platform/src
COPY platform/alembic.ini /app/platform/alembic.ini
COPY platform/alembic /app/platform/alembic
COPY platform/docker-entrypoint.sh /app/platform/docker-entrypoint.sh

WORKDIR /app/platform

# grpcio-tools first (needed to run protoc for gen_proto.py) - stubs must exist
# *before* `pip install .` below, since that's a non-editable install that copies
# src/ into site-packages at that moment (see scripts/gen_proto.py docstring).
RUN pip install --no-cache-dir --upgrade pip grpcio-tools \
    && python scripts/gen_proto.py \
    && pip install --no-cache-dir . \
    && chmod +x docker-entrypoint.sh

ENV PLATFORM_HTTP_HOST=0.0.0.0 \
    PLATFORM_HTTP_PORT=8000 \
    PLATFORM_GRPC_HOST=0.0.0.0 \
    PLATFORM_GRPC_PORT=50051

EXPOSE 8000 50051

ENTRYPOINT ["./docker-entrypoint.sh"]
