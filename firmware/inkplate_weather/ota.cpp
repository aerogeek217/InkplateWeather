#include "ota.h"

#include <Arduino.h>
#include <HTTPUpdate.h>
#include <WiFi.h>

#include "settings.h"
#include "timekeeping.h"
#include "version.h"

bool otaShouldTry(long announced, int batteryPct)
{
    if (announced <= FW_VERSION)
        return false;
    if (batteryPct >= 0 && batteryPct < OTA_MIN_BATTERY_PCT)
    {
        Serial.printf("[ota] version %ld is available but the battery is at %d%% (needs %d%%)\n", announced, batteryPct,
                      OTA_MIN_BATTERY_PCT);
        return false;
    }
    const uint32_t last = settingsOtaLastTry();
    if (last != 0)
    {
        // A previous attempt didn't finish. Without a clock we can't tell how long ago, so stay out of trouble.
        if (!timeIsValid())
        {
            Serial.println("[ota] an earlier attempt failed and the clock isn't set; not retrying yet");
            return false;
        }
        const uint32_t now = (uint32_t)time(nullptr);
        if (now >= last && now - last < OTA_RETRY_HOURS * 3600UL)
        {
            Serial.printf("[ota] last attempt %lu min ago; waiting for %u h\n", (unsigned long)((now - last) / 60),
                          (unsigned)OTA_RETRY_HOURS);
            return false;
        }
    }
    return true;
}

bool otaInstall(const char *host, uint16_t port, long version, char *err, size_t errLen)
{
    // Remember the attempt before starting. With no valid clock the value is 1 ("tried, time unknown").
    settingsSetOtaLastTry(timeIsValid() ? (uint32_t)time(nullptr) : 1);

    char uri[64];
    snprintf(uri, sizeof(uri), "/firmware/inkplate_weather-%ld.bin", version);
    Serial.printf("[ota] installing version %ld from http://%s:%u%s\n", version, host, (unsigned)port, uri);

    WiFiClient client;
    httpUpdate.rebootOnUpdate(false);
    httpUpdate.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
    char current[16];
    snprintf(current, sizeof(current), "%d", FW_VERSION);
    const uint32_t t0 = millis();
    const t_httpUpdate_return ret = httpUpdate.update(client, host, port, uri, current);
    switch (ret)
    {
    case HTTP_UPDATE_OK:
        Serial.printf("[ota] done in %lu ms\n", (unsigned long)(millis() - t0));
        settingsSetOtaLastTry(0);
        return true;
    case HTTP_UPDATE_NO_UPDATES:
        snprintf(err, errLen, "The server had no update to send.");
        break;
    default:
        snprintf(err, errLen, "%s", httpUpdate.getLastErrorString().c_str());
        break;
    }
    Serial.printf("[ota] failed: %s\n", err);
    return false;
}
