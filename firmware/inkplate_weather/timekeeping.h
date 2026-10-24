// Wall-clock helpers. The ESP32 system clock keeps running through deep sleep (RTC timer), so once it
// has been set from the server's HTTP Date header it stays roughly right between wakes. It is lost on
// power-on/reset until the next successful request (or NTP, see config.h).
#pragma once
#include <stddef.h>
#include <stdint.h>
#include <time.h>

// Apply the POSIX TZ string (must be called on every wake; the env is not kept in deep sleep).
void timeInitZone(const char *posixTz);

// True once the clock has been set to something after 2024-01-01.
bool timeIsValid();

// Set the system clock (UTC epoch seconds).
void timeSet(time_t utc);

// Parse an RFC 7231 IMF-fixdate ("Sat, 24 Oct 2026 15:21:03 GMT"). Returns false if malformed.
bool timeParseHttpDate(const char *s, time_t *out);

// Seconds from now until the next multiple of `periodMinutes` within the local hour, plus `offsetSeconds`.
// Never returns less than 60 (rolls to the following slot). Requires a valid clock.
uint32_t timeSecondsToNextSlot(int periodMinutes, int offsetSeconds);

// Local wall-clock time like "3:30 PM" (no leading zero), same style as the server's messages.clock().
void timeFormatClock(time_t utc, char *out, size_t outLen);
