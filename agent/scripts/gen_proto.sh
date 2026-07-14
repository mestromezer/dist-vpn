#!/usr/bin/env bash
# Regenerate Python gRPC stubs for:
#   1. the Agent<->Platform control-plane contract
#      (../../contracts/proto/agent_control.proto), and
#   2. the vendored real Xray-core control protos (vendor/xray-proto/, see
#      that directory's README.md for exactly which files and why).
#
# Do not edit generated output by hand. Do not edit agent_control.proto from
# here -- see contracts/README.md for the contract-change policy. Do not
# hand-edit vendor/xray-proto/**/*.proto either -- see that directory's
# README.md for how to refresh it from upstream.
#
# Output goes to src/dist_vpn_agent/generated/, which is covered by the repo's
# root .gitignore (**/generated/, **/*_pb2.py, **/*_pb2_grpc.py) -- nothing under
# it is committed. Run this script after cloning and after any change to
# either .proto source.
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
XRAY_PROTO_DIR="$(cd "${AGENT_DIR}/vendor/xray-proto" && pwd)"
OUT_DIR="${AGENT_DIR}/src/dist_vpn_agent/generated"
XRAY_OUT_DIR="${OUT_DIR}/xray"

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

# --- Xray-core control protos (vendor/xray-proto/) -------------------------
#
# Unlike agent_control.proto (a single file sitting directly at its
# proto_path root), this is a multi-file tree that mirrors Xray-core's own
# directory layout (app/proxyman/command/command.proto, core/config.proto,
# etc. -- see vendor/xray-proto/README.md), because those files import each
# other by relative path (e.g. `import "core/config.proto"`). protoc's
# Python plugin emits imports rooted at that proto_path for *every* file in
# the tree, not just the top one, so the single sed line above isn't enough
# here -- _fix_xray_proto_imports.py generalizes the same fix (package-
# relative imports, computed per file from its depth under the output root)
# across the whole generated tree, and also adds the __init__.py files
# protoc itself does not create for nested output directories.

mkdir -p "${XRAY_OUT_DIR}"

mapfile -t XRAY_PROTO_FILES < <(cd "${XRAY_PROTO_DIR}" && find . -name '*.proto' -printf '%P\n' | sort)

if [ "${#XRAY_PROTO_FILES[@]}" -eq 0 ]; then
  echo "error: no .proto files found under ${XRAY_PROTO_DIR}" >&2
  exit 1
fi

"${PYTHON_BIN}" -m grpc_tools.protoc \
  --proto_path="${XRAY_PROTO_DIR}" \
  --python_out="${XRAY_OUT_DIR}" \
  --grpc_python_out="${XRAY_OUT_DIR}" \
  --pyi_out="${XRAY_OUT_DIR}" \
  "${XRAY_PROTO_FILES[@]}"

"${PYTHON_BIN}" "${SCRIPT_DIR}/_fix_xray_proto_imports.py" "${XRAY_OUT_DIR}"

echo "Generated Xray-core gRPC stubs in ${XRAY_OUT_DIR}"
