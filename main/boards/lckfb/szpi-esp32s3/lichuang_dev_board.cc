#include "wifi_board.h"
#include "codecs/box_audio_codec.h"
#include "display/lcd_display.h"
#include "display/emote_display.h"
#include "application.h"
#include "button.h"
#include "config.h"
#include "i2c_device.h"
#include "esp32_camera.h"
#include "mcp_server.h"

#include <esp_log.h>
#include <esp_timer.h>
#include <esp_lcd_panel_vendor.h>
#include <driver/i2c_master.h>
#include <driver/spi_common.h>
#include <esp_lcd_touch_ft5x06.h>
#include <esp_lvgl_port.h>
#include <lvgl.h>

#define TAG "LichuangDevBoard"

class Pca9557 : public I2cDevice {
public:
    Pca9557(i2c_master_bus_handle_t i2c_bus, uint8_t addr) : I2cDevice(i2c_bus, addr) {
        WriteReg(0x01, 0x03);
        WriteReg(0x03, 0xf8);
    }

    void SetOutputState(uint8_t bit, uint8_t level) {
        uint8_t data = ReadReg(0x01);
        data = (data & ~(1 << bit)) | (level << bit);
        WriteReg(0x01, data);
    }
};

class CustomAudioCodec : public BoxAudioCodec {
private:
    Pca9557* pca9557_;

public:
    CustomAudioCodec(i2c_master_bus_handle_t i2c_bus, Pca9557* pca9557)
        : BoxAudioCodec(i2c_bus, AUDIO_INPUT_SAMPLE_RATE, AUDIO_OUTPUT_SAMPLE_RATE,
                        AUDIO_I2S_GPIO_MCLK, AUDIO_I2S_GPIO_BCLK, AUDIO_I2S_GPIO_WS,
                        AUDIO_I2S_GPIO_DOUT, AUDIO_I2S_GPIO_DIN, GPIO_NUM_NC,
                        AUDIO_CODEC_ES8311_ADDR, AUDIO_CODEC_ES7210_ADDR, AUDIO_INPUT_REFERENCE,
                        28.0f,  // Physical MIC1 gain
                        2,      // Physical MIC3 is the playback reference input
                        0.0f),
          pca9557_(pca9557) {}

    virtual void EnableOutput(bool enable) override {
        BoxAudioCodec::EnableOutput(enable);
        if (enable) {
            pca9557_->SetOutputState(1, 1);
        } else {
            pca9557_->SetOutputState(1, 0);
        }
    }
};

class LichuangDevBoard : public WifiBoard {
private:
    i2c_master_bus_handle_t i2c_bus_;
    i2c_master_dev_handle_t pca9557_handle_;
    Button boot_button_;
    Display* display_;
    Pca9557* pca9557_;
    Esp32Camera* camera_;

    // ================= 可調參數 =================
    static constexpr int      kScreenOffSeconds   = 30;    // 一般模式：幾秒熄屏
    static constexpr uint8_t  kFallbackBrightness = 75;    // 熄屏前量不到亮度時的備用值
    static constexpr uint16_t kLongPressMs        = 400;   // 按住多久開始收音
    static constexpr bool     kTouchWake          = false; // 觸摸喚醒（已測試完，關閉）

    // 對話中的閒置計時（愛睏 / 關屏）
    static constexpr int      kSleepyAfterSec     = 180;   // 閒置 3 分鐘 -> 愛睏表情
    static constexpr int      kClickOffAfterSec   = 210;   // 再過 30 秒（3 分 30 秒）-> 關屏

    // ================= 狀態 =================
    esp_timer_handle_t screen_timer_ = nullptr;
    int idle_seconds_ = 0;
    int conv_idle_seconds_ = 0;
    int tick_div_ = 0;
    bool screen_off_ = false;
    bool long_press_started_ = false;
    bool sleepy_ = false;                           // 對話中閒置太久，進入愛睏表情
    bool suppress_wake_ = false;                    // 長按說話期間，不要因為「有活動」就亮屏
    uint8_t saved_brightness_ = kFallbackBrightness;
    esp_lcd_touch_handle_t tp_ = nullptr;
    esp_lcd_panel_handle_t panel_ = nullptr;   // 保留面板 handle，用於 DISPOFF/DISPON

    void StartScreenTimer() {
        const esp_timer_create_args_t timer_args = {
            .callback = [](void* arg) {
                static_cast<LichuangDevBoard*>(arg)->OnScreenTimerTick();
            },
            .arg = this,
            .dispatch_method = ESP_TIMER_TASK,
            .name = "screen_idle",
            .skip_unhandled_events = true,
        };
        ESP_ERROR_CHECK(esp_timer_create(&timer_args, &screen_timer_));
        // 200ms 一次：一般熄屏只需要秒級，但語音模式要「一出聲就亮」，所以要快一點
        ESP_ERROR_CHECK(esp_timer_start_periodic(screen_timer_, 200 * 1000));
        ESP_LOGI(TAG, "Screen idle timer started (off %d s, long press %d ms)",
                 kScreenOffSeconds, (int)kLongPressMs);
    }

    void OnScreenTimerTick() {
        auto& app = Application::GetInstance();
        DeviceState state = app.GetDeviceState();
        bool one_second = ((++tick_div_) % 5) == 0;   // 200ms x 5 = 1 秒

        // ===== 對話中（狀態由裝置自己決定，我不猜）=====
        // 排除「長按說話」那一輪：那是長按模式，維持原本的 30 秒邏輯
        bool in_conversation =
            (state == kDeviceStateListening || state == kDeviceStateSpeaking) && !suppress_wake_;

        if (in_conversation) {
            if (app.IsVoiceDetected()) {
                conv_idle_seconds_ = 0;
                if (sleepy_) {
                    SetSleepyFace(false);
                }
                if (screen_off_) {
                    ESP_LOGI(TAG, "Conversation: voice detected -> screen on");
                    WakeScreen();
                }
                return;
            }

            if (!one_second) {
                return;
            }

            conv_idle_seconds_++;

            if (!sleepy_ && conv_idle_seconds_ >= kSleepyAfterSec) {
                ESP_LOGI(TAG, "Conversation idle %d s -> sleepy", conv_idle_seconds_);
                SetSleepyFace(true);
            }
            if (!screen_off_ && conv_idle_seconds_ >= kClickOffAfterSec) {
                ESP_LOGI(TAG, "Conversation idle %d s -> screen off", conv_idle_seconds_);
                SleepScreen();
            }
            return;
        }

        // 離開對話就重置計時
        conv_idle_seconds_ = 0;
        if (sleepy_ && !screen_off_) {
            SetSleepyFace(false);
        }

        // ===== 一般情況：idle 30 秒熄屏 =====
        // 注意：不要用 app.CanEnterSleepMode()——persist_ws 模式下通道常開，
        // 它會一直回 false，導致螢幕永不熄滅。只看裝置狀態即可。
        bool busy = state != kDeviceStateIdle;

        if (!busy) {
            // 回到閒置 -> 解除「長按期間不亮屏」的鎖
            suppress_wake_ = false;
        }

        if (busy) {
            idle_seconds_ = 0;
            // 若是「長按說話」引起的活動，就不要把屏幕叫亮
            if (!suppress_wake_) {
                WakeScreen();
            }
            return;
        }

        if (screen_off_) {
            // 只在黑屏時讀觸摸，避免干擾 LVGL 的正常輸入處理
            if (kTouchWake && TouchDetected()) {
                ESP_LOGI(TAG, "TOUCH TEST: PASS -> screen on");
                WakeScreen();
                idle_seconds_ = 0;
            }
            return;
        }

        if (one_second && ++idle_seconds_ >= kScreenOffSeconds) {
            SleepScreen();
        }
    }

    void SetSleepyFace(bool sleepy) {
        sleepy_ = sleepy;
        auto display = GetDisplay();
        if (display == nullptr) {
            return;
        }
        // 這兩組名字是本版資源裡最接近「愛睏 / 精神」的表情
        display->SetEmotion(sleepy ? "relaxed" : "happy");
        ESP_LOGI(TAG, "Emotion -> %s", sleepy ? "relaxed(sleepy)" : "happy(awake)");
    }

    // 直接讀 FT5x06（不依賴 LVGL 的 inactive time，結果確定）
    bool TouchDetected() {
        if (tp_ == nullptr) {
            return false;
        }
        if (esp_lcd_touch_read_data(tp_) != ESP_OK) {
            return false;
        }
        uint8_t points = tp_->data.points;
        if (points == 0) {
            return false;
        }
        tp_->data.points = 0;  // 吃掉這批資料，避免 LVGL 重複處理
        ESP_LOGI(TAG, "TOUCH: %u point(s)", (unsigned)points);
        return true;
    }

    void SleepScreen() {
        auto backlight = GetBacklight();
        if (backlight == nullptr || screen_off_) {
            return;
        }
        screen_off_ = true;

        saved_brightness_ = backlight->brightness();
        if (saved_brightness_ < 10) {
            // 你之前用語音把亮度關到 0 的情況，記一個看得到的值
            saved_brightness_ = kFallbackBrightness;
        }

        backlight->SetBrightness(0);  // permanent = false，不寫入 NVS
        // 真正的關屏：送 ST7789 DISPOFF，面板停止掃描（比只關背光更省電）
        if (panel_ != nullptr) {
            esp_lcd_panel_disp_on_off(panel_, false);
        }
        ESP_LOGI(TAG, "Screen off (conv idle=%d s, idle=%d s, saved brightness %d)",
                 conv_idle_seconds_, idle_seconds_, saved_brightness_);
    }

    void WakeScreen() {
        auto backlight = GetBacklight();
        if (backlight == nullptr || !screen_off_) {
            return;
        }
        screen_off_ = false;
        if (panel_ != nullptr) {
            esp_lcd_panel_disp_on_off(panel_, true);
        }
        backlight->SetBrightness(saved_brightness_);
        ESP_LOGI(TAG, "Screen on (brightness %d)", saved_brightness_);
    }

    // ===================================================

    void InitializeI2c() {
        // Initialize I2C peripheral
        i2c_master_bus_config_t i2c_bus_cfg = {
            .i2c_port = (i2c_port_t)1,
            .sda_io_num = AUDIO_CODEC_I2C_SDA_PIN,
            .scl_io_num = AUDIO_CODEC_I2C_SCL_PIN,
            .clk_source = I2C_CLK_SRC_DEFAULT,
            .glitch_ignore_cnt = 7,
            .intr_priority = 0,
            .trans_queue_depth = 0,
            .flags = {
                .enable_internal_pullup = 1,
            },
        };
        ESP_ERROR_CHECK(i2c_new_master_bus(&i2c_bus_cfg, &i2c_bus_));

        // Initialize PCA9557
        pca9557_ = new Pca9557(i2c_bus_, 0x19);
    }

    void InitializeSpi() {
        spi_bus_config_t buscfg = {};
        buscfg.mosi_io_num = GPIO_NUM_40;
        buscfg.miso_io_num = GPIO_NUM_NC;
        buscfg.sclk_io_num = GPIO_NUM_41;
        buscfg.quadwp_io_num = GPIO_NUM_NC;
        buscfg.quadhd_io_num = GPIO_NUM_NC;
        buscfg.max_transfer_sz = DISPLAY_WIDTH * DISPLAY_HEIGHT * sizeof(uint16_t);
        ESP_ERROR_CHECK(spi_bus_initialize(SPI3_HOST, &buscfg, SPI_DMA_CH_AUTO));
    }

    void InitializeButtons() {
        // 按下：什麼都不做 —— 不碰屏幕、不碰音頻
        boot_button_.OnPressDown([this]() {
            long_press_started_ = false;
        });

        // 按住 kLongPressMs：只在「沒在對話」時有效（對話中長按完全不干預）
        boot_button_.OnLongPress([this]() {
            auto& app = Application::GetInstance();
            DeviceState st = app.GetDeviceState();
            if (st != kDeviceStateIdle) {
                // 對話中（聆聽/說話/連線）-> 長按無效，避免打斷
                ESP_LOGI(TAG, "Long press ignored (state=%d)", (int)st);
                return;
            }
            long_press_started_ = true;
            suppress_wake_ = true;   // 別讓狀態變化把屏幕叫亮
            app.StartListening();
            ESP_LOGI(TAG, "Long press -> start listening (screen untouched)");
        });

        // 鬆手：結束收音並送出
        boot_button_.OnPressUp([this]() {
            if (long_press_started_) {
                long_press_started_ = false;
                Application::GetInstance().StopListening();
                ESP_LOGI(TAG, "Release -> stop listening");
            }
        });

        // 短按：黑屏只亮屏；亮屏 = 原廠的開/關對話
        boot_button_.OnClick([this]() {
            auto& app = Application::GetInstance();
            suppress_wake_ = false;   // 任何短按都解除「長按不亮屏」的鎖

            if (screen_off_) {
                WakeScreen();
                idle_seconds_ = 0;
                ESP_LOGI(TAG, "Short press -> screen on only");
                return;
            }

            // During startup (before connected), pressing BOOT button enters Wi-Fi config mode without reboot
            if (app.GetDeviceState() == kDeviceStateStarting) {
                EnterWifiConfigMode();
                return;
            }

            // 短按 = 原廠那顆鍵的行為：開/關對話
            app.ToggleChatState();
            conv_idle_seconds_ = 0;
            if (sleepy_) {
                SetSleepyFace(false);
            }
            ESP_LOGI(TAG, "Short press -> toggle chat");
        });

        // 注意：原廠的「雙擊切換 AEC 模式」已移除。
        // 原因：短按關閉對話後立刻長按時，第二次按壓會落進雙擊判定窗口，
        // 被 DOUBLE_CLICK 吃掉 → LONG_PRESS 永遠不發 → 長按看起來「沒反應」。
    }

    void InitializeSt7789Display() {
        esp_lcd_panel_io_handle_t panel_io = nullptr;
        esp_lcd_panel_handle_t panel = nullptr;
        // 液晶屏控制IO初始化
        ESP_LOGD(TAG, "Install panel IO");
        esp_lcd_panel_io_spi_config_t io_config = {};
        io_config.cs_gpio_num = GPIO_NUM_NC;
        io_config.dc_gpio_num = GPIO_NUM_39;
        io_config.spi_mode = 2;
        io_config.pclk_hz = 80 * 1000 * 1000;
        io_config.trans_queue_depth = 10;
        io_config.lcd_cmd_bits = 8;
        io_config.lcd_param_bits = 8;
        ESP_ERROR_CHECK(esp_lcd_new_panel_io_spi(SPI3_HOST, &io_config, &panel_io));

        // 初始化液晶屏驱动芯片ST7789
        ESP_LOGD(TAG, "Install LCD driver");
        esp_lcd_panel_dev_config_t panel_config = {};
        panel_config.reset_gpio_num = GPIO_NUM_NC;
        panel_config.rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB;
        panel_config.bits_per_pixel = 16;
        ESP_ERROR_CHECK(esp_lcd_new_panel_st7789(panel_io, &panel_config, &panel));

        esp_lcd_panel_reset(panel);
        pca9557_->SetOutputState(0, 0);

        esp_lcd_panel_init(panel);
        esp_lcd_panel_invert_color(panel, true);
        esp_lcd_panel_swap_xy(panel, DISPLAY_SWAP_XY);
        esp_lcd_panel_mirror(panel, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y);
        esp_lcd_panel_disp_on_off(panel, true);
        panel_ = panel;   // 存起來，SleepScreen/WakeScreen 要用

#if CONFIG_USE_EMOTE_MESSAGE_STYLE
        display_ = new emote::EmoteDisplay(panel, panel_io, DISPLAY_WIDTH, DISPLAY_HEIGHT);
#else
        display_ = new SpiLcdDisplay(panel_io, panel,
            DISPLAY_WIDTH, DISPLAY_HEIGHT, DISPLAY_OFFSET_X, DISPLAY_OFFSET_Y, DISPLAY_MIRROR_X, DISPLAY_MIRROR_Y, DISPLAY_SWAP_XY);
#endif
    }

    void InitializeTouch()
    {
        esp_lcd_touch_handle_t tp;
        esp_lcd_touch_config_t tp_cfg = {
            .x_max = DISPLAY_HEIGHT,
            .y_max = DISPLAY_WIDTH,
            .rst_gpio_num = GPIO_NUM_NC, // Shared with LCD reset
            .int_gpio_num = GPIO_NUM_NC,
            .levels = {
                .reset = 0,
                .interrupt = 0,
            },
            .flags = {
                .swap_xy = 1,
                .mirror_x = 1,
                .mirror_y = 0,
            },
        };
        esp_lcd_panel_io_handle_t tp_io_handle = NULL;
        esp_lcd_panel_io_i2c_config_t tp_io_config = {
            .dev_addr = ESP_LCD_TOUCH_IO_I2C_FT5x06_ADDRESS,
            .control_phase_bytes = 1,
            .dc_bit_offset = 0,
            .lcd_cmd_bits = 8,
            .flags =
            {
                .disable_control_phase = 1,
            }
        };
        tp_io_config.scl_speed_hz = 400000;

        esp_lcd_new_panel_io_i2c(i2c_bus_, &tp_io_config, &tp_io_handle);
        esp_lcd_touch_new_i2c_ft5x06(tp_io_handle, &tp_cfg, &tp);
        assert(tp);
        tp_ = tp;   // 保存下來，熄屏時自己讀

        /* Add touch input (for selected screen) */
        const lvgl_port_touch_cfg_t touch_cfg = {
            .disp = lv_display_get_default(),
            .handle = tp,
        };

        if(touch_cfg.disp) {
            lvgl_port_add_touch(&touch_cfg);
        } else {
            ESP_LOGE(TAG, "Touch display is not initialized");
        }
    }

    void InitializeCamera() {
        // Open camera power
        pca9557_->SetOutputState(2, 0);

        camera_config_t config = {};
        config.ledc_channel = LEDC_CHANNEL_2;
        config.ledc_timer = LEDC_TIMER_2;
        config.pin_d0 = CAMERA_PIN_D0;
        config.pin_d1 = CAMERA_PIN_D1;
        config.pin_d2 = CAMERA_PIN_D2;
        config.pin_d3 = CAMERA_PIN_D3;
        config.pin_d4 = CAMERA_PIN_D4;
        config.pin_d5 = CAMERA_PIN_D5;
        config.pin_d6 = CAMERA_PIN_D6;
        config.pin_d7 = CAMERA_PIN_D7;
        config.pin_xclk = CAMERA_PIN_XCLK;
        config.pin_pclk = CAMERA_PIN_PCLK;
        config.pin_vsync = CAMERA_PIN_VSYNC;
        config.pin_href = CAMERA_PIN_HREF;
        config.pin_sccb_sda = -1;
        config.pin_sccb_scl = CAMERA_PIN_SIOC;
        config.sccb_i2c_port = 1;
        config.pin_pwdn = CAMERA_PIN_PWDN;
        config.pin_reset = CAMERA_PIN_RESET;
        config.xclk_freq_hz = XCLK_FREQ_HZ;
        config.pixel_format = PIXFORMAT_RGB565;
        config.frame_size = FRAMESIZE_QVGA;
        config.jpeg_quality = 12;
        config.fb_count = 1;
        config.fb_location = CAMERA_FB_IN_PSRAM;
        config.grab_mode = CAMERA_GRAB_WHEN_EMPTY;

        camera_ = new Esp32Camera(config);
    }

    void InitializeTools() {
        auto &mcp_server = McpServer::GetInstance();
        mcp_server.AddTool("self.system.reconfigure_wifi",
            "End this conversation and enter WiFi configuration mode.\n"
            "**CAUTION** You must ask the user to confirm this action.",
            PropertyList(), [this](const PropertyList& properties) {
                EnterWifiConfigMode();
                return true;
            });
    }

public:
    LichuangDevBoard() : boot_button_(BOOT_BUTTON_GPIO, false, kLongPressMs, 0) {
        InitializeI2c();
        InitializeSpi();
        InitializeSt7789Display();
        InitializeTouch();
        InitializeButtons();
        InitializeCamera();
        InitializeTools();

        GetBacklight()->RestoreBrightness();

        // 啟動「閒置 30 秒關背光」的計時器
        StartScreenTimer();
    }

    virtual AudioCodec* GetAudioCodec() override {
        static CustomAudioCodec audio_codec(
            i2c_bus_,
            pca9557_);
        return &audio_codec;
    }

    virtual Display* GetDisplay() override {
        return display_;
    }

    virtual Backlight* GetBacklight() override {
        static PwmBacklight backlight(DISPLAY_BACKLIGHT_PIN, DISPLAY_BACKLIGHT_OUTPUT_INVERT);
        return &backlight;
    }

    virtual Camera* GetCamera() override {
        return camera_;
    }
};

DECLARE_BOARD(LichuangDevBoard);
