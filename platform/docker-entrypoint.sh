#!/bin/sh
# TZ-5 §8: "Единая команда развёртывания (docker compose up), миграции применяются
# автоматически при старте (Alembic)" - migrations, then the (idempotent) default
# admin seed, then the actual server process, all under one `docker compose up`.
set -e

echo "Running Alembic migrations..."
alembic upgrade head

echo "Seeding default admin account (no-op if it already exists)..."
python -m dist_vpn_platform.seed

echo "Starting Platform (HTTP :${PLATFORM_HTTP_PORT:-8000} + gRPC :${PLATFORM_GRPC_PORT:-50051} + gRPC enrollment :${PLATFORM_GRPC_ENROLLMENT_PORT:-50052})..."
exec python -m dist_vpn_platform.main
