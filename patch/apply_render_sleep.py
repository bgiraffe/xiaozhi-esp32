#!/usr/bin/env python3
"""熄屏時停下大臉動畫渲染。

`EmoteDisplay::SetPowerSaveMode()` 原本是空殼（只印 log）。改成：
- on  = true  → emote_set_anim_visible(handle, false)，隱藏大臉 → 播放器停止繪製
- on  = false → emote_set_anim_visible(handle, true)，恢復

配合板子在 SleepScreen/WakeScreen 呼叫 Display::SetPowerSaveMode()。
"""

from __future__ import annotations

import pathlib
import sys

TARGET = pathlib.Path("main/display/emote_display.cc")

OLD = (
    "void EmoteDisplay::SetPowerSaveMode(bool on) {\n"
    '    ESP_LOGI(TAG, "SetPowerSaveMode: %s", on ? "ON" : "OFF");\n'
    "    if (!emote_handle_) {\n"
    "        return;\n"
    "    }\n"
    "}"
)

NEW = (
    "void EmoteDisplay::SetPowerSaveMode(bool on) {\n"
    '    ESP_LOGI(TAG, "SetPowerSaveMode: %s", on ? "ON" : "OFF");\n'
    "    if (!emote_handle_) {\n"
    "        return;\n"
    "    }\n"
    "    // 熄屏時隱藏大臉動畫：播放器不再產生新幀（省 CPU/SPI）\n"
    "    emote_set_anim_visible(emote_handle_, !on);\n"
    "}"
)


def main() -> int:
    if not TARGET.exists():
        print(f"missing {TARGET}", file=sys.stderr)
        return 1
    text = TARGET.read_text(encoding="utf-8")
    if "emote_set_anim_visible(emote_handle_" in text:
        print("already patched")
        return 0
    if "void EmoteDisplay::SetPowerSaveMode(bool on)" not in text:
        print("SetPowerSaveMode anchor not found", file=sys.stderr)
        return 1
    text = text.replace(OLD, NEW, 1)
    TARGET.write_text(text, encoding="utf-8")
    print("emote_display.cc patched")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
