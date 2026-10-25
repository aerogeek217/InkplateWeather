#include "fetch.h"

#include <Arduino.h>
#include <HTTPClient.h>
#include <WiFi.h>

#include "timekeeping.h"
#include "version.h"

// Contract says 60-150 kB; anything far beyond that is not our PNG.
static const size_t MAX_PNG_BYTES = 1024 * 1024;
static const uint32_t CONNECT_TIMEOUT_MS = 5000;

static const uint8_t PNG_SIGNATURE[8] = {0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n'};
// Last 12 bytes of every complete PNG: zero-length IEND chunk + its CRC.
static const uint8_t PNG_IEND[12] = {0, 0, 0, 0, 'I', 'E', 'N', 'D', 0xAE, 0x42, 0x60, 0x82};

static uint32_t readBE32(const uint8_t *p)
{
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

static void fail(FetchResult &r, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(r.error, sizeof(r.error), fmt, ap);
    va_end(ap);
    r.ok = false;
    fetchFree(r);
}

void fetchFree(FetchResult &r)
{
    if (r.png)
        free(r.png);
    r.png = nullptr;
    r.pngLen = 0;
}

void fetchDisplay(const char *host, uint16_t port, const char *path, int batteryPct, int batteryMv,
                  const char *reason, uint32_t timeoutMs, FetchResult &r)
{
    r = FetchResult();
    const uint32_t start = millis();

    String uri = path;
    uri += "?";
    if (batteryPct >= 0 && batteryMv >= 0)
    {
        uri += "batt=" + String(batteryPct) + "&mv=" + String(batteryMv) + "&";
    }
    uri += "reason=";
    uri += reason;
    uri += "&fw=" + String(FW_VERSION);
    Serial.printf("[fetch] GET http://%s:%u%s\n", host, port, uri.c_str());

    WiFiClient client;
    HTTPClient http;
    http.useHTTP10(true); // no chunked transfer encoding; body ends at Content-Length or connection close
    http.setReuse(false);
    http.setConnectTimeout(CONNECT_TIMEOUT_MS);
    http.setTimeout((uint16_t)(timeoutMs > CONNECT_TIMEOUT_MS + 1000 ? timeoutMs - CONNECT_TIMEOUT_MS : 1000));
    const char *headerKeys[] = {"X-Sleep-Seconds", "Date", "Content-Type", "X-Firmware-Update"};
    http.collectHeaders(headerKeys, sizeof(headerKeys) / sizeof(headerKeys[0]));

    if (!http.begin(client, host, port, uri))
    {
        fail(r, "Couldn't start the request.");
        return;
    }

    r.httpCode = http.GET();
    if (r.httpCode <= 0)
    {
        fail(r, "No answer from the server (%s).", HTTPClient::errorToString(r.httpCode).c_str());
        http.end();
        return;
    }
    if (millis() - start > timeoutMs)
    {
        fail(r, "No answer within %lu seconds.", (unsigned long)(timeoutMs / 1000));
        http.end();
        return;
    }

    // Headers are useful even on errors (Date sets the clock), so read them first.
    String dateHeader = http.header("Date");
    time_t serverTime;
    if (dateHeader.length() && timeParseHttpDate(dateHeader.c_str(), &serverTime))
        r.serverTime = serverTime;
    String sleepHeader = http.header("X-Sleep-Seconds");
    if (sleepHeader.length())
    {
        long s = sleepHeader.toInt();
        if (s >= 60 && s <= 3600)
            r.sleepSeconds = s;
        else
            Serial.printf("[fetch] ignoring out-of-range X-Sleep-Seconds: '%s'\n", sleepHeader.c_str());
    }

    String fwHeader = http.header("X-Firmware-Update");
    if (fwHeader.length())
    {
        long v = fwHeader.toInt();
        if (v > 0 && v < 1000000)
            r.fwUpdate = v;
    }

    if (r.httpCode != HTTP_CODE_OK)
    {
        fail(r, "The server answered with error %d.", r.httpCode);
        http.end();
        return;
    }

    const int declared = http.getSize(); // -1 when no Content-Length
    if (declared == 0 || declared > (int)MAX_PNG_BYTES)
    {
        fail(r, "The server sent an image of unexpected size (%d bytes).", declared);
        http.end();
        return;
    }
    const size_t cap = declared > 0 ? (size_t)declared : MAX_PNG_BYTES;
    r.png = (uint8_t *)ps_malloc(cap);
    if (!r.png)
        r.png = (uint8_t *)malloc(cap);
    if (!r.png)
    {
        fail(r, "Out of memory for the image.");
        http.end();
        return;
    }

    NetworkClient *stream = http.getStreamPtr();
    size_t got = 0;
    bool timedOut = false;
    while (got < cap)
    {
        if (millis() - start > timeoutMs)
        {
            timedOut = true;
            break;
        }
        int avail = stream->available();
        if (avail > 0)
        {
            size_t want = min((size_t)avail, cap - got);
            int n = stream->read(r.png + got, want);
            if (n > 0)
                got += n;
        }
        else if (!stream->connected())
        {
            break;
        }
        else
        {
            delay(2);
        }
    }
    http.end();
    r.pngLen = got;
    Serial.printf("[fetch] HTTP %d, %u bytes in %lu ms\n", r.httpCode, (unsigned)got,
                  (unsigned long)(millis() - start));

    if (timedOut)
    {
        fail(r, "The image didn't finish downloading within %lu seconds.", (unsigned long)(timeoutMs / 1000));
        return;
    }
    if (declared > 0 && got != (size_t)declared)
    {
        fail(r, "The image download was cut off (%u of %d bytes).", (unsigned)got, declared);
        return;
    }
    if (got < 33 || memcmp(r.png, PNG_SIGNATURE, 8) != 0 || memcmp(r.png + 12, "IHDR", 4) != 0)
    {
        fail(r, "The server's reply isn't a PNG image.");
        return;
    }
    if (memcmp(r.png + got - 12, PNG_IEND, 12) != 0)
    {
        fail(r, "The image download was incomplete.");
        return;
    }
    const uint32_t w = readBE32(r.png + 16), h = readBE32(r.png + 20);
    if (w != (uint32_t)DISPLAY_W || h != (uint32_t)DISPLAY_H)
    {
        fail(r, "The image is %lux%lu, expected %dx%d.", (unsigned long)w, (unsigned long)h, DISPLAY_W, DISPLAY_H);
        return;
    }
    r.ok = true;
}
