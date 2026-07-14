#!/usr/bin/env python
"""Rewrite the absolute, proto_path-rooted imports that protoc's Python/pyi
plugins emit for a *nested* proto tree into package-relative imports, and
drop ``__init__.py`` markers so the generated tree is importable as a
sub-package.

Background (see gen_proto.sh and README.md "Регенерация gRPC-стабов" for the
single-file case this generalizes): protoc's Python codegen always emits
imports rooted at whatever directory was passed as --proto_path, e.g. a file
compiled from ``app/proxyman/command/command.proto`` gets::

    from common.protocol import user_pb2 as common_dot_protocol_dot_user__pb2

That resolves fine only if ``common`` is a top-level, sys.path-visible
package -- never true once the output is nested inside
``dist_vpn_agent.generated.xray`` instead of sitting on sys.path directly.
This is the exact same protoc limitation (grpc/grpc#9575) the existing
sed one-liner in gen_proto.sh works around for the single flat
agent_control_pb2_grpc.py file; here the tree is multi-file and multi-level,
so a plain sed substitution isn't enough and this script does the equivalent
rewrite generically for every file, based on each file's own depth under the
output root.

Usage: python _fix_xray_proto_imports.py <output_dir>
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Matches the "from <dotted.pkg.path> import <name>_pb2 as <alias>" lines
# protoc emits for cross-file references within the compiled tree. Anything
# under google.protobuf.* is a real installed package and must be left
# alone.
_IMPORT_RE = re.compile(
    r'^from (?!google\.protobuf)([A-Za-z_][\w.]*) import (\w+_pb2) as (\w+)$'
)


def _dots_for_depth(rel_dir: Path) -> str:
    """Number of leading dots to reach the output root from a module whose
    own package is at ``rel_dir`` (relative to the output root).

    A module living in a package that is *n* directories below the root
    needs ``n + 1`` dots to climb back out to the root package (1 dot is
    "my own containing package", each further dot climbs one more level) --
    see the walkthrough in this file's docstring / the code-review notes in
    gen_proto.sh.
    """
    depth = 0 if str(rel_dir) in ("", ".") else len(rel_dir.parts)
    return "." * (depth + 1)


def fix_file(path: Path, output_root: Path) -> None:
    rel_dir = path.parent.relative_to(output_root)
    dots = _dots_for_depth(rel_dir)

    text = path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    changed = False
    for i, line in enumerate(lines):
        m = _IMPORT_RE.match(line.rstrip("\n"))
        if not m:
            continue
        pkg, mod, alias = m.groups()
        newline = f"from {dots}{pkg} import {mod} as {alias}\n"
        if newline != line:
            lines[i] = newline
            changed = True
    if changed:
        path.write_text("".join(lines), encoding="utf-8")


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: _fix_xray_proto_imports.py <output_dir>", file=sys.stderr)
        raise SystemExit(2)

    output_root = Path(sys.argv[1]).resolve()
    if not output_root.is_dir():
        print(f"error: {output_root} is not a directory", file=sys.stderr)
        raise SystemExit(1)

    # Every directory in the generated tree (including the root) needs an
    # __init__.py -- protoc's Python plugin does not create these, it only
    # writes the *_pb2.py/_pb2_grpc.py/*.pyi files.
    for d in [output_root, *(p for p in output_root.rglob("*") if p.is_dir())]:
        init_file = d / "__init__.py"
        if not init_file.exists():
            init_file.write_text("", encoding="utf-8")

    for py_file in output_root.rglob("*.py"):
        if py_file.name == "__init__.py":
            continue
        fix_file(py_file, output_root)
    for pyi_file in output_root.rglob("*.pyi"):
        fix_file(pyi_file, output_root)

    print(f"Rewrote imports under {output_root}")


if __name__ == "__main__":
    main()
