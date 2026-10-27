#!/usr/bin/env python3
"""套用「可切換常駐連線」固件補丁（v2.5.3 版）。

用法（repo 根目錄）：
    python3 patch/apply_persist_ws.py

設計：
- CloseAudioChannel()      —— 自動待機路徑；persist 模式只保留連線，不 reset
- CloseAudioChannelForce() —— 用戶主動退出（單擊 ToggleChat）；一律關閉
- OpenAudioChannel()       —— 連線仍在就直接重用，不重送 hello
- application.cc           —— 處理 {"type":"config","persist_ws":bool}，寫入 NVS
- protocol.cc              —— persist 時 IsTimeout() 回 false（否則待機刷 log 導致重啟）
"""

from __future__ import annotations

import pathlib
import sys

PROTO_H = pathlib.Path("main/protocols/protocol.h")
PROTO_CC = pathlib.Path("main/protocols/protocol.cc")
WS_H = pathlib.Path("main/protocols/websocket_protocol.h")
WS_CC = pathlib.Path("main/protocols/websocket_protocol.cc")
APP = pathlib.Path("main/application.cc")

INCLUDE_OLD = '#include <esp_log.h>\n\n#define TAG "Protocol"'
INCLUDE_NEW = '#include <esp_log.h>\n#include <esp_timer.h>\n#include "settings.h"\n\n#define TAG "Protocol"'

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
    "void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {\n"
    "    (void)send_goodbye;  // Websocket doesn't need to send goodbye message\n"
    "    websocket_.reset();\n"
    "}"
)

CLOSE_NEW = (
    "void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {\n"
    "    (void)send_goodbye;  // Websocket doesn't need to send goodbye message\n"
    '    Settings settings("websocket", false);\n'
    '    if (settings.GetBool("persist_ws", false)) {\n'
    '        ESP_LOGI(TAG, "persist_ws on: keep websocket alive");\n'
    "        return;\n"
    "    }\n"
    "    websocket_.reset();\n"
    "}\n"
    "\n"
    "void WebsocketProtocol::CloseAudioChannelForce(bool send_goodbye) {\n"
    "    (void)send_goodbye;\n"
    '    ESP_LOGI(TAG, "force close audio channel");\n'
    "    websocket_.reset();\n"
    "}"
)

OPEN_OLD = (
    "bool WebsocketProtocol::OpenAudioChannel() {\n"
    '    Settings settings("websocket", false);'
)

OPEN_NEW = (
    "bool WebsocketProtocol::OpenAudioChannel() {\n"
    "    // persist 模式下 WebSocket 常駐，已連線就直接重用（不重送 hello）\n"
    "    if (websocket_ != nullptr && websocket_->IsConnected()) {\n"
    "        error_occurred_ = false;\n"
    "        return true;\n"
    "    }\n"
    '    Settings settings("websocket", false);'
)

TOGGLE_OLD = (
    "    } else if (state == kDeviceStateListening) {\n"
    "        protocol_->CloseAudioChannel();\n"
    "    }"
)
TOGGLE_NEW = (
    "    } else if (state == kDeviceStateListening) {\n"
    "        protocol_->CloseAudioChannel();\n"
    "        // persist 模式下通道不會真的關閉，必須顯式回到 Idle 才能退出收音\n"
    "        if (protocol_->IsAudioChannelOpened()) {\n"
    "            SetDeviceState(kDeviceStateIdle);\n"
    "        }\n"
    "    }"
)

APP_ANCHOR = (
    "        } else {\n"
    '            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);\n'
    "        }"
)

APP_BRANCH = (
    '        } else if (strcmp(type->valuestring, "config") == 0) {\n'
    '            auto persist = cJSON_GetObjectItem(root, "persist_ws");\n'
    "            if (cJSON_IsBool(persist)) {\n"
    "                bool persist_on = cJSON_IsTrue(persist);\n"
    '                Settings settings("websocket", true);\n'
    '                settings.SetBool("persist_ws", persist_on);\n'
    '                ESP_LOGI(TAG, "config: persist_ws=%d", persist_on ? 1 : 0);\n'
    "                if (!persist_on) {\n"
    "                    Schedule([this]() { protocol_->CloseAudioChannelForce(); });\n"
    "                }\n"
    "            }\n"
    "        } else {\n"
    '            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);\n'
    "        }"
)

INIT_OLD = (
    "void Application::Initialize() {\n"
    "    auto& board = Board::GetInstance();"
)
INIT_NEW = (
    "void Application::Initialize() {\n"
    "    // 開機預設回到 normal 模式：persist_ws 不跨重啟保留\n"
    "    {\n"
    '        Settings settings("websocket", true);\n'
    '        settings.SetBool("persist_ws", false);\n'
    "    }\n"
    "    auto& board = Board::GetInstance();"
)

PREV_OLD = (
    "void Application::HandleStateChangedEvent() {\n"
    "    DeviceState new_state = state_machine_.GetState();"
)
PREV_NEW = (
    "void Application::HandleStateChangedEvent() {\n"
    "    static DeviceState s_prev_state = kDeviceStateUnknown;\n"
    "    DeviceState prev_state = s_prev_state;\n"
    "    DeviceState new_state = state_machine_.GetState();\n"
    "    s_prev_state = new_state;"
)

IDLE_OLD = (
    "            audio_service_.EnableVoiceProcessing(false);\n"
    "            audio_service_.EnableWakeWordDetection(true);\n"
    "            break;\n"
    "        case kDeviceStateConnecting:"
)

IDLE_NEW = (
    "            audio_service_.EnableVoiceProcessing(false);\n"
    "            audio_service_.EnableWakeWordDetection(true);\n"
    "            // 非 persist 模式：只在「回覆播完（Speaking -> Idle）」才斷線，\n"
    "            // 避免鬆手瞬間（Listening -> Idle）就切斷、收不到最後音頻。\n"
    "            if (prev_state == kDeviceStateSpeaking && !persist_enabled()) {\n"
    "                Schedule([this]() {\n"
    "                    if (protocol_ && protocol_->IsAudioChannelOpened()) {\n"
    "                        protocol_->CloseAudioChannel();\n"
    "                    }\n"
    "                });\n"
    "            }\n"
    "            break;\n"
    "        case kDeviceStateConnecting:"
)

PERSIST_HELPER_OLD = (
    "void Application::Initialize() {\n"
    "    // 開機預設回到 normal 模式：persist_ws 不跨重啟保留\n"
    "    {\n"
    '        Settings settings("websocket", true);\n'
    '        settings.SetBool("persist_ws", false);\n'
    "    }\n"
    "    auto& board = Board::GetInstance();"
)

PERSIST_HELPER_NEW = (
    "static bool persist_enabled() {\n"
    '    Settings settings("websocket", false);\n'
    '    return settings.GetBool("persist_ws", false);\n'
    "}\n"
    "\n"
    "void Application::Initialize() {\n"
    "    // 開機預設回到 normal 模式：persist_ws 不跨重啟保留\n"
    "    {\n"
    '        Settings settings("websocket", true);\n'
    '        settings.SetBool("persist_ws", false);\n'
    "    }\n"
    "    auto& board = Board::GetInstance();"
)

TIMEOUT_OLD = (
    "bool Protocol::IsTimeout() const {\n"
    "    const int kTimeoutSeconds = 120;"
)

TIMEOUT_NEW = (
    "bool Protocol::IsTimeout() const {\n"
    "    // persist 模式：連線常駐，不判超時（否則待機會每 200ms 刷錯誤，導致重啟）\n"
    "    static bool persist_cached = false;\n"
    "    static int64_t persist_checked_ms = 0;\n"
    "    int64_t persist_now_ms = esp_timer_get_time() / 1000;\n"
    "    if (persist_now_ms - persist_checked_ms > 3000) {\n"
    '        Settings settings("websocket", false);\n'
    '        persist_cached = settings.GetBool("persist_ws", false);\n'
    "        persist_checked_ms = persist_now_ms;\n"
    "    }\n"
    "    if (persist_cached) {\n"
    "        return false;\n"
    "    }\n"
    "    const int kTimeoutSeconds = 120;"
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
    for p in (PROTO_H, PROTO_CC, WS_H, WS_CC, APP):
        if not p.exists():
            print(f"missing {p}", file=sys.stderr)
            return 1

    ok = True
    ok &= patch_text(PROTO_H, PROTO_ANCHOR, PROTO_ADD, "CloseAudioChannelForce", "protocol.h")
    ok &= patch_text(WS_H, WS_H_ANCHOR, WS_H_ADD, "CloseAudioChannelForce", "websocket_protocol.h")
    ok &= patch_text(WS_CC, CLOSE_OLD, CLOSE_NEW, "force close audio channel", "websocket_protocol.cc/close")
    ok &= patch_text(WS_CC, OPEN_OLD, OPEN_NEW, "已連線就直接重用", "websocket_protocol.cc/open")
    # 短按退出收音：persist 下通道關不掉，需顯式回 Idle
    ok &= patch_text(APP, TOGGLE_OLD, TOGGLE_NEW, "必須顯式回到 Idle", "application.cc/toggle")
    ok &= patch_text(APP, APP_ANCHOR, APP_BRANCH, "persist_ws", "application.cc/config")
    ok &= patch_text(APP, INIT_OLD, INIT_NEW, "不跨重啟保留", "application.cc/boot-reset")
    ok &= patch_text(APP, PERSIST_HELPER_OLD, PERSIST_HELPER_NEW, "static bool persist_enabled", "application.cc/helper")
    ok &= patch_text(APP, PREV_OLD, PREV_NEW, "s_prev_state", "application.cc/prev-state")
    ok &= patch_text(APP, IDLE_OLD, IDLE_NEW, "回覆播完", "application.cc/idle-close")
    ok &= patch_text(PROTO_CC, INCLUDE_OLD, INCLUDE_NEW, "esp_timer.h", "protocol.cc/includes")
    ok &= patch_text(PROTO_CC, TIMEOUT_OLD, TIMEOUT_NEW, "persist 模式：連線常駐", "protocol.cc/timeout")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
