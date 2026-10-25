// Captive-portal setup: a SoftAP with a catch-all DNS and a one-page form for the Wi-Fi name, password and
// server address. Uses only libraries that ship with the ESP32 core (WiFi, WebServer, DNSServer, Preferences).
#pragma once
#include <stddef.h>
#include <stdint.h>

#include "settings.h"

// Network name "Inkplate-Weather-XXXX" (last two bytes of the MAC) and a fresh 8-digit WPA2 password.
// The e-paper prints both, so a stranger who can't see the screen can't join.
void portalMakeCredentials(char *apName, size_t apNameLen, char *apPassword, size_t apPasswordLen);

enum PortalResult
{
    PORTAL_SAVED,   // settings were written to NVS; reboot to use them
    PORTAL_TIMEOUT, // nobody finished within timeoutMs
};

// Blocks while the portal runs. The caller has already put the instructions on the e-paper.
PortalResult portalRun(const char *apName, const char *apPassword, const Settings &current, uint32_t timeoutMs);
