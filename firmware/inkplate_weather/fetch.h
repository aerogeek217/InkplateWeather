// Download /display.png into PSRAM and check it against the frozen contract (docs/ARCHITECTURE.md).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <time.h>

static const int DISPLAY_W = 1200;
static const int DISPLAY_H = 825;

struct FetchResult
{
    bool ok = false;        // true: `png` holds a complete 1200x825 PNG
    int httpCode = 0;       // HTTP status, or a negative HTTPClient error
    uint8_t *png = nullptr; // heap (PSRAM) buffer; free with fetchFree()
    size_t pngLen = 0;
    long sleepSeconds = -1; // X-Sleep-Seconds if present and within 60..3600, else -1
    time_t serverTime = 0;  // parsed HTTP Date header, 0 if absent
    long fwUpdate = -1;     // X-Firmware-Update (a newer build number the server holds), else -1
    char error[96] = "";    // short plain-language reason when !ok
};

// GET http://host:port/path?batt=..&mv=..&reason=..&fw=<FW_VERSION> with an overall deadline of timeoutMs.
// batteryPct / batteryMv < 0 are omitted from the query.
void fetchDisplay(const char *host, uint16_t port, const char *path, int batteryPct, int batteryMv,
                  const char *reason, uint32_t timeoutMs, FetchResult &r);

void fetchFree(FetchResult &r);
