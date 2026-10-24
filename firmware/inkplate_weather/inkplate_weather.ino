// Inkplate 10 weather display client.
//
// Each wake (timer, wake button, or power-on):
//   read battery -> join Wi-Fi -> GET /display.png?batt=&mv=&reason= -> draw full screen in
//   3-bit grayscale -> full refresh -> Wi-Fi off -> deep sleep until the next :00/:30.
// If anything fails, draw a local error screen and retry in 5 minutes (up to 3 times), then fall
// back to the normal schedule. See firmware/README.md and docs/ARCHITECTURE.md (/display.png contract).

#if !defined(ARDUINO_INKPLATE10) && !defined(ARDUINO_INKPLATE10V2)
#error "Select the board 'Soldered Inkplate 10' (Inkplate10V2) or 'e-radionica Inkplate 10' (Inkplate10)."
#endif

#include <Inkplate.h>
#include <WiFi.h>
#include <esp_sleep.h>
#include <esp_timer.h>

#include "config.h" // copy config.example.h to config.h (gitignored) and fill it in

#include "battery.h"
#include "fetch.h"
#include "timekeeping.h"

#include "fonts/FreeSans18pt7b.h"
#include "fonts/FreeSansBold24pt7b.h"

// ---------------------------------------------------------------------------------------------
// Plain-language messages. Same wording as server/app/messages.py SERVER_UNREACHABLE
// ("Can't reach the weather server at {host}. Will retry at {time}."), split into its two
// sentences so each starts on its own line.
// ---------------------------------------------------------------------------------------------
static const char MSG_UNREACHABLE[] = "Can't reach the weather server at %s.";
static const char MSG_RETRY_AT[] = "Will retry at %s.";
static const char MSG_RETRY_IN[] = "Will retry in %u minutes."; // when the clock isn't known yet
static const char MSG_BUTTON_HINT[] = "Press the wake button on the side to try again now.";

// Inkplate 10 wake button (both board versions): GPIO36, active low, wakes via ext0.
static const gpio_num_t WAKE_BUTTON = GPIO_NUM_36;

// If the device is still awake after this long, something hung: count it as a failure and sleep.
static const uint64_t FAILSAFE_AWAKE_US = 120ULL * 1000000ULL;

// 3-bit grayscale palette indices (0 = black ... 7 = white).
static const uint8_t INK_BLACK = 0;
static const uint8_t INK_DARK = 2;
static const uint8_t INK_MID = 4;

// Survives deep sleep (reset to 0 on power-on).
RTC_DATA_ATTR static uint8_t rtcRetryCount = 0;     // failed attempts since the last success
RTC_DATA_ATTR static bool rtcRetryPending = false;  // next timer wake is a retry
RTC_DATA_ATTR static uint32_t rtcWakeCount = 0;

Inkplate display(INKPLATE_3BIT);

static int batteryMv = -1;
static int batteryPct = -1;

// ---------------------------------------------------------------------------------------------
// Sleep
// ---------------------------------------------------------------------------------------------

static void wifiOff()
{
    if (WiFi.getMode() != WIFI_OFF)
    {
        WiFi.disconnect(true);
        WiFi.mode(WIFI_OFF);
    }
}

static void deepSleep(uint32_t seconds)
{
    wifiOff();

    // Don't go to sleep while the button is still held, or ext0 would wake us again immediately.
    pinMode(WAKE_BUTTON, INPUT);
    const uint32_t t0 = millis();
    while (digitalRead(WAKE_BUTTON) == LOW && millis() - t0 < 10000)
        delay(20);

    Serial.printf("[sleep] %lu s (retry %u/%d, next wake %s)\n", (unsigned long)seconds, rtcRetryCount,
                  RETRY_COUNT, rtcRetryPending ? "is a retry" : "is scheduled");
    Serial.flush();
    esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
    esp_sleep_enable_ext0_wakeup(WAKE_BUTTON, 0);
    esp_deep_sleep_start();
    for (;;)
    {
    }
}

// Seconds until the next regular refresh when the server didn't tell us.
static uint32_t scheduledSleepSeconds()
{
    if (timeIsValid())
        return timeSecondsToNextSlot(REFRESH_MINUTES, ALIGN_OFFSET_SECONDS);
    return (uint32_t)REFRESH_MINUTES * 60;
}

// Bookkeeping after a failed attempt; returns how long to sleep.
static uint32_t noteFailure()
{
    if (rtcRetryCount < RETRY_COUNT)
    {
        rtcRetryCount++;
        rtcRetryPending = true;
        return RETRY_DELAY_SECONDS;
    }
    // Retries used up: go back to the normal schedule; the count starts over after that wake.
    rtcRetryCount = 0;
    rtcRetryPending = false;
    return scheduledSleepSeconds();
}

static void failsafeTimeout(void *)
{
    Serial.println("[failsafe] still awake after 120 s; sleeping");
    deepSleep(noteFailure());
}

// ---------------------------------------------------------------------------------------------
// Error screen
// ---------------------------------------------------------------------------------------------

// Word-wrap `text` into the box starting at (x, baselineY), at most maxW wide, in the current font.
// Returns the baseline for the line after the paragraph. With draw=false it only measures; *tooWide
// is set when a single word is wider than maxW.
static int drawWrapped(const char *text, int x, int baselineY, int maxW, int lineH, bool draw, bool *tooWide)
{
    char line[160] = "";
    size_t lineLen = 0;
    const char *p = text;
    int y = baselineY;
    int16_t bx, by;
    uint16_t bw, bh;
    while (*p)
    {
        while (*p == ' ')
            p++;
        const char *wordEnd = p;
        while (*wordEnd && *wordEnd != ' ')
            wordEnd++;
        size_t wordLen = (size_t)(wordEnd - p);
        if (wordLen == 0)
            break;
        if (wordLen > sizeof(line) - 2)
            wordLen = sizeof(line) - 2;

        // Try appending the word to the current line.
        char trial[sizeof(line)];
        size_t n = lineLen;
        memcpy(trial, line, n);
        if (n && n + 1 + wordLen < sizeof(trial))
            trial[n++] = ' ';
        size_t take = (n + wordLen < sizeof(trial)) ? wordLen : sizeof(trial) - 1 - n;
        memcpy(trial + n, p, take);
        n += take;
        trial[n] = '\0';

        display.getTextBounds(trial, 0, 0, &bx, &by, &bw, &bh);
        if ((int)bw > maxW && lineLen)
        {
            // Doesn't fit: emit the current line and start a new one with this word.
            if (draw)
            {
                display.setCursor(x, y);
                display.print(line);
            }
            y += lineH;
            memcpy(line, p, wordLen);
            lineLen = wordLen;
        }
        else
        {
            memcpy(line, trial, n);
            lineLen = n;
        }
        line[lineLen] = '\0';
        display.getTextBounds(line, 0, 0, &bx, &by, &bw, &bh);
        if ((int)bw > maxW && tooWide)
            *tooWide = true;
        p = wordEnd;
    }
    if (line[0])
    {
        if (draw)
        {
            display.setCursor(x, y);
            display.print(line);
        }
        y += lineH;
    }
    return y;
}

static const int ERR_MARGIN = 70;
static const int ERR_FOOTER_BASELINE = DISPLAY_H - ERR_MARGIN + 20;

// Lays out (and optionally draws) the error text. headScale 2 = big headline, 1 = fallback when the
// message is too long for the big size. Returns the baseline after the last line.
static int layoutErrorText(const char *s1, const char *s2, const char *detail, int headScale, bool draw,
                           bool *tooWide)
{
    const int width = DISPLAY_W - 2 * ERR_MARGIN;
    const int headLine = headScale == 2 ? 104 : 58;

    // Headline: 24 pt bold, at 2x (about 70 px capitals) so it reads from across the room.
    display.setFont(&FreeSansBold24pt7b);
    display.setTextSize(headScale);
    display.setTextColor(INK_BLACK);
    int y = ERR_MARGIN + (headScale == 2 ? 70 : 40);
    y = drawWrapped(s1, ERR_MARGIN, y, width, headLine, draw, tooWide);
    y += 20;
    y = drawWrapped(s2, ERR_MARGIN, y, width, headLine, draw, tooWide);

    // Details: normal size, dark gray.
    display.setFont(&FreeSans18pt7b);
    display.setTextSize(1);
    display.setTextColor(INK_DARK);
    y += 30;
    if (detail && detail[0])
        y = drawWrapped(detail, ERR_MARGIN, y, width, 46, draw, nullptr);
    y = drawWrapped(MSG_BUTTON_HINT, ERR_MARGIN, y, width, 46, draw, nullptr);
    return y;
}

static void drawErrorScreen(const char *detail, uint32_t sleepSeconds)
{
    char host[72];
    if (SERVER_PORT == 80)
        snprintf(host, sizeof(host), "%s", SERVER_HOST);
    else
        snprintf(host, sizeof(host), "%s:%u", SERVER_HOST, (unsigned)SERVER_PORT);

    char sentence1[160], sentence2[64];
    snprintf(sentence1, sizeof(sentence1), MSG_UNREACHABLE, host);
    if (timeIsValid())
    {
        char clock[16];
        timeFormatClock(time(nullptr) + sleepSeconds, clock, sizeof(clock));
        snprintf(sentence2, sizeof(sentence2), MSG_RETRY_AT, clock);
    }
    else
    {
        snprintf(sentence2, sizeof(sentence2), MSG_RETRY_IN, (unsigned)((sleepSeconds + 30) / 60));
    }

    display.clearDisplay();
    display.setTextWrap(false);

    // Measure at the big size first; fall back to normal size if a word overflows or the text would
    // run into the footer.
    bool tooWide = false;
    int end = layoutErrorText(sentence1, sentence2, detail, 2, false, &tooWide);
    const int scale = (tooWide || end - 46 > ERR_FOOTER_BASELINE - 50) ? 1 : 2;
    layoutErrorText(sentence1, sentence2, detail, scale, true, nullptr);

    // Footer: battery, small and light.
    display.setTextColor(INK_MID);
    char footer[64];
    if (batteryPct >= 0)
        snprintf(footer, sizeof(footer), "Battery %d%% (%.2f V)", batteryPct, batteryMv / 1000.0);
    else
        snprintf(footer, sizeof(footer), "Battery level unknown");
    display.setCursor(ERR_MARGIN, ERR_FOOTER_BASELINE);
    display.print(footer);

    display.display();
}

// ---------------------------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------------------------

static const char *wakeReason()
{
    switch (esp_sleep_get_wakeup_cause())
    {
    case ESP_SLEEP_WAKEUP_EXT0:
        return "button";
    case ESP_SLEEP_WAKEUP_TIMER:
        return rtcRetryPending ? "retry" : "timer";
    default:
        return "boot";
    }
}

static void readBattery()
{
    // Read before Wi-Fi starts: radio current makes the cell voltage sag.
    double sum = 0;
    const int samples = 4;
    for (int i = 0; i < samples; ++i)
        sum += display.readBattery();
    const int mv = (int)(sum / samples * 1000.0 + 0.5);
    if (mv >= BATTERY_MV_MIN_PLAUSIBLE && mv <= BATTERY_MV_MAX_PLAUSIBLE)
    {
        batteryMv = mv;
        batteryPct = batteryPercent(mv);
    }
    Serial.printf("[battery] %d mV -> %d%%\n", mv, batteryPct);
}

// Returns true when connected; otherwise fills `detail` with a reason.
static bool connectWifi(char *detail, size_t detailLen)
{
    WiFi.persistent(false); // don't rewrite credentials to flash on every wake
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    const uint32_t t0 = millis();
    wl_status_t st = WiFi.status();
    while (st != WL_CONNECTED && millis() - t0 < WIFI_TIMEOUT_MS)
    {
        delay(100);
        st = WiFi.status();
    }
    if (st == WL_CONNECTED)
    {
        Serial.printf("[wifi] connected in %lu ms, ip %s, rssi %d\n", (unsigned long)(millis() - t0),
                      WiFi.localIP().toString().c_str(), WiFi.RSSI());
        return true;
    }
    if (st == WL_NO_SSID_AVAIL)
        snprintf(detail, detailLen, "Wi-Fi network \"%s\" wasn't found.", WIFI_SSID);
    else if (st == WL_CONNECT_FAILED)
        snprintf(detail, detailLen, "Couldn't join Wi-Fi \"%s\". Check the password.", WIFI_SSID);
    else
        snprintf(detail, detailLen, "Wi-Fi \"%s\" didn't connect within %d seconds.", WIFI_SSID,
                 WIFI_TIMEOUT_MS / 1000);
    Serial.printf("[wifi] failed (status %d)\n", (int)st);
    return false;
}

// Best effort: get the clock from NTP so the error screen can name a retry time.
static void trySyncClockFromNtp()
{
    const char *ntp = NTP_SERVER;
    if (!ntp[0] || timeIsValid() || WiFi.status() != WL_CONNECTED)
        return;
    configTzTime(TZ_POSIX, ntp);
    const uint32_t t0 = millis();
    while (!timeIsValid() && millis() - t0 < 4000)
        delay(100);
    Serial.printf("[ntp] %s\n", timeIsValid() ? "clock set" : "no answer");
}

// ---------------------------------------------------------------------------------------------

void setup()
{
    Serial.begin(115200);
    timeInitZone(TZ_POSIX);
    const char *reason = wakeReason();
    if (strcmp(reason, "boot") == 0)
    {
        rtcRetryCount = 0;
        rtcRetryPending = false;
    }
    rtcWakeCount++;
    Serial.printf("\n[wake] #%lu reason=%s retry=%u clock=%s\n", (unsigned long)rtcWakeCount, reason,
                  rtcRetryCount, timeIsValid() ? "set" : "unknown");

    // Hard upper bound on time awake, whatever happens below.
    esp_timer_handle_t failsafe;
    const esp_timer_create_args_t failsafeArgs = {.callback = failsafeTimeout, .arg = nullptr,
                                                  .dispatch_method = ESP_TIMER_TASK, .name = "failsafe",
                                                  .skip_unhandled_events = true};
    if (esp_timer_create(&failsafeArgs, &failsafe) == ESP_OK)
        esp_timer_start_once(failsafe, FAILSAFE_AWAKE_US);

    display.begin();
    readBattery();

    char detail[128] = "";
    FetchResult r;
    if (connectWifi(detail, sizeof(detail)))
    {
        fetchDisplay(SERVER_HOST, SERVER_PORT, SERVER_PATH, batteryPct, batteryMv, reason, HTTP_TIMEOUT_MS, r);
        if (r.serverTime)
            timeSet(r.serverTime);
        if (!r.ok)
        {
            snprintf(detail, sizeof(detail), "%s", r.error);
            trySyncClockFromNtp();
        }
    }
    wifiOff(); // radio off before the slow part (decode + refresh)

    if (r.ok)
    {
        display.clearDisplay();
        // No dithering: the server already quantized to the panel's 8 levels, and the library maps
        // 0,36,73,...,255 exactly onto palette indices 0..7.
        const bool drawn = display.image.drawPngFromBuffer(r.png, (int32_t)r.pngLen, 0, 0, false, false);
        fetchFree(r);
        if (drawn)
        {
            display.display(); // full refresh
            rtcRetryCount = 0;
            rtcRetryPending = false;
            const uint32_t sleepS = r.sleepSeconds > 0 ? (uint32_t)r.sleepSeconds : scheduledSleepSeconds();
            Serial.printf("[draw] ok; X-Sleep-Seconds %ld\n", r.sleepSeconds);
            esp_timer_stop(failsafe);
            deepSleep(sleepS);
        }
        snprintf(detail, sizeof(detail), "The image from the server didn't decode.");
    }

    Serial.printf("[error] %s\n", detail);
    const uint32_t sleepS = noteFailure();
    drawErrorScreen(detail, sleepS);
    esp_timer_stop(failsafe);
    deepSleep(sleepS);
}

void loop()
{
    // Never reached: setup() always ends in deep sleep.
}
