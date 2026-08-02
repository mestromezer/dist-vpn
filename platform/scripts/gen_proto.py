#!/usr/bin/env python
"""Regenerate Python gRPC stubs from contracts/proto/agent_control.proto AND
contracts/proto/platform_peering.proto (Platform<->Platform peering - Agent never
sees or needs this one, see platform_peering.proto's own header comment for why it's
a separate contract; agent/scripts/gen_proto.sh intentionally only compiles
agent_control.proto).

Output goes to src/dist_vpn_platform/generated/ - gitignored (see repo .gitignore
`**/generated/`, `**/*_pb2*.py`) and regenerated as a normal part of `pip install -e
.[dev]` / CI setup, per contracts/README.md step 1 ("Если меняется agent_control.proto
- сначала обновить эту схему, затем регенерировать стабы в agent/ и platform/").

Usage (from anywhere):
    python platform/scripts/gen_proto.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

PROTO_FILES = ["agent_control.proto", "platform_peering.proto"]


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "contracts" / "proto" / "agent_control.proto").is_file():
            return candidate
    raise SystemExit("Could not locate contracts/proto/agent_control.proto above " + str(start))


def _fix_relative_import(out_dir: Path, module_stem: str) -> None:
    # grpc_tools generates `import <stem>_pb2 as ..._pb2` (a top-level absolute
    # import) inside <stem>_pb2_grpc.py, which breaks once this file lives inside the
    # dist_vpn_platform.generated package. Rewrite it to a relative import.
    grpc_file = out_dir / f"{module_stem}_pb2_grpc.py"
    if not grpc_file.is_file():
        return  # platform_peering.proto has no service-less companion here, but guard anyway
    text = grpc_file.read_text(encoding="utf-8")
    mangled = module_stem.replace("_", "__")
    text = text.replace(f"import {module_stem}_pb2 as {mangled}__pb2", f"from . import {module_stem}_pb2 as {mangled}__pb2")
    grpc_file.write_text(text, encoding="utf-8")


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    repo_root = find_repo_root(script_dir)
    proto_dir = repo_root / "contracts" / "proto"

    platform_dir = script_dir.parent
    out_dir = platform_dir / "src" / "dist_vpn_platform" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)

    for proto_name in PROTO_FILES:
        proto_file = proto_dir / proto_name
        cmd = [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{proto_dir}",
            f"--python_out={out_dir}",
            f"--grpc_python_out={out_dir}",
            f"--pyi_out={out_dir}",
            str(proto_file),
        ]
        print("Running:", " ".join(cmd))
        subprocess.run(cmd, check=True)
        _fix_relative_import(out_dir, proto_file.stem)

    init_file = out_dir / "__init__.py"
    init_file.write_text(
        '"""Generated from contracts/proto/*.proto by scripts/gen_proto.py. '
        'Do not edit by hand - do not commit (see .gitignore)."""\n',
        encoding="utf-8",
    )

    print(f"Generated stubs in {out_dir}")


if __name__ == "__main__":
    main()
