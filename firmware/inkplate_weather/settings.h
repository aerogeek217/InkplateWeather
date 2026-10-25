// Wi-Fi + server settings. Stored in NVS (ESP32 Preferences); the values compiled in from config.h are the
// defaults that apply while NVS holds nothing, so a device flashed with a filled-in config.h keeps working
// and the captive portal only has to be used to change them.
#pragma once
#include <stddef.h>
#include <stdint.h>

struct Settings
{
    char ssid[33] = "";
    char password[65] = "";
    char host[64] = "";
    uint16_t port = 0;
    bool ssidFromNvs = false; // true when the saved (portal) SSID is in use
    bool serverFromNvs = false;
};

// NVS values where present, else config.h. An SSID that is empty or still the example placeholder counts as unset.
void settingsLoad(Settings &s);
bool settingsHaveSsid(const Settings &s);

// Writes the portal's values to NVS. Returns false if NVS can't be opened.
bool settingsSave(const char *ssid, const char *password, const char *host, uint16_t port);
// Forget everything saved in NVS (back to the config.h defaults). Keeps the OTA bookkeeping.
void settingsClear();

// Parses "host", "host:port" or "http://host:port/" (the port defaults to defaultPort). Host characters are
// limited to letters, digits, '.', '-' and '_'. Returns false when it isn't usable.
bool settingsParseServer(const char *text, uint16_t defaultPort, char *host, size_t hostLen, uint16_t *port);

// Firmware-update bookkeeping: epoch seconds of the last update attempt that didn't finish (0 = none).
uint32_t settingsOtaLastTry();
void settingsSetOtaLastTry(uint32_t epoch);
