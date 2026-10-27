#!/usr/bin/env python3
"""套用「可切換常駐連線」固件補丁。

用法（在 repo 根目錄、src/ 已 checkout upstream 之後）：
    python3 patch/apply_persist_ws.py

補丁內容：
1. application.cc：處理伺服器下發的 {"type":"config","persist_ws":bool}
   → 寫入 NVS（Settings "websocket"/"persist_ws"）；關閉時立刻收掉連線。
2. websocket_protocol.cc：
   - CloseAudioChannel() 在 persist 模式只停音頻，不 reset WebSocket
   - OpenAudioChannel() 若連線仍在，直接重用（不重送 hello）
"""

from __future__ import annotations

import pathlib
import sys

APP = pathlib.Path("src/main/application.cc")
WS = pathlib.Path("src/main/protocols/websocket_protocol.cc")

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
    '                    Schedule([this]() { protocol_->CloseAudioChannel(); });\n'
    '                }\n'
    '            }\n'
    '        } else {\n'
    '            ESP_LOGW(TAG, "Unknown message type: %s", type->valuestring);\n'
    '        }'
)

CLOSE_OLD = (
    'void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {\n'
    '    (void)send_goodbye;  // Websocket doesn\'t need to send goodbye message\n'
    '    websocket_.reset();\n'
    '}'
)

CLOSE_NEW = (
    'void WebsocketProtocol::CloseAudioChannel(bool send_goodbye) {\n'
    '    (void)send_goodbye;  // Websocket doesn\'t need to send goodbye message\n'
    '    Settings settings("websocket", false);\n'
    '    if (settings.GetBool("persist_ws", false)) {\n'
    '        ESP_LOGI(TAG, "persist_ws on: keep websocket alive");\n'
    '        return;\n'
    '    }\n'
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


def patch_app() -> bool:
    text = APP.read_text(encoding="utf-8")
    if "persist_ws" in text:
        print("application.cc: already patched")
        return True
    if APP_ANCHOR not in text:
        print("application.cc: ANCHOR NOT FOUND", file=sys.stderr)
        return False
    APP.write_text(text.replace(APP_ANCHOR, APP_BRANCH, 1), encoding="utf-8")
    print("application.cc: patched")
    return True


def patch_ws() -> bool:
    text = WS.read_text(encoding="utf-8")
    ok = True

    if "persist_ws on: keep websocket alive" in text:
        print("websocket_protocol.cc: CloseAudioChannel already patched")
    elif CLOSE_OLD in text:
        text = text.replace(CLOSE_OLD, CLOSE_NEW, 1)
        print("websocket_protocol.cc: CloseAudioChannel patched")
    else:
        print("websocket_protocol.cc: CloseAudioChannel ANCHOR NOT FOUND", file=sys.stderr)
        ok = False

    if "已連線就直接重用" in text:
        print("websocket_protocol.cc: OpenAudioChannel already patched")
    elif OPEN_OLD in text:
        text = text.replace(OPEN_OLD, OPEN_NEW, 1)
        print("websocket_protocol.cc: OpenAudioChannel patched")
    else:
        print("websocket_protocol.cc: OpenAudioChannel ANCHOR NOT FOUND", file=sys.stderr)
        ok = False

    WS.write_text(text, encoding="utf-8")
    return ok


def main() -> int:
    if not APP.exists() or not WS.exists():
        print("src/ 尚未 checkout？找不到 application.cc 或 websocket_protocol.cc", file=sys.stderr)
        return 1
    ok = patch_app() and patch_ws()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
