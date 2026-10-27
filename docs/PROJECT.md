# 小智 AI  handheld — 專案總覽

> 立創·實戰派 ESP32-S3（`lckfb/szpi-esp32s3`）+ 自建 `xiaozhi-esp32-server` + BytePlus（LLM / ASR / TTS）。
> 本文件記錄所有改動、參數、決策與日常運維，供日後回溯。

---

## 1. 系統架構

```
┌──────────────┐   OTA (HTTP)    ┌────────────────────────────┐
│  ESP32-S3    │ ───────────────▶│  iot.bgiraffe.cc:18003     │
│  固件 2.4.2  │                 │  /xiaozhi/ota/             │
│  (自編)      │◀── ws url ──────│                            │
│              │                 │  iot.bgiraffe.cc:18000     │
│  顯示/按鍵   │◀═══ WebSocket ══▶│  /xiaozhi/v1/ (Opus 16k)   │
│  相機/麥克風 │                 │  xiaozhi-esp32-server      │
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

- 對外域名：`iot.bgiraffe.cc` → Cloudflare **DNS only（灰雲）** A record → `34.92.245.126`
- 家用 DNS 由 AdGuard 提供，需為 `iot.bgiraffe.cc` 加精確 rewrite（優先於 `*.bgiraffe.cc`）
- 伺服器：GCP `e2-medium`（2 vCPU / 4 GB），Debian 12

---

## 2. 倉庫結構（本 repo）

```
.github/workflows/build-screen-off.yml   固件 CI（手動觸發）
patch/lichuang_dev_board.cc              唯一改動的板級檔案
docs/PROJECT.md                          本文件
README.md                                早期使用說明
```

固件建置流程（GitHub Actions）：

```
1. checkout fork（取 patch/ 與 workflow）
2. checkout 78/xiaozhi-esp32@v2.4.2 到 src/
3. 用 fork 的 .cc 覆蓋 src 的同名檔
4. 改 config.json：CONFIG_USE_EMOTE_MESSAGE_STYLE=y、CONFIG_OTA_URL
5. sed config.h：輸入/輸出採樣率 → 16000
6. patch mcp_server.cc：把相機工具移出 HAVE_LVGL 守衛
7. scripts/build.py → merged-binary.bin / xiaozhi.bin / flash_args
```

---

## 3. 固件改動

檔案：`main/boards/lckfb/szpi-esp32s3/lichuang_dev_board.cc`

| 功能 | 行為 |
|---|---|
| 自動熄屏 | 閒置 30 秒關背光；`SetBrightness(0)` 不寫 NVS |
| 短按 | 黑屏 → 只亮屏；亮屏 → `ToggleChatState()` |
| 長按 400ms | 開始收音、鬆手送出；**全程不改屏幕狀態** |
| 雙擊 | 已移除（原本切 AEC，會吞掉長按） |
| 觸摸 | 測試通過（FT5x06），現已關閉 |
| 音頻 | 輸入/輸出均 16 kHz（必須相同，否則 `CreateDuplexChannels` assert） |

建置層另外的兩個 patch（在 workflow 內）：

- `CONFIG_USE_EMOTE_MESSAGE_STYLE=y` → 大臉 UI（`emote::EmoteDisplay`）
- `mcp_server.cc`：`self.camera.take_photo` 移出 `#ifdef HAVE_LVGL`（emote 模式下 `HAVE_LVGL` 不再定義）

燒錄位置（16 MB flash）：

```
0x0        bootloader
0x8000     partition table
0xd000     ota_data
0x20000    xiaozhi.bin        ← 只改代碼時燒這裡
0x800000   expression_assets.bin  ← 改 UI 資源才需要
```

---

## 4. 伺服器改動（`/opt/xiaozhi`）

### 4.1 docker-compose.yml

```yaml
environment:
  - TZ=Asia/Hong_Kong
  - OPUS_BITRATE=16000
ports:
  - "18000:8000"    # WebSocket
  - "18003:8003"    # OTA / vision
volumes:
  - ./data:/opt/xiaozhi-esp32-server/data
  - ./patches/vllm_openai.py:.../core/providers/vllm/openai.py:ro
  - ./patches/agent-base-prompt.txt:.../agent-base-prompt.txt:ro
  - ./patches/opus_encoder_utils.py:.../core/utils/opus_encoder_utils.py:ro
  - ./patches/openai.py:.../core/providers/llm/openai/openai.py:ro
  - ./patches/byteplus_tts_stream.py:.../core/providers/tts/byteplus_tts_stream.py:ro
  - ./patches/doubao_stream.py:.../core/providers/asr/doubao_stream.py:ro
  - ./patches/byteplus.py:.../core/providers/tts/byteplus.py:ro
```

### 4.2 patches 一覽

| 檔案 | 改了什麼 | 為什麼 |
|---|---|---|
| `doubao_stream.py` (ASR) | ws 指向 `voice.ap-southeast-1.bytepluses.com`；認證改 `x-api-key`；`language` 無條件送出 | BytePlus 用 x-api-key；async 端點也接受 language |
| `openai.py` (LLM) | `THINKING_DISABLED_DOMAINS` 加入 `bytepluses.com` | 關思考鏈：22s → 1.3s |
| `byteplus.py` (TTS HTTP) | 新寫的 provider，NDJSON + base64 音頻；`requests` 丟到 `asyncio.to_thread` | 不阻塞事件循環 |
| `byteplus_tts_stream.py` (TTS 串流) | 火山 v3 雙向 provider 改 x-api-key + BytePlus ws | 實驗用；與「工具直出」不相容，暫時停用 |
| `vllm_openai.py` | prompt 加「口語化、不要編號、2-3 句」 | 視覺描述原本會念出「1. 2. 3.」 |
| `agent-base-prompt.txt` | 6.5 KB → 1.1 KB 精簡模板 | 首字延遲（prompt 6225 → 800 字） |
| `opus_encoder_utils.py` | bitrate 改讀 `OPUS_BITRATE` 環境變數 | 24k → 16kbps |
| `connection.py` | 新增 `ACTIVE_CONNECTIONS` 註冊表（device_id → handler），連線建立時註冊、關閉時註銷 | 讓外部 API 能找到線上裝置 |
| `http_server.py` | 新增 `GET /api/devices`、`POST /api/say` 與 `_push_say` 協程 | 主動推送 |

### 4.3 `.config.yaml` 關鍵設定

```yaml
server:
  port: 8000
  http_port: 8003
  websocket: ws://iot.bgiraffe.cc:18000/xiaozhi/v1/
  vision_explain: http://iot.bgiraffe.cc:18003/mcp/vision/explain

tts_audio_send_delay: 0
xiaozhi:
  audio_params: {format: opus, sample_rate: 16000, channels: 1, frame_duration: 60}

selected_module:
  VAD: SileroVAD
  ASR: BytePlusASR
  LLM: BytePlusLLM
  VLLM: BytePlusLLM
  TTS: BytePlusTTS          # HTTP 版；串流版 BytePlusTTSStream 暫停用
  Intent: function_call
  Memory: nomem

ASR.BytePlusASR:
  type: doubao_stream
  api_key: <redacted>
  resource_id: volc.seedasr.sauc.duration
  end_window_size: 500
  enable_multilingual: false
  language: yue-CN          # 粵語識別，同時保串流

LLM.BytePlusLLM / VLLM.BytePlusLLM:
  type: openai
  base_url: https://ark.ap-southeast.bytepluses.com/api/v3
  model_name: dola-seed-2-1-turbo-260628
  api_key: <redacted>
  max_tokens: 1024
  temperature: 0.7

TTS.BytePlusTTS:
  type: byteplus
  api_key: <redacted>
  resource_id: seed-tts-2.0
  speaker: zh_female_liuchangnv_uranus_bigtts
  format: mp3
  sample_rate: 16000

Intent.function_call.functions: [change_role, web_search, get_news_from_newsnow]
plugins.get_weather: 已停用（共用 key 被限流）
```

> 密鑰全部 `redacted`。實際值只在伺服器的 `data/.config.yaml` 與 `patches/` 內。

---

## 5. 效能現況

| 指標 | 數值 | 說明 |
|---|---|---|
| 對話延遲（講完→出聲） | **~4 秒** | ASR 收尾 ~1s + LLM ~2s + TTS ~0.5s |
| LLM 首字（單獨測） | 1.4–1.8 s | 關思考後 |
| TTS 首塊 | 0.39 s | BytePlus 實測 |
| 拍照問答 | ~13 秒 | 裝置拍照上傳 ~7s + VLM ~6s |
| 容器記憶體 | ~180 MB | 升級 4 GB 後 swap 幾乎不用 |

已排除的瓶頸：思考鏈（22s）、prompt 過長、工具定義、opus bitrate、採樣率。

---

## 6. 重要決策記錄

1. **關掉 LLM thinking**：`dola-seed-2-1-turbo` 一句「你好」燒 1388 reasoning tokens、22 秒。
2. **16 kHz 而非 24 kHz**：必須輸入輸出同時改，否則 `BoxAudioCodec::CreateDuplexChannels` assert 重啟。
3. **TTS 用 HTTP 版而非串流版**：串流版在「工具直接回覆」（相機）路徑上不出音頻；HTTP 版正常。
4. **ASR 用 `bigmodel_async` + `language`**：保住串流，同時識別粵語；`enable_multilingual: true` 會退化成 `bigmodel_nostream`（較慢）。
5. **回覆用普通話、識別用粵語**：TTS 音色是普通話，prompt 有明確規則。
6. **移除雙擊 AEC**：它會把「短按後立刻長按」判成雙擊，吞掉長按事件。
7. **相機工具移出 HAVE_LVGL**：開 emote UI 後 `HAVE_LVGL` 不再定義，原本整段被編譯掉。

---

## 7. 運維 Runbook

### 建置與燒錄（全自動，由 agent 執行）

```
clone/pull fork → 同步 patch 與 workflow → push
→ POST /actions/workflows/build-screen-off.yml/dispatches
→ 輪詢 run → 下載 artifact → esptool write-flash 0x20000
→ 讀 COM5 serial 驗證
```

手動等效：

```powershell
py -3.12 -m esptool --chip esp32s3 -p COM5 -b 921600 write-flash 0x20000 "xiaozhi.bin"
```

### 伺服器

```bash
cd /opt/xiaozhi
docker compose up -d --force-recreate     # 改完 patches 一定要 recreate（bind mount inode）
docker compose logs --tail=30
docker logs xiaozhi-esp32-server --since 5m 2>&1 | grep -Ei '识别到文本|SentenceType|ERROR'
```

### 常見故障

| 症狀 | 原因 | 處理 |
|---|---|---|
| 裝置無限重啟 | 輸入/輸出採樣率不一致 | 兩者都改 16k |
| 沒聲音、只有 `LAST, None` | 串流 TTS 不吃工具直出文本 | 切回 `BytePlusTTS` |
| 相機說「看不到」 | `VLLM` 段缺少對應 provider 鍵 | 補 `VLLM.BytePlusLLM` |
| ASR 亂碼 | 沒送 `language` | patch `doubao_stream.py` |
| A2A 401 | 重連後 token 未刷新 | GUI 刪除後重新添加 |
| 換 IP 後裝置離線 | 固件內寫死 OTA URL | 用域名 + 改 DNS A record |

---

## 8. 主動推送（speak API）

讓伺服器**主動**叫裝置說話，不需要使用者先叫它。

### API

```bash
GET  http://iot.bgiraffe.cc:18003/api/devices
     X-API-Token: <token>
     → {"ok":true,"devices":["10:51:db:80:ea:f4"]}

POST http://iot.bgiraffe.cc:18003/api/say
     X-API-Token: <token>
     Content-Type: application/json; charset=utf-8
     {"device_id":"10:51:db:80:ea:f4","text":"提醒你，十分鐘後開會"}
     → {"ok":true,"device_id":"..."}
```

token 存放於 `/opt/xiaozhi/.say_token`（600），compose 以 `SAY_API_TOKEN` 注入。

### 命令列

```bash
bash /opt/xiaozhi/say.sh "要說的話" [device_id]   # 不給 device_id 自動取第一台在線
```

### 實作要點（踩過的坑）

`_push_say` 必須完整走一次協議狀態機，缺一不可：

```
send_tts_message(conn,"start")        # 裝置切到 speaking
投 FIRST DTO                           # 只有 FIRST 會重置 tts_text_buff/processed_chars
tts_one_sentence(text, sentence_id)    # 只投 MIDDLE，不會重置
投 LAST DTO                            # 觸發自動 stop + clearSpeakStatus
```

四個關鍵修正：

1. **`ContentType` 必須在模組層級匯入**（原本只在 handler 內局部 import，模組層級用會 NameError）。
2. **每次推送換新的 `sentence_id`** 並清 `client_abort`，避免上輪殘留音頻／abort 吃掉本輪。
3. **必須補投 `FIRST`**：`tts_one_sentence` 只發 MIDDLE，若剛結束一輪對話，`tts_text_buff` 與 `processed_chars` 還是舊的，新文本會被判定「沒有新句子」→ 永遠不出首包。
4. **成功判據是「首包真的送出」**（`tts_audio_first_sentence` 由 True 變 False），不是隊列空——否則會假成功（有日誌沒聲音）。

失敗路徑：首包 10 秒未出 → 換 `sentence_id`（丟棄遲到音頻）+ `stop` + `clearSpeakStatus`，記 WARNING。

### 限制

- **裝置必須在線**才會推送成功。
- **實測結論（2026-10-27）**：裝置的 WebSocket 壽命 = 收音會話壽命。
  退出長收音後固件呼叫 `WebsocketProtocol::CloseAudioChannel()`，內部直接 `websocket_.reset()`，
  連線立刻結束（實測 2 秒內 release）。所以「退出收音 → 待機 → 推送」目前收不到。
- 伺服器端閒置超時已設 `close_connection_no_voice_time: 86400`（24 小時），但這不是限制來源；
  限制在裝置端。
- 已加伺服器 keepalive（`connection.py::_keepalive_loop`，每 60 秒發
  `{"type":"keepalive"}`），用於對抗裝置端 `protocol.cc` 的 `kTimeoutSeconds = 120`
  （120 秒收不到任何資料就判定通道超時）。目前因連線活不到 60 秒而尚無作用，
  等固件改成常駐連線後才會生效。
- 若要「隨時可推」，需要改固件：
  1. `WebsocketProtocol::CloseAudioChannel()` 不 `reset()` WebSocket（只停音頻通道）
  2. 待機時保持連線，靠上面那條 keepalive 續命
- 推送與正常對話**可以交替**，實測互不干擾（每輪不同 `sentence_id`）。

---

## 9. 待辦

- [x] 主動推送：`POST /api/say`
- [ ] 固件常駐連線（`CloseAudioChannel` 不 reset WS + 待機 keepalive）→ 解鎖「隨時推送」
- [ ] 拍照加速（裝置端拍照上傳 ~7s，可考慮降解析度/JPEG 品質）
- [ ] 天氣功能（需自備 QWeather key）
- [ ] 串流 TTS 修好後再啟用（與「工具直出」路徑不相容）
- [ ] 定時提醒示範（cron + say.sh，需先有常駐連線）
