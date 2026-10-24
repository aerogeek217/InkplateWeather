// Inkplate weather display: device configuration.
//
// Copy this file to config.h in the same folder and fill in your values:
//     copy config.example.h config.h        (Windows)
//     cp config.example.h config.h          (macOS/Linux)
// config.h is gitignored, so your Wi-Fi password never ends up in git.
#pragma once

// ---- Wi-Fi (2.4 GHz only; the ESP32 can't join 5 GHz networks) ----
#define WIFI_SSID     "your-wifi-name"
#define WIFI_PASSWORD "your-wifi-password"

// Give up joining Wi-Fi after this long (milliseconds) and show the error screen.
#define WIFI_TIMEOUT_MS 15000

// ---- Weather server (the container on the Pi) ----
// Host is an IP address or a name your network resolves (e.g. "omv.local" may not resolve on ESP32; prefer the IP).
#define SERVER_HOST "192.168.1.20"
#define SERVER_PORT 8080
#define SERVER_PATH "/display.png"

// Whole request budget: connect + render + download (milliseconds). The contract says 20 s.
#define HTTP_TIMEOUT_MS 20000

// ---- Time ----
// POSIX TZ string for the display timezone. America/Chicago:
#define TZ_POSIX "CST6CDT,M3.2.0,M11.1.0"

// Optional NTP server, used only when the weather server is unreachable and the clock is
// unknown (e.g. right after power-on), so the error screen can say "Will retry at 3:35 PM".
// The normal clock source is the server's HTTP Date header. Set to "" to disable NTP.
#define NTP_SERVER "pool.ntp.org"

// ---- Schedule ----
// Normal refresh period in minutes. Wakes are aligned to the clock (30 -> :00 and :30).
// Only used when the server doesn't send a valid X-Sleep-Seconds header.
#define REFRESH_MINUTES 30

// Seconds to wait past the :00/:30 boundary so the server sees the new half hour.
#define ALIGN_OFFSET_SECONDS 20

// After a failure: retry this many times, this many seconds apart, then return to the normal schedule.
#define RETRY_COUNT         3
#define RETRY_DELAY_SECONDS 300
