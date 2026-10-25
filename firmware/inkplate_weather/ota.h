// Pull-style over-the-air updates. The device sleeps almost all the time, so it can't wait for a push: the server
// announces a newer build in the X-Firmware-Update header of /display.png and the device downloads
// /firmware/inkplate_weather-<n>.bin with the core's HTTPUpdate (which checks the x-MD5 header).
#pragma once
#include <stddef.h>
#include <stdint.h>

// Don't start an update below this charge (unknown charge, i.e. no battery reading, is treated as USB power).
static const int OTA_MIN_BATTERY_PCT = 40;
// After an attempt that didn't finish, wait this long before trying again.
static const uint32_t OTA_RETRY_HOURS = 6;

// Decides whether to install `announced` now. Looks at the version, the battery and the failed-attempt timestamp
// in NVS (needs a valid clock). Logs the reason when it says no.
bool otaShouldTry(long announced, int batteryPct);

// Downloads and installs the build. Call with Wi-Fi connected. Records the attempt in NVS first (so a crash
// or power cut counts against the 6 h limit) and clears the record on success. Returns true when the new
// image is written; the caller restarts. On false, `err` holds a short reason.
bool otaInstall(const char *host, uint16_t port, long version, char *err, size_t errLen);
