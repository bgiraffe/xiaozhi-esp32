# 可切換設定清單（Switch Registry）

> 這個文件是**長期維護**的：專案裡每一個「可切換 / 可調整」的東西都登記在這裡。
> 新增功能時，請同步加一節。
>
> 最後更新：2026-10-28

---

## 快速索引

| # | 名稱 | 切換方式 | 需要重啟 | 影響範圍 |
|---|---|---|---|---|
| 1 | 連線模式（normal / persist） | HTTP API | 否 | 裝置 |
| 2 | 對話 ASR provider | config + 重啟 | 是 | 伺服器 |
| 3 | 對話 TTS provider | config + 重啟 | 是 | 伺服器 |
| 4 | LLM 模型 | config + 重啟 | 是 | 伺服器 |
| 5 | ASR 語言（粵語 / 普通話） | config + 重啟 | 是 | 伺服器 |
| 6 | 音頻位元率 / 採樣率 | 環境變數 / config | 是 | 兩端 |
| 7 | 天氣工具開關 | config + 重啟 | 是 | 伺服器 |
| 8 | 推送音檔位元率 | patch + 重啟 | 是 | 伺服器 |
| 9 | 螢幕熄屏時間 | 固件常數 + 重燒 | 是 | 裝置 |
| 10 | 常駐連線開機預設 | 固件 patch + 重燒 | 是 | 裝置 |

---

## 1. 連線模式（normal / persist）

**作用**：決定裝置退出對話後是否保留 WebSocket。

| 模式 | 行為 | 待機耗電（估） |
|---|---|---|
| `normal` | 回覆播完就斷線 | 25–40 mA（2–3 天） |
| `persist` | 保持連線，可隨時推送 | 40–55 mA（1.5–2 天） |

**切換（裝置必須在線）**

```bash
TOKEN=$(cat /opt/xiaozhi/.say_token)

# 切常駐
curl -s -X POST -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"mode":"persist"}' http://127.0.0.1:8003/api/mode

# 切一般
curl -s -X POST -H "X-API-Token: $TOKEN" -H 'Content-Type: application/json' \
     -d '{"mode":"normal"}'  http://127.0.0.1:8003/api/mode

# 查在線
curl -s -H "X-API-Token: $TOKEN" http://127.0.0.1:8003/api/devices
```

**注意**
- 需要重啟？**不需要**（即時生效，寫入裝置 NVS）
- **裝置重開機一律回到 `normal`**（固件在 `Application::Initialize()` 清掉旗標）
- 裝置離線時無法切換（指令靠 WebSocket 下發）→ 先短按/長按讓它上線
- 切 `normal` 會**立即斷線**

---

## 2. 對話 ASR provider

**位置**：`data/.config.yaml` → `selected_module.ASR`

**目前**：`BytePlusASR`（doubao_stream，串流，`language: yue-CN`）

**可選（鏡像內建）**

| 名稱 | 檔案 | 備註 |
|---|---|---|
| `BytePlusASR` | patches/doubao_stream.py | 目前使用，支援粵語 |
| `AliyunASR` / `AliyunStreamASR` | aliyun*.py | |
| `BaiduASR` | baidu.py | |
| `TencentASR` | tencent.py | |
| `XunfeiStreamASR` | xunfei_stream.py | |
| `OpenaiASR` | openai.py | Whisper，非串流 |
| `Qwen3ASRFlash` | qwen3_asr_flash.py | |
| `FunASR` / `Vosk` / `SherpaOnnx` | fun_*.py / vosk.py / sherpa_onnx_local.py | 本地，吃 CPU/RAM |

**切換**：改 `selected_module.ASR` → `docker compose up -d --force-recreate`

**注意**：每個 provider 需要自己的 `api_key` / `resource_id` 區塊。

---

## 3. 對話 TTS provider

**位置**：`data/.config.yaml` → `selected_module.TTS`

**目前**：`BytePlusTTS`（HTTP，16k mp3，普通話女聲）

**可選（鏡像內建）**

| 名稱 | 檔案 | 備註 |
|---|---|---|
| `BytePlusTTS` | patches/byteplus.py | 目前使用 |
| `BytePlusTTSStream` | patches/byteplus_tts_stream.py | 串流 v3，與「工具直出」不相容 |
| `EdgeTTS` | edge.py | 免費，音質一般，可作對照 |
| `AliyunTTS` / `TencentTTS` / `XunfeiTTS` | | |
| `DoubaoTTS` | doubao.py | |
| `OpenAITTS` | openai.py | |
| `MinimaxTTS` / `SiliconFlowTTS` / `FishSpeech` / `GPTSoVITS` | | |

**切換**：改 `selected_module.TTS` → 重啟

---

## 4. LLM 模型

**位置**：`LLM.BytePlusLLM.model_name`（目前 `dola-seed-2-1-turbo-260628`）

**注意**
- 必須關掉思考鏈：`patches/openai.py` 的 `THINKING_DISABLED_DOMAINS` 含 `bytepluses.com`
- 這個 API key 只開通 `dola-*`；其他模型要在 BytePlus 控制台開 endpoint
- 換模型後延遲會變（目前首句 ~2s）

---

## 5. ASR 語言

**位置**：`ASR.BytePlusASR.language`（目前 `yue-CN`）＋ `enable_multilingual: false`

| 設定 | 效果 |
|---|---|
| `language: yue-CN` + `enable_multilingual: false` | 粵語識別、**保留串流**（目前） |
| `enable_multilingual: true` | 走 `bigmodel_nostream`，較慢 |
| 移除 `language` | 普通話 |

**注意**：`patches/doubao_stream.py` 已改成「無條件送出 language」，所以 async 端點也吃得到。

---

## 6. 音頻位元率 / 採樣率

| 項目 | 位置 | 目前值 |
|---|---|---|
| 對話 opus 位元率 | `docker-compose.yml` → `OPUS_BITRATE` | `16000` |
| 對話採樣率 | 固件 `config.h` + `config` 的 `xiaozhi.audio_params.sample_rate` | `16000`（兩端必須一致） |
| 推送音檔位元率 | `patches/http_server.py` ffmpeg `-b:a` | `16k` |

**注意**：固件輸入/輸出採樣率不一致會 assert 重啟。

---

## 7. 天氣工具開關

**位置**：`Intent.function_call.functions`（`- get_weather`）

**目前**：**已停用**（共用 API key 被限流，`Authentication failed`）

**重新啟用**：申請 QWeather key → 改 `plugins.get_weather.api_key` → 取消註解

---

## 8. 推送音檔參數

**位置**：`patches/http_server.py` → `_push_notify_say`

| 參數 | 目前 |
|---|---|
| 格式 | OGG / Opus / mono / 16 kHz（`NotifyPlayer` 只吃 OGG） |
| 位元率 | `-b:a 16k`（實測 ~14.7 kbps） |
| 清理 | 推送時刪除超過 1 小時的舊檔 |

---

## 9. 螢幕熄屏時間

**位置**：`main/boards/lckfb/szpi-esp32s3/lichuang_dev_board.cc` → `kScreenOffSeconds`

**目前**：`30`（秒）

**注意**：熄屏是三層（背光 0 → 面板 DISPOFF → 停渲染），改常數後要重新 build + 燒錄。

---

## 10. 常駐連線開機預設

**位置**：`patch/apply_persist_ws.py` 的 boot-reset 段

**目前**：**開機一律 reset 成 `normal`**

**若想開機就常駐**：移除 `Application::Initialize()` 裡那段 `SetBool("persist_ws", false)`。

---

## 附：伺服器版控

```bash
cd /opt/xiaozhi
git log --oneline          # 目前 0b093d3
git status                 # 應為乾淨
```

追蹤內容：`docker-compose.yml`、`patches/*`、`say.sh`、`config.sample.yaml`（去敏）。
`data/`、`.say_token` 不入版控。

**回滾方式**：改壞了就把對應檔案 `git checkout -- <file>`，然後 `docker compose up -d --force-recreate`。
