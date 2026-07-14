#!/usr/bin/env python
"""Regenerate Python gRPC stubs from contracts/proto/agent_control.proto.

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


def find_repo_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "contracts" / "proto" / "agent_control.proto").is_file():
            return candidate
    raise SystemExit("Could not locate contracts/proto/agent_control.proto above " + str(start))


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    repo_root = find_repo_root(script_dir)
    proto_dir = repo_root / "contracts" / "proto"
    proto_file = proto_dir / "agent_control.proto"

    platform_dir = script_dir.parent
    out_dir = platform_dir / "src" / "dist_vpn_platform" / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)

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

    init_file = out_dir / "__init__.py"
    init_file.write_text(
        '"""Generated from contracts/proto/agent_control.proto by scripts/gen_proto.py. '
        'Do not edit by hand - do not commit (see .gitignore)."""\n',
        encoding="utf-8",
    )

    # grpc_tools generates `import agent_control_pb2 as agent__control__pb2` (a
    # top-level absolute import) inside agent_control_pb2_grpc.py, which breaks once
    # this file lives inside the dist_vpn_platform.generated package. Rewrite it to a
    # relative import.
    grpc_file = out_dir / "agent_control_pb2_grpc.py"
    text = grpc_file.read_text(encoding="utf-8")
    text = text.replace(
        "import agent_control_pb2 as agent__control__pb2",
        "from . import agent_control_pb2 as agent__control__pb2",
    )
    grpc_file.write_text(text, encoding="utf-8")

    print(f"Generated stubs in {out_dir}")


if __name__ == "__main__":
    main()
