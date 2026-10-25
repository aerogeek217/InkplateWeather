#include "settings.h"

#include <Arduino.h>
#include <Preferences.h>

#include "config.h"

static const char NS[] = "inkplate";
static const char PLACEHOLDER_SSID[] = "your-wifi-name"; // from config.example.h

void settingsLoad(Settings &s)
{
    s = Settings();
    snprintf(s.ssid, sizeof(s.ssid), "%s", WIFI_SSID);
    snprintf(s.password, sizeof(s.password), "%s", WIFI_PASSWORD);
    snprintf(s.host, sizeof(s.host), "%s", SERVER_HOST);
    s.port = SERVER_PORT;
    if (strcmp(s.ssid, PLACEHOLDER_SSID) == 0)
        s.ssid[0] = '\0';

    Preferences p;
    if (!p.begin(NS, true)) // read-only; fails when the namespace doesn't exist yet
        return;
    String ssid = p.getString("ssid", "");
    if (ssid.length())
    {
        snprintf(s.ssid, sizeof(s.ssid), "%s", ssid.c_str());
        snprintf(s.password, sizeof(s.password), "%s", p.getString("pass", "").c_str());
        s.ssidFromNvs = true;
    }
    String host = p.getString("host", "");
    uint16_t port = p.getUShort("port", 0);
    if (host.length() && port)
    {
        snprintf(s.host, sizeof(s.host), "%s", host.c_str());
        s.port = port;
        s.serverFromNvs = true;
    }
    p.end();
}

bool settingsHaveSsid(const Settings &s)
{
    return s.ssid[0] != '\0';
}

bool settingsSave(const char *ssid, const char *password, const char *host, uint16_t port)
{
    Preferences p;
    if (!p.begin(NS, false))
        return false;
    p.putString("ssid", ssid);
    p.putString("pass", password);
    p.putString("host", host);
    p.putUShort("port", port);
    p.end();
    return true;
}

void settingsClear()
{
    Preferences p;
    if (!p.begin(NS, false))
        return;
    p.remove("ssid");
    p.remove("pass");
    p.remove("host");
    p.remove("port");
    p.end();
}

bool settingsParseServer(const char *text, uint16_t defaultPort, char *host, size_t hostLen, uint16_t *port)
{
    String t = text;
    t.trim();
    if (t.startsWith("http://"))
        t.remove(0, 7);
    int slash = t.indexOf('/');
    if (slash >= 0)
        t.remove(slash);
    uint32_t prt = defaultPort;
    int colon = t.lastIndexOf(':');
    if (colon >= 0)
    {
        String ps = t.substring(colon + 1);
        t.remove(colon);
        if (ps.length() == 0 || ps.length() > 5)
            return false;
        for (size_t i = 0; i < ps.length(); ++i)
            if (!isDigit(ps[i]))
                return false;
        prt = (uint32_t)ps.toInt();
    }
    if (t.length() == 0 || t.length() >= hostLen || prt == 0 || prt > 65535)
        return false;
    for (size_t i = 0; i < t.length(); ++i)
    {
        const char c = t[i];
        if (!(isAlphaNumeric(c) || c == '.' || c == '-' || c == '_'))
            return false;
    }
    snprintf(host, hostLen, "%s", t.c_str());
    *port = (uint16_t)prt;
    return true;
}

uint32_t settingsOtaLastTry()
{
    Preferences p;
    if (!p.begin(NS, true))
        return 0;
    const uint32_t v = p.getUInt("ota_try", 0);
    p.end();
    return v;
}

void settingsSetOtaLastTry(uint32_t epoch)
{
    Preferences p;
    if (!p.begin(NS, false))
        return;
    p.putUInt("ota_try", epoch);
    p.end();
}
