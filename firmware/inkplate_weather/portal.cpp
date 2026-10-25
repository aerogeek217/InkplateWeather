#include "portal.h"

#include <Arduino.h>
#include <DNSServer.h>
#include <WebServer.h>
#include <WiFi.h>
#include <esp_mac.h>
#include <esp_system.h>

#include <vector>

#include "config.h"

void portalMakeCredentials(char *apName, size_t apNameLen, char *apPassword, size_t apPasswordLen)
{
    uint8_t mac[6] = {0};
    esp_read_mac(mac, ESP_MAC_WIFI_STA);
    snprintf(apName, apNameLen, "Inkplate-Weather-%02X%02X", mac[4], mac[5]);
    snprintf(apPassword, apPasswordLen, "%08lu", (unsigned long)(esp_random() % 100000000UL)); // WPA2 needs >= 8
}

// ---------------------------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------------------------

static String htmlEscape(const String &s)
{
    String o;
    o.reserve(s.length() + 8);
    for (size_t i = 0; i < s.length(); ++i)
    {
        switch (s[i])
        {
        case '&': o += "&amp;"; break;
        case '<': o += "&lt;"; break;
        case '>': o += "&gt;"; break;
        case '"': o += "&quot;"; break;
        case '\'': o += "&#39;"; break;
        default: o += s[i];
        }
    }
    return o;
}

static const char PAGE_HEAD[] =
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>Inkplate weather setup</title>"
    "<style>body{font:20px/1.4 system-ui,sans-serif;max-width:32rem;margin:1rem auto;padding:0 1rem}"
    "h1{font-size:1.5rem}label{display:block;margin-top:1rem;font-weight:600}"
    "input,select,button{font:inherit;width:100%;box-sizing:border-box;padding:.5rem;margin-top:.25rem}"
    "button{margin-top:1.5rem;background:#111;color:#fff;border:0;border-radius:.4rem}"
    "button.alt{background:#eee;color:#111}.err{background:#fde;border:1px solid #c00;padding:.5rem;margin-top:1rem}"
    ".hint{color:#555;font-size:.85rem;font-weight:400}.chk{display:flex;gap:.5rem;align-items:center;font-weight:400}"
    ".chk input{width:auto;margin:0}</style></head><body>";

static String formPage(const Settings &cur, const std::vector<String> &networks, const String &error)
{
    String p = PAGE_HEAD;
    p += "<h1>Inkplate weather setup</h1>";
    if (error.length())
        p += "<div class=\"err\">" + htmlEscape(error) + "</div>";
    p += "<form method=\"post\" action=\"/save\">";

    p += "<label for=\"pick\">Wi-Fi network (2.4 GHz)</label><select id=\"pick\" name=\"pick\">";
    p += "<option value=\"\">Other (type it below)</option>";
    for (const String &n : networks)
    {
        const bool sel = strcmp(n.c_str(), cur.ssid) == 0;
        p += "<option value=\"" + htmlEscape(n) + "\"" + (sel ? " selected" : "") + ">" + htmlEscape(n) + "</option>";
    }
    p += "</select>";
    p += "<label for=\"ssid\">Or type the network name<span class=\"hint\"> (overrides the list)</span></label>"
         "<input id=\"ssid\" name=\"ssid\" maxlength=\"32\" autocapitalize=\"none\" autocomplete=\"off\">";
    p += "<label for=\"pass\">Wi-Fi password";
    if (cur.ssid[0])
        p += "<span class=\"hint\"> (leave blank to keep the saved one)</span>";
    p += "</label><input id=\"pass\" name=\"pass\" type=\"password\" maxlength=\"63\" autocomplete=\"off\">"
         "<label class=\"chk\"><input type=\"checkbox\" onclick=\"document.getElementById('pass').type=this.checked?'text':'password'\">"
         "Show password</label>";

    String server = cur.host;
    server += ":" + String((unsigned)cur.port);
    p += "<label for=\"server\">Weather server<span class=\"hint\"> (address of the Pi, like 192.168.1.20:8080)</span></label>"
         "<input id=\"server\" name=\"server\" maxlength=\"70\" autocapitalize=\"none\" autocomplete=\"off\" value=\"" +
         htmlEscape(server) + "\">";
    p += "<button type=\"submit\">Save and restart</button></form>";
    p += "<form method=\"get\" action=\"/scan\"><button class=\"alt\" type=\"submit\">Scan for networks again</button></form>";
    p += "<form method=\"post\" action=\"/forget\"><button class=\"alt\" type=\"submit\">Forget saved settings "
         "(use the built-in defaults)</button></form>";
    p += "</body></html>";
    return p;
}

static String messagePage(const String &title, const String &body)
{
    return String(PAGE_HEAD) + "<h1>" + htmlEscape(title) + "</h1><p>" + htmlEscape(body) + "</p></body></html>";
}

// ---------------------------------------------------------------------------------------------

static void scanNetworks(std::vector<String> &out)
{
    out.clear();
    const int n = WiFi.scanNetworks(false, false); // blocking, strongest first
    for (int i = 0; i < n && out.size() < 20; ++i)
    {
        String s = WiFi.SSID(i);
        if (!s.length() || s.length() > 32)
            continue;
        bool dup = false;
        for (const String &o : out)
            dup = dup || o == s;
        if (!dup)
            out.push_back(s);
    }
    WiFi.scanDelete();
    Serial.printf("[portal] scan found %u networks\n", (unsigned)out.size());
}

PortalResult portalRun(const char *apName, const char *apPassword, const Settings &current, uint32_t timeoutMs)
{
    WiFi.persistent(false);
    WiFi.mode(WIFI_AP_STA); // STA side is needed for scanning
    WiFi.softAP(apName, apPassword);
    delay(200);
    const IPAddress ip = WiFi.softAPIP();
    Serial.printf("[portal] AP %s up at %s\n", apName, ip.toString().c_str());

    std::vector<String> networks;
    scanNetworks(networks);

    DNSServer dns;
    dns.setErrorReplyCode(DNSReplyCode::NoError);
    dns.start(53, "*", ip);

    WebServer server(80);
    bool saved = false;
    bool rescan = false;

    server.on("/", HTTP_GET, [&]() { server.send(200, "text/html", formPage(current, networks, "")); });
    server.on("/scan", HTTP_GET, [&]() {
        rescan = true; // done in the main loop so this handler returns first
        server.sendHeader("Location", "/");
        server.send(303, "text/plain", "");
    });
    server.on("/save", HTTP_POST, [&]() {
        String ssid = server.arg("ssid");
        ssid.trim();
        if (!ssid.length())
            ssid = server.arg("pick");
        String pass = server.arg("pass");
        String srv = server.arg("server");
        char host[64];
        uint16_t port;
        String error;
        if (!ssid.length())
            error = "Choose or type the Wi-Fi network name.";
        else if (ssid.length() > 32 || pass.length() > 63)
            error = "The network name is at most 32 characters and the password at most 63.";
        else if (pass.length() && pass.length() < 8)
            error = "A Wi-Fi password is at least 8 characters (leave it empty for an open network).";
        else if (!settingsParseServer(srv.c_str(), SERVER_PORT, host, sizeof(host), &port))
            error = "The server address should look like 192.168.1.20:8080.";
        if (error.length())
        {
            server.send(200, "text/html", formPage(current, networks, error));
            return;
        }
        // Blank password with the same network = keep the saved one.
        if (!pass.length() && strcmp(ssid.c_str(), current.ssid) == 0)
            pass = current.password;
        if (!settingsSave(ssid.c_str(), pass.c_str(), host, port))
        {
            server.send(200, "text/html", formPage(current, networks, "Couldn't save to the device's memory. Try again."));
            return;
        }
        Serial.printf("[portal] saved ssid '%s', server %s:%u\n", ssid.c_str(), host, (unsigned)port);
        server.send(200, "text/html",
                    messagePage("Saved", "The display is restarting and will join \"" + ssid +
                                             "\" now. You can close this page and reconnect your phone to your usual Wi-Fi."));
        saved = true;
    });
    server.on("/forget", HTTP_POST, [&]() {
        settingsClear();
        Serial.println("[portal] saved settings cleared");
        server.send(200, "text/html",
                    messagePage("Settings cleared", "The display is restarting with the settings built into its firmware."));
        saved = true;
    });
    // Phones probe fixed URLs (generate_204, hotspot-detect.html, ...) to find a captive portal: send them all to the form.
    server.onNotFound([&]() {
        server.sendHeader("Location", String("http://") + ip.toString() + "/");
        server.send(302, "text/plain", "");
    });
    server.begin();

    const uint32_t t0 = millis();
    while (!saved && millis() - t0 < timeoutMs)
    {
        dns.processNextRequest();
        server.handleClient();
        if (rescan)
        {
            rescan = false;
            scanNetworks(networks);
        }
        delay(2);
    }
    if (saved) // let the confirmation page reach the phone before the radio goes away
    {
        const uint32_t t1 = millis();
        while (millis() - t1 < 1500)
        {
            server.handleClient();
            delay(2);
        }
    }
    server.stop();
    dns.stop();
    WiFi.softAPdisconnect(true);
    WiFi.mode(WIFI_OFF);
    return saved ? PORTAL_SAVED : PORTAL_TIMEOUT;
}
