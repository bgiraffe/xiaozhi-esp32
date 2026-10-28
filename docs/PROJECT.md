# 小智 AI handheld — 專案總覽

> 立創·實戰派 ESP32-S3（`lckfb/szpi-esp32s3`）+ 自建 `xiaozhi-esp32-server` + BytePlus（LLM / ASR / TTS / VLM）。
> 本文件記錄所有改動、參數、決策與日常運維。

---

## 1. 系統架構

```
┌──────────────┐   OTA (HTTP)    ┌────────────────────────────┐
│  ESP32-S3    │ ───────────────▶│  iot.bgiraffe.cc:18003     │
│  固件 v2.5.3 │                 │  /xiaozhi/ota/  /audio/*.ogg│
│  (自編)      │◀── ws url ──────│                            │
│              │                 │  iot.bgiraffe.cc:18000     │
│  顯示/按鍵   │◀═══ WebSocket ══▶│  /xiaozhi/v1/ (Opus 16k)   │
│  相機/麥克風 │   + notify 播放  │  xiaozhi-esp32-server      │
└──────────────┘                 │  (Docker, /opt/xiaozhi)    │
                                 └────────┬───────────────────┘
                                          │ HTTPS
                          ┌───────────────┴───────────────┐
                          │ BytePlus (ap-southeast-1)     │
                          │  ASR  seedasr.sauc.duration   │
                          │  LLM  dola-seed-2-1-turbo     │
                          │  TTS  seed-tts-2.0            │
                          │  VLM  同 LLM（支援讀圖）      │
                          └───────────────────────────────┘
```

- 域名：`iot.bgiraffe.cc` → Cloudflare **DNS only（灰雲）** A record → `34.92.245.126`
- 家用 DNS 由 AdGuard 提供，需為 `iot.bgiraffe.cc` 加精確 rewrite（優先於 `*.bgiraffe.cc`）
- 伺服器：GCP `e2-medium`（2 vCPU / 4 GB），Debian 12

---

## 2. 倉庫與建置

**直接編譯 fork 本身**（fork 內含 upstream 源碼 + 我們的板級檔），不再 overlay 到舊 tag。

```
.github/workflows/build-screen-off.yml   手動觸發的建置流程
patch/apply_camera_tool.py               相機工具移出 HAVE_LVGL 守衛
patch/apply_persist_ws.py                常駐連線（persist_ws）補丁
patch/apply_render_sleep.py              熄屏時停大臉渲染
main/boards/lckfb/szpi-esp32s3/          板級檔案（含我們的 .cc/config.h/config.json）
docs/PROJECT.md                          本文件
```

建置步驟：

```
1. checkout fork
2. 改 config.json：CONFIG_USE_EMOTE_MESSAGE_STYLE=y、CONFIG_OTA_URL
3. 改 config.h：AUDIO_INPUT/OUTPUT_SAMPLE_RATE → 16000
4. patch/apply_camera_tool.py
5. patch/apply_persist_ws.py
6. patch/apply_render_sleep.py
7. python scripts/build.py lckfb/szpi-esp32s3 --name lichuang-dev
```

### 伺服器端也有自己的 git repo

`/opt/xiaozhi` 已 `git init`（本地，無 remote），追蹤：
`docker-compose.yml`、`patches/*`、`say.sh`、`config.sample.yaml`（去敏）、`.gitignore`。
`data/` 與 `.say_token` 不入版控。

> **歷史教訓**：曾用 `v2.4.2` tag 編譯，但 v2.4.2 **沒有 `notify`**（沒有 `notify_player.cc`），
> 導致「伺服器主動播放」永遠不可能成功。現已改用 fork 的 `main`（v2.5.3）。

---

## 3. 固件改動

### 3.1 板級檔案 `lichuang_dev_board.cc`

| 功能 | 行為 |
|---|---|
| 自動熄屏 | 閒置 30 秒：背光 0 → ST7789 **DISPOFF** → 停大臉渲染（三層） |
| 喚醒 | 短按：**DISPON** → 恢復渲染 → 背光回到先前亮度 |
| 短按（黑屏） | 只亮屏 |
| 短按（亮屏） | `ToggleChatState()`（開／關對話；persist 下不會斷線） |
| 長按 400ms | `StartListening()`（PTT），鬆手 `StopListening()`；不改螢幕狀態 |
| 雙擊 | 已移除（原本切 AEC，會吞掉長按） |
| 音頻 | 輸入/輸出均 16 kHz（必須相同，否則 `CreateDuplexChannels` assert） |

熄屏三層（實測 log）：

```
Backlight: Set brightness to 0
Panel DISPOFF -> ESP_OK
EmoteDisplay: SetPowerSaveMode: ON     ← 隱藏大臉，播放器停產幀
```

> **注意 1**：熄屏判斷**不能**用 `app.CanEnterSleepMode()`。persist 模式下通道常開，
> 該函式永遠回 false，會導致螢幕永不熄滅。現在只看 `state != kDeviceStateIdle`。
>
> **注意 2**：`EmoteDisplay::SetPowerSaveMode()` 原本是空殼，現已 patch 成
> `emote_set_anim_visible(handle, !on)`，否則大臉會一直以 30fps 渲染（白白燒 CPU）。

### 3.2 建置層補丁

- `CONFIG_USE_EMOTE_MESSAGE_STYLE=y`（大臉 UI）
- `mcp_server.cc`：`self.camera.take_photo` 移出 `#ifdef HAVE_LVGL`（emote 模式下 `HAVE_LVGL` 不再定義）
- `persist_ws`（見第 9 節）

燒錄位置（16 MB）：

```
0x0        bootloader
0x8000     partition table
0xd000     ota_data
0x20000    xiaozhi.bin
0x800000   expression_assets.bin
```

只改代碼時燒 `0x20000` 即可（保留 NVS 的 WiFi 與設定）。

---

## 4. 伺服器改動（`/opt/xiaozhi`）

### 4.1 docker-compose.yml

```yaml
environment:
  - TZ=Asia/Hong_Kong
  - OPUS_BITRATE=16000      # 對話 opus 位元率
  - SAY_API_TOKEN=<random>  # /api/* 的 token（另存 .say_token）
  - WS_KEEPALIVE=0          # 應用層 keepalive，實測會令裝置斷線，保持關閉
ports:
  - "18000:8000"
  - "18003:8003"
volumes:
  - ./data:/opt/xiaozhi-esp32-server/data
  - ./patches/vllm_openai.py:.../core/providers/vllm/openai.py:ro
  - ./patches/agent-base-prompt.txt:.../agent-base-prompt.txt:ro
  - ./patches/opus_encoder_utils.py:.../core/utils/opus_encoder_utils.py:ro
  - ./patches/openai.py:.../core/providers/llm/openai/openai.py:ro
  - ./patches/byteplus_tts_stream.py:.../core/providers/tts/byteplus_tts_stream.py:ro
  - ./patches/doubao_stream.py:.../core/providers/asr/doubao_stream.py:ro
  - ./patches/byteplus.py:.../core/providers/tts/byteplus.py:ro
  - ./patches/connection.py:.../core/connection.py:ro
  - ./patches/http_server.py:.../core/http_server.py:ro
```

### 4.2 patches 一覽

| 檔案 | 改了什麼 |
|---|---|
| `doubao_stream.py` | BytePlus ASR：x-api-key、`language` 無條件送出（async 也帶 `yue-CN`） |
| `openai.py` | `THINKING_DISABLED_DOMAINS` 加 `bytepluses.com`（關思考鏈） |
| `byteplus.py` | 自寫 TTS provider（NDJSON）；`requests` 丟到 `asyncio.to_thread` |
| `byteplus_tts_stream.py` | 火山 v3 雙向串流（暫停用，與工具直出路徑不相容） |
| `vllm_openai.py` | 視覺 prompt 加「口語化、不要編號、2–3 句」 |
| `agent-base-prompt.txt` | 6.5 KB → 1.1 KB 精簡模板 |
| `opus_encoder_utils.py` | bitrate 改讀 `OPUS_BITRATE` |
| `connection.py` | `ACTIVE_CONNECTIONS` 註冊表 + `_keepalive_loop`（預設關閉） |
| `http_server.py` | `/api/devices`、`/api/say`（notify）、`/api/mode`、`/audio/{name}`；`_is_conn_alive()` 驗證 WS 真的 OPEN（避免殘留連線被當成在線） |

### 4.3 `.config.yaml` 關鍵設定

```yaml
server:
  websocket: ws://iot.bgiraffe.cc:18000/xiaozhi/v1/
  vision_explain: http://iot.bgiraffe.cc:18003/mcp/vision/explain
close_connection_no_voice_time: 86400
tts_audio_send_delay: 0
xiaozhi.audio_params: {format: opus, sample_rate: 16000, channels: 1, frame_duration: 60}
selected_module:
  ASR: BytePlusASR      # doubao_stream, end_window_size 500, language yue-CN
  LLM: BytePlusLLM      # dola-seed-2-1-turbo, thinking disabled
  VLLM: BytePlusLLM     # 視覺（同一個接入點，支援讀圖）
  TTS: BytePlusTTS      # HTTP 版, 16k mp3, zh_female_liuchangnv
  Intent: function_call
TTS.BytePlusTTS.sample_rate: 16000
```

---

## 5. 效能現況

| 指標 | 數值 |
|---|---|
| 對話延遲（講完→出聲） | ~4 秒（ASR ~1s + LLM ~2s + TTS ~0.5s） |
| LLM 首字（單獨測） | 1.4–1.8 s |
| TTS 首塊 | 0.39 s |
| 拍照問答 | ~13 秒（裝置拍照上傳 ~7s + VLM ~6s） |
| 主動推送（notify） | 請求→裝置開始播 ≈ 1–2 秒 |
| 容器記憶體 | ~180 MB（4 GB 機器，swap 幾乎不用） |
| 裝置 free sram | ~111 KB（穩定；熄屏時短暫升到 ~116 KB） |

### 待機耗電（估算，未用電流計實測）

| 狀態 | 估計電流 | 2000 mAh 可撐 |
|---|---|---|
| normal 待機（斷線 + 三層熄屏） | 25–40 mA | **50–80 小時（2–3 天）** |
| persist 待機（保持連線 + 三層熄屏） | 40–55 mA | **36–50 小時（1.5–2 天）** |
| 說話／播放中 | 100–150 mA | — |

熄屏三層的貢獻：背光 5–15 mA、面板 DISPOFF 2–5 mA、停渲染 10–20 mA。

已排除的瓶頸：思考鏈（22s→1.3s）、prompt 過長、工具定義、採樣率、位元率。

---

## 6. 主動推送（`/api/say`）

**架構：notify + audio_url**（不是直接推 TTS 音頻）。

```
POST /api/say  → TTS 合成 mp3 → ffmpeg 轉 OGG/Opus(16k)
              → 存 data/audio/<uuid>.ogg
              → WS 送 {"type":"notify","audio_url":"http://.../audio/x.ogg"}
              → 裝置（NotifyPlayer）自己下載並播放
```

```bash
GET  /api/devices        → {"ok":true,"devices":["10:51:db:80:ea:f4"]}
POST /api/say            → {"device_id":"...","text":"..."}
POST /api/mode           → {"mode":"persist"|"normal"}
GET  /audio/<name>.ogg   → audio/ogg
```

命令列：`bash /opt/xiaozhi/say.sh "要說的話" [device_id]`

### 為什麼不用「直接推 TTS 音頻」

實測在 persist 模式（通道常開但裝置 Idle）下，直接送 `tts start` + 音頻會令**裝置立刻斷線**——
固件狀態機假設「播放 TTS 必須在收音會話中」。`notify` 是 v2.5.3 原生的伺服器主動播放路徑，
要求裝置 Idle，正好符合 persist 模式的狀態。

### 限制

- 裝置必須在線（persist 模式下可以長時間保持）。
- 音檔會在 `data/audio/` 累積；推送時會清掉超過 1 小時的舊檔。
- 位元率 16 kbps（省流量）；提高到 32k 實測約 27.6k，聽感提升有限（源頭是 16 kHz）。

---

## 7. 重要決策記錄

1. **LLM 關思考鏈**：一句「你好」原本燒 1388 reasoning tokens、22 秒。
2. **16 kHz**：輸入輸出必須同時改，否則 `BoxAudioCodec::CreateDuplexChannels` assert 重啟。
3. **ASR 用 `bigmodel_async` + `language`**：保住串流，同時識別粵語（`enable_multilingual` 會退化成 nostream）。
4. **回覆用普通話、識別用粵語**：TTS 音色是普通話，prompt 有明確規則。
5. **相機工具移出 HAVE_LVGL**：開 emote UI 後 `HAVE_LVGL` 不再定義，整段會被編譯掉。
6. **用 fork main（v2.5.3）而非 v2.4.2**：v2.4.2 沒有 notify。
7. **notify 而非 TTS 推送**：避開固件狀態機限制。
8. **關閉伺服器 keepalive**：`{"type":"keepalive"}` 會令裝置斷線（實測 61 秒）。

---

## 8. 運維 Runbook

```bash
cd /opt/xiaozhi
docker compose up -d --force-recreate     # 改 patches 後必須 recreate（bind mount inode）
docker logs xiaozhi-esp32-server --since 5m 2>&1 | grep -Ei '识别到文本|SentenceType|ERROR|notify'
bash /opt/xiaozhi/say.sh "測試"
```

燒錄（只改代碼）：

```powershell
py -3.12 -m esptool --chip esp32s3 -p COM5 -b 921600 write-flash 0x20000 "xiaozhi.bin"
```

| 症狀 | 原因 | 處理 |
|---|---|---|
| 裝置無限重啟 | 輸入/輸出採樣率不一致 | 兩者都改 16k |
| 相機說「看不到」 | `VLLM` 段缺對應 provider 鍵 | 補 `VLLM.BytePlusLLM` |
| ASR 亂碼 | 沒送 `language` | patch `doubao_stream.py` |
| 推送無聲 | 固件不支援 notify | 必須 v2.5.3+ |
| 短按退不出收音 | persist 下通道關不掉 | 已修：顯式 `SetDeviceState(Idle)` |
| 螢幕不熄 | 用了 `CanEnterSleepMode()` | 已修：只看 `state != Idle` |
| 換 IP 後裝置離線 | 固件寫死 OTA URL | 用域名 + 改 DNS |

---

## 9. 常駐連線（persist_ws）

目標：退出收音後仍保持 WebSocket，讓 `/api/say` 隨時可用。

切換：`POST /api/mode {"mode":"persist"}` / `{"mode":"normal"}` → 伺服器送
`{"type":"config","persist_ws":bool}` → 裝置存 NVS 並即時套用。

**開機一律回到 normal**：`Application::Initialize()` 會把 `persist_ws` 寫成 false，
所以重開機後預設是 normal（短按關閉對話會斷線），需要常駐時再由伺服器開啟。

固件改動（`patch/apply_persist_ws.py`）：

| 改動 | 作用 |
|---|---|
| `CloseAudioChannel()` persist 時不 reset | 待機保留連線 |
| 新增 `CloseAudioChannelForce()` | 只在「關閉 persist」時用 |
| `OpenAudioChannel()` 重用已連線 | 不重送 hello |
| ToggleChat 退出收音時顯式回 Idle | persist 下通道關不掉，靠這行退出 |
| `IsTimeout()` persist 時回 false | 否則待機每 200ms 刷 `Channel timeout`，約 5 分鐘重啟 |
| `Application::Initialize()` 清 `persist_ws` | 開機固定回 normal |

已知限制：

- 裝置**開機後不會自動連線**，要第一次收音才會建立連線（之後靠 persist 保持）。
- 重新啟動伺服器容器會切斷連線，裝置需再收音一次。
- WiFi 省電會降到 `LOW_POWER`，長連線穩定性仍在觀察。

---

## 10. 待辦

- [x] 自動熄屏 / 長按 PTT / 大臉 UI
- [x] 粵語 ASR + 普通話回覆
- [x] 相機問答
- [x] 主動推送（notify）
- [x] 常駐連線 + 伺服器切換
- [ ] 拍照加速（裝置端 ~7s）
- [ ] 天氣功能（需自備 QWeather key）
- [ ] 串流 TTS 修好後再啟用
- [ ] 定時提醒示範（cron + say.sh）
- [ ] 長時間（>1 小時）連線穩定性觀察
