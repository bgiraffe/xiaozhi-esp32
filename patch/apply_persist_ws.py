#!/usr/bin/env python3
"""套用「可切換常駐連線」固件補丁（v2，安全版）。

用法（在 repo 根目錄、src/ 已 checkout upstream 之後）：
    python3 patch/apply_persist_ws.py

設計（v2）：
- `CloseAudioChannel()`      —— 自動待機路徑；persist 模式只保留連線，不 reset
- `CloseAudioChannelForce()` —— 用戶主動退出（單擊 ToggleChat）；一律關閉
- `OpenAudioChannel()`       —— 連線仍在就直接重用，不重送 hello
- `application.cc`           —— 處理 {"type":"config","persist_ws":bool}，寫入 NVS

v1 的錯誤：把 CloseAudioChannel 全域 no-op，導致用戶按單擊也退不出收音。
"""

from __future__ import annotations

import pathlib
import sys

PROTO_H = pathlib.Path("src/main/protocols/protocol.h")
WS_H = pathlib.Path("src/main/protocols/websocket_protocol.h")
WS_CC = pathlib.Path("src/main/protocols/websocket_protocol.cc")
APP = pathlib.Path("src/main/application.cc")

PROTO_ANCHOR = "    virtual void CloseAudioChannel(bool send_goodbye = true) = 0;"
PROTO_ADD = (
    PROTO_ANCHOR
    + "\n"
    + "    // 用戶主動退出（單擊）時使用：一定關閉連線，不受 persist 模式影響\n"
    + "    virtual void CloseAudioChannelForce(bool send_goodbye = true) {\n"
    + "        CloseAudioChannel(send_goodbye);\n"
    + "    }"
)

WS_H_ANCHOR = "    void CloseAudioChannel(bool send_goodbye = true) override;"
WS_H_ADD = WS_H_ANCHOR + "\n    void CloseAudioChannelForce(bool send_goodbye = true) override;"

CLOSE_OLD = (
    'void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {\n'
    "    (void)send_goodbye;  // Websocket doesn't need to send goodbye message\n"
    '    websocket_.reset();\n'
    '}'
)

CLOSE_NEW = (
    'void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {\n'
    "    (void)send_goodbye;  // Websocket doesn't need to send goodbye message\n"
    '    Settings settings("websocket", false);\n'
    '    if (settings.GetBool("persist_ws", false)) {\n'
    '        ESP_LOGI(TAG, "persist_ws on: keep websocket alive");\n'
    '        return;\n'
    '    }\n'
    '    websocket_.reset();\n'
    '}\n'
    '\n'
    'void WebsocketProtocol::CloseAudioChannelForce(bool send_goodbye) {\n'
    '    (void)send_goodbye;\n'
    '    ESP_LOGI(TAG, "force close audio channel");\n'
    '    websocket_.reset();\n'
    '}'
)

OPEN_OLD = (
    'bool WebsocketProtocol::OpenAudioChannel() {\n'
    '    Settings settings("websocket", false);'
)

OPEN_NEW = (
    'bool WebsocketProtocol::OpenAudioChannel() {\n'
    '    // persist 模式下 WebSocket 常駐，已連線就直接重用（不重送 hello）\n'
    '    if (websocket_ != nullptr && websocket_->IsConnected()) {\n'
    '        error_occurred_ = false;\n'
    '        return true;\n'
    '    }\n'
    '    Settings settings("websocket", false);'
)

# ExecuteToggleChat 的「Listening -> 關閉」分支改走 Force
TOGGLE_OLD = (
    '    } else if (state == kDeviceStateListening) {\n'
    '        protocol_->CloseAudioChannel();\n'
    '    }'
)
TOGGLE_NEW = (
    '    } else if (state == kDeviceStateListening) {\n'
    '        protocol_->CloseAudioChannelForce();\n'
    '    }'
)

APP_ANCHOR = (
    '        } else {\n'
    '            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);\n'
    '        }'
)

APP_BRANCH = (
    '        } else if (strcmp(type->valuestring, "config") == 0) {\n'
    '            auto persist = cJSON_GetObjectItem(root, "persist_ws");\n'
    '            if (cJSON_IsBool(persist)) {\n'
    '                bool persist_on = cJSON_IsTrue(persist);\n'
    '                Settings settings("websocket", true);\n'
    '                settings.SetBool("persist_ws", persist_on);\n'
    '                ESP_LOGI(TAG, "config: persist_ws=%d", persist_on ? 1 : 0);\n'
    '                if (!persist_on) {\n'
    '                    Schedule([this]() { protocol_->CloseAudioChannelForce(); });\n'
    '                }\n'
    '            }\n'
    '        } else {\n'
    '            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);\n'
    '        }'
)


def patch_text(path: pathlib.Path, anchor: str, replacement: str, marker: str, label: str) -> bool:
    text = path.read_text(encoding="utf-8")
    if marker in text:
        print(f"{label}: already patched")
        return True
    if anchor not in text:
        print(f"{label}: ANCHOR NOT FOUND", file=sys.stderr)
        return False
    path.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")
    print(f"{label}: patched")
    return True


def main() -> int:
    for p in (PROTO_H, WS_H, WS_CC, APP):
        if not p.exists():
            print(f"missing {p}（src/ 尚未 checkout？）", file=sys.stderr)
            return 1

    ok = True
    ok &= patch_text(PROTO_H, PROTO_ANCHOR, PROTO_ADD, "CloseAudioChannelForce", "protocol.h")
    ok &= patch_text(WS_H, WS_H_ANCHOR, WS_H_ADD, "CloseAudioChannelForce", "websocket_protocol.h")
    ok &= patch_text(WS_CC, CLOSE_OLD, CLOSE_NEW, "force close audio channel", "websocket_protocol.cc (close)")
    ok &= patch_text(WS_CC, OPEN_OLD, OPEN_NEW, "已連線就直接重用", "websocket_protocol.cc (open)")
    ok &= patch_text(APP, TOGGLE_OLD, TOGGLE_NEW, "CloseAudioChannelForce();", "application.cc (toggle)")
    ok &= patch_text(APP, APP_ANCHOR, APP_BRANCH, "persist_ws", "application.cc (config)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
