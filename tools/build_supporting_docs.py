#!/usr/bin/env python3
"""Build the single supporting-documentation file for submission.

Reads docs/ERROR_CODES.md, docs/ARCHITECTURE.md and docs/RELEASE_NOTES.md and writes
docs/SUPPORTING_DOCUMENTATION.md with the three parts in the order the assignment lists them:
error codes, architecture and design, release notes (product description, usage, known
issues, coming soon).

The three source files stay the place to edit. Run this after changing any of them:

    python tools/build_supporting_docs.py

Standard library only.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
OUT = DOCS / "SUPPORTING_DOCUMENTATION.md"

# (source file, heading in the combined file)
PARTS = [
    ("ERROR_CODES.md", "1. Error codes"),
    ("ARCHITECTURE.md", "2. Architecture and design"),
    ("RELEASE_NOTES.md", "3. Release notes"),
]
# Links between the source files become in-page links in the combined file.
LINKS = {
    "(ERROR_CODES.md)": "(#1-error-codes)",
    "(ARCHITECTURE.md)": "(#2-architecture-and-design)",
    "(RELEASE_NOTES.md)": "(#3-release-notes)",
}
HEADER = """# Supporting documentation

Vehicle-location (VL) data product. This file holds the three supporting documents the
exercise asks for: the list of error codes, the architecture and design, and the release
notes. It is built from `docs/ERROR_CODES.md`, `docs/ARCHITECTURE.md` and
`docs/RELEASE_NOTES.md` by `tools/build_supporting_docs.py`. Edit those files, not this one.

"""


def demote(body: str) -> str:
    """Drop the file's own title and push every heading one level down, skipping code fences."""
    out: list[str] = []
    in_fence = False
    title_dropped = False
    for line in body.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence and re.match(r"^#{1,5} ", line):
            if line.startswith("# ") and not title_dropped:
                title_dropped = True
                continue
            line = "#" + line
        out.append(line)
    return "\n".join(out).strip("\n")


def build() -> str:
    chunks = [HEADER]
    for name, heading in PARTS:
        body = (DOCS / name).read_text(encoding="utf-8").replace("\r\n", "\n")
        for old, new in LINKS.items():
            body = body.replace(old, new)
        chunks.append(f"## {heading}\n\n{demote(body)}\n\n")
    return "".join(chunks).rstrip("\n") + "\n"


def main() -> int:
    OUT.write_text(build(), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
