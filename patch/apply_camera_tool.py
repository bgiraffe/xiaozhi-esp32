#!/usr/bin/env python3
"""把相機工具移出 HAVE_LVGL 守衛（emote UI 會讓 HAVE_LVGL 不再定義）。

用法（repo 根目錄）：
    python3 patch/apply_camera_tool.py
"""

from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path("main/mcp_server.cc")

CAMERA_START = "    auto camera = board.GetCamera();\n"
CAMERA_START_NEW = (
    "    // camera tool moved out of HAVE_LVGL guard (emote UI disables HAVE_LVGL)\n"
    "#endif\n"
    "    auto camera = board.GetCamera();\n"
)

GUARD_TAIL_OLD = "    }\n#endif\n\n    // Restore the original tools list"
GUARD_TAIL_NEW = "    }\n\n    // Restore the original tools list"


def main() -> int:
    if not TARGET.exists():
        print(f"missing {TARGET}", file=sys.stderr)
        return 1

    text = TARGET.read_text(encoding="utf-8")
    if "camera tool moved out of HAVE_LVGL guard" in text:
        print("already patched")
        return 0

    if CAMERA_START not in text:
        print("camera start anchor not found", file=sys.stderr)
        return 1
    if GUARD_TAIL_OLD not in text:
        print("guard tail anchor not found", file=sys.stderr)
        return 1

    text = text.replace(CAMERA_START, CAMERA_START_NEW, 1)
    text = text.replace(GUARD_TAIL_OLD, GUARD_TAIL_NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print("camera tool patch applied")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
