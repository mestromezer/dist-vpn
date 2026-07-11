#!/usr/bin/env bash
# Regenerate Python gRPC stubs for the Agent<->Platform control-plane contract.
#
# Source of truth: ../../contracts/proto/agent_control.proto (do not edit generated
# output by hand; do not edit the .proto from here -- see contracts/README.md for
# the contract-change policy).
#
# Output goes to src/dist_vpn_agent/generated/, which is covered by the repo's
# root .gitignore (**/generated/, **/*_pb2.py, **/*_pb2_grpc.py) -- nothing under
# it is committed. Run this script after cloning and after any change to the
# .proto file.
#
# Usage (from anywhere):
#   bash agent/scripts/gen_proto.sh
# or, from agent/:
#   bash scripts/gen_proto.sh
#
# Requires the project venv to be active (or grpcio-tools importable by
# whichever `python` is on PATH).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
PROTO_DIR="$(cd "${AGENT_DIR}/../contracts/proto" && pwd)"
OUT_DIR="${AGENT_DIR}/src/dist_vpn_agent/generated"

PROTO_FILE="agent_control.proto"

if [ ! -f "${PROTO_DIR}/${PROTO_FILE}" ]; then
  echo "error: ${PROTO_DIR}/${PROTO_FILE} not found" >&2
  exit 1
fi

PYTHON_BIN="${PYTHON_BIN:-python}"

mkdir -p "${OUT_DIR}"

"${PYTHON_BIN}" -m grpc_tools.protoc \
  --proto_path="${PROTO_DIR}" \
  --python_out="${OUT_DIR}" \
  --grpc_python_out="${OUT_DIR}" \
  --pyi_out="${OUT_DIR}" \
  "${PROTO_FILE}"

# Make the output a proper Python package so it can be imported as
# `dist_vpn_agent.generated.agent_control_pb2` etc. This file is regenerated
# every run (the whole directory is gitignored) rather than tracked in git.
: > "${OUT_DIR}/__init__.py"

# protoc's Python plugin always emits a flat, top-level import in the generated
# *_grpc.py file (e.g. `import agent_control_pb2 as agent_control__pb2`),
# regardless of where the output lands. That breaks once the file is nested
# inside a package (dist_vpn_agent.generated) instead of sitting on sys.path
# directly. Patch it to a package-relative import. This is a well-known
# grpc_tools limitation (see grpc/grpc#9575); tools like `protoletariat` exist
# solely to automate this same fix.
GRPC_FILE="${OUT_DIR}/${PROTO_FILE%.proto}_pb2_grpc.py"
if [ -f "${GRPC_FILE}" ]; then
  sed -i -E 's/^import ([A-Za-z_]+_pb2) as (.*)$/from . import \1 as \2/' "${GRPC_FILE}"
fi

echo "Generated gRPC stubs in ${OUT_DIR}"
