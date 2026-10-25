# Inkplate 10 firmware

This firmware runs the Inkplate 10 as a thin client for the weather server. The server renders the whole screen.
The Inkplate wakes, downloads one PNG, shows it, and goes back to sleep.

Two optional features sit on top: a [Wi-Fi setup portal](#wi-fi-setup-portal) (no re-flashing to change the network
or the server address) and [over-the-air updates](#over-the-air-updates) pulled from the weather server.

## What it does

On every wake (from the timer, the wake button, or power-on):

0. **Checks the wake button.** If it is held for 5 s the [setup portal](#wi-fi-setup-portal) opens instead of the steps below.
   With no Wi-Fi network configured at all, a power-on or button wake opens it too.
1. **Reads the battery** before Wi-Fi starts, because radio current makes the voltage sag. See [Battery reporting](#battery-reporting).
2. **Joins Wi-Fi.** It gives up after 15 s (`WIFI_TIMEOUT_MS`).
3. **Requests the screen** with `GET http://<server host>:<port>/display.png?batt=<pct>&mv=<millivolts>&reason=<timer|button|retry|boot>&fw=<build>`.
   The host and port are the saved setup values, else `SERVER_HOST`/`SERVER_PORT` from `config.h`.
   This follows the frozen contract in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md#displaypng-contract-frozen-2026-10-24-firmware-builds-against-this).
   The whole request has a 20 s budget covering connect, render and download.
4. **Checks the reply.** It must be HTTP 200 and a complete PNG of exactly 1200 × 825.
5. **Turns Wi-Fi off**, decodes the PNG into the 3-bit (8-gray) framebuffer and does a **full refresh**.
   Dithering is off: the server already uses only the panel's 8 levels (0, 36, 73 … 255), and the
   library maps each of those exactly onto one of the panel's 8 grays.
6. **Updates itself if told to.** When the reply carried `X-Firmware-Update: <n>` with `n` newer than this build and the
   battery is at least 40 %, it installs that build now (after the picture is on screen). See
   [Over-the-air updates](#over-the-air-updates).
7. **Deep-sleeps.** The sleep length comes from the server's `X-Sleep-Seconds` header (the next :00 or :30 plus about
   20 s). If the header is missing or outside 60–3600, the device works out the next :00/:30 from its own clock.
   If the clock isn't set either, it sleeps 1800 s.

The HTTP `Date` header sets the ESP32 clock on every successful request. The clock keeps running through deep
sleep, and it is used for the fallback alignment and for the time on the error screen.

### When something fails

Any of these counts as a failure: Wi-Fi doesn't connect, a non-200 status, the 20 s timeout, a truncated body,
something that isn't a PNG, the wrong size, or an image that doesn't decode. On a failure the device draws a
local error screen:

> **Can't reach the weather server at 192.168.1.20:8080.**
> **Will retry at 3:35 PM.**
> *Wi-Fi "MyNetwork" didn't connect within 15 seconds.*
> *Press the wake button on the side to try again now.*
> Battery 78% (4.02 V)

After 3 failed retry cycles in a row with Wi-Fi as the cause (about 2 hours without Wi-Fi), a line is added:
*Wi-Fi has been failing for a while. To change the Wi-Fi settings, hold the wake button for 5 seconds.*
The radio is not kept on: the device keeps sleeping on the normal schedule.

- The first two sentences use the same wording as `SERVER_UNREACHABLE` in `server/app/messages.py`.
- The headline is drawn at double size. If the host name is too long, it drops back to normal size.
- If the clock isn't known yet (right after power-on), the device first tries NTP (`NTP_SERVER`). If that fails
  too, the second line reads "Will retry in 5 minutes."
- The device retries every 5 minutes, up to 3 times (`RETRY_COUNT`, `RETRY_DELAY_SECONDS`). Those wakes send
  `reason=retry`. After the third failed retry it goes back to the normal :00/:30 schedule, and the retry
  count starts over.
- A failsafe timer forces deep sleep if the device is still awake after 120 s, so a hang can't drain the battery.
  It counts as a failure.

### Wake button

The Inkplate 10's **WAKE** button is wired to **GPIO 36**, active low. Before every deep sleep the firmware
enables it as an `ext0` wake source, the same way the official
`Inkplate10_Wake_Up_Button` example does it. Pressing it wakes the device straight away and requests a fresh
screen with `reason=button`. **Holding it for about 5 seconds** opens the [setup portal](#wi-fi-setup-portal) instead. If the button is still held when the device is about to sleep, it waits (up to
10 s) for you to let go, so it doesn't wake itself again immediately.

`reason=boot` means a power-on or reset, including the reset that flashing or opening the serial port causes.

### Battery reporting

- **Voltage.** `display.readBattery()` (InkplateLibrary) switches on the battery divider MOSFET and reads GPIO 35
  with `analogReadMilliVolts`, then multiplies by 2. The firmware averages 4 reads taken before Wi-Fi starts.
- **Percentage.** A piecewise-linear table maps voltage to charge. It is a typical resting discharge curve
  for a single LiPo cell (see `battery.cpp`):

  | mV | 4200 | 4150 | 4110 | 4080 | 4020 | 3980 | 3950 | 3910 | 3870 | 3850 | 3840 |
  |---|---|---|---|---|---|---|---|---|---|---|---|
  | % | 100 | 95 | 90 | 85 | 80 | 75 | 70 | 65 | 60 | 55 | 50 |

  | mV | 3820 | 3800 | 3790 | 3770 | 3750 | 3730 | 3710 | 3690 | 3610 | 3270 |
  |---|---|---|---|---|---|---|---|---|---|---|
  | % | 45 | 40 | 35 | 30 | 25 | 20 | 15 | 10 | 5 | 0 |

  Values in between are interpolated. Anything above 4200 mV reads 100 % and anything below 3270 mV reads 0 %.
  The curve is flat in the middle, so treat 30–70 % as roughly ±10 %. With USB plugged in, the charger holds the
  cell near 4.2 V and it reads close to 100 %.
- **No battery.** If the reading falls outside 2500–4600 mV, `batt` and `mv` are left out of the request.
  Both parameters are optional in the contract.

## Configure

```powershell
copy firmware\inkplate_weather\config.example.h firmware\inkplate_weather\config.h
notepad firmware\inkplate_weather\config.h
```

Set `WIFI_SSID` and `WIFI_PASSWORD`; the network must be 2.4 GHz. Then set `SERVER_HOST`, an IP address of the Pi,
and `SERVER_PORT` (8080). The defaults for `TZ_POSIX` (America/Chicago), `REFRESH_MINUTES` (30), the retry
settings and the timeouts match the architecture. `config.h` is gitignored, so never commit it.

These Wi-Fi and server values are only the **defaults**. Values saved through the [setup portal](#wi-fi-setup-portal)
are kept in the device's NVS (non-volatile storage) and win over `config.h`; they survive deep sleep, power loss
and re-flashing the app. Leave `WIFI_SSID` as `"your-wifi-name"` (or empty) to ship a build with no network, which
then starts the portal on first power-on.

`version.h` holds `FW_VERSION`, the build number used by [OTA updates](#over-the-air-updates).

### Which board: Soldered or e-radionica?

This project's display is the **Soldered** Inkplate 10, so the default (`Inkplate10V2`) is correct.

There are two Inkplate 10 hardware versions, and they use different board definitions:

| Board | Arduino board name | FQBN | `build.ps1 -Board` |
|---|---|---|---|
| Soldered Inkplate 10 (current, sold by soldered.com since 2022) | Soldered Inkplate 10 | `soldered-inkplate-boards:esp32:Inkplate10V2` | `soldered` (default) |
| e-radionica Inkplate 10 (original Crowd Supply version, "e-radionica.com" on the PCB) | e-radionica Inkplate 10 | `soldered-inkplate-boards:esp32:Inkplate10` | `eradionica` |

Both compile cleanly. If you pick the wrong one, the screen never changes, because the I/O expander
address differs.

## Build (Arduino CLI, project-local)

Every tool goes under `<repo>\.tools\`, which is gitignored. Nothing is installed system-wide.

### Versions used

| Component | Version |
|---|---|
| arduino-cli | 1.5.1 (Windows x64 release zip, SHA-256 `fabe42e0…853dd40`) |
| Board package `soldered-inkplate-boards:esp32` | 4.0.0 (Arduino-ESP32 core 3.3.11) |
| Board manager URL | `https://github.com/SolderedElectronics/Inkplate-Board-Definitions-for-Arduino-IDE/raw/refs/heads/main/package_Inkplate_Boards_index.json` |
| Library `InkplateLibrary` (Soldered) | 11.2.1 |

Compile result for Inkplate10V2 (partition scheme `no_fs`, see [Partitions](#partitions-and-the-first-usb-flash)):

| Memory | Used | Limit |
|---|---|---|
| Flash | 1,115,608 bytes (54 %) | 2,031,616 (one app slot) |
| Static RAM | 94,536 bytes (28 %) | 327,680 |

Before the setup portal and OTA were added (partition scheme `huge_app`) it was 1,051,724 bytes of flash and
93,944 bytes of RAM, so the two features cost about 64 kB of flash and 0.6 kB of static RAM.

The 3-bit framebuffer and the downloaded PNG live in PSRAM. The sketch itself compiles with no warnings at
`--warnings all`. The core and library produce their own warnings (pin macros redefined, `B00000000`
deprecations); these are harmless.

### With the script (Windows PowerShell)

```powershell
# one time: download arduino-cli, the board package and the library into .tools\
.\firmware\build.ps1 -Setup

# compile (Soldered Inkplate 10)
.\firmware\build.ps1

# compile for the older e-radionica board
.\firmware\build.ps1 -Board eradionica

# compile and flash (replace COM5 with your port)
.\firmware\build.ps1 -Upload -Port COM5

# compile and write firmware\dist\inkplate_weather-<ver>.bin + manifest.json for an over-the-air update
.\firmware\build.ps1 -Export
```

The script writes `.tools\arduino-cli.yaml`. That file keeps the data, downloads, libraries (`user`) and
build cache inside `.tools\`.

### By hand

The repo root is the working directory:

```powershell
$cli = ".\.tools\arduino-cli\arduino-cli.exe"
$cfg = ".\.tools\arduino-cli.yaml"   # written by build.ps1 -Setup

& $cli --config-file $cfg core update-index
& $cli --config-file $cfg core install soldered-inkplate-boards:esp32@4.0.0
& $cli --config-file $cfg lib install "InkplateLibrary@11.2.1"

& $cli --config-file $cfg compile --fqbn soldered-inkplate-boards:esp32:Inkplate10V2:PartitionScheme=no_fs firmware\inkplate_weather
& $cli --config-file $cfg upload  --fqbn soldered-inkplate-boards:esp32:Inkplate10V2:PartitionScheme=no_fs --port COM5 firmware\inkplate_weather
```

To do compile and upload in one step, use `compile --upload --port COM5 …`.

## Build (Arduino IDE 2.x)

1. **File → Preferences → Additional boards manager URLs**: add the board manager URL from the table above.
2. **Tools → Board → Boards Manager**: search for "Soldered Inkplate Boards" and install **4.0.0**.
3. **Tools → Manage Libraries**: search for "InkplateLibrary" and install **11.2.1**.
4. Open `firmware/inkplate_weather/inkplate_weather.ino`. The other `.cpp`/`.h` files open as tabs.
5. **Tools → Board → Inkplate Boards → Soldered Inkplate 10**, or e-radionica Inkplate 10 for the old board.
   Set **Tools → Partition Scheme → No FS 4MB (2MB APP x2)**, which OTA needs (the board's default, Huge APP, has
   no second app slot). Leave the other Tools options at their defaults (PSRAM enabled).
6. **Tools → Port**: pick the Inkplate's COM port. Then click **Upload**.

## Partitions and the first USB flash

OTA needs two app slots: the new image is written to the slot that is *not* running, then the boot pointer
moves. The Soldered board's default partition table (`huge_app`) has a single 3 MB slot and **no OTA**, so
`build.ps1` selects `PartitionScheme=no_fs` ("No FS 4MB (2MB APP x2)"): two 1.94 MB app slots (`app0`, `app1`),
`otadata`, 20 kB of NVS, no filesystem. The firmware uses 1.1 MB, so each slot is about 55 % full.

Switching schemes rewrites the partition table, so **the first flash of this firmware has to go over USB**
(`build.ps1 -Upload` does that and writes the bootloader, partition table and app together). Updates after that
can be over the air. Flashing a build made with a different scheme by OTA is not possible; an OTA image that is
larger than a slot is rejected by the updater and the device keeps running.

## Wi-Fi setup portal

Use it to join a new network or point the display at a different server without a cable.

**It opens when**

1. no Wi-Fi network is configured anywhere (blank/placeholder `WIFI_SSID` and nothing saved) and the device is
   powered on or the wake button is pressed (a timer wake just goes back to sleep for 6 h, because the
   instructions are already on screen), or
2. you **hold the wake button for about 5 seconds** while it is waking (a shorter press is the normal refresh), or
3. Wi-Fi has failed for 3 retry cycles in a row: then the error screen *tells you* to do (2). The portal does
   not start by itself in this case, so a Wi-Fi outage never keeps the radio on and drains the battery.

**What you do**

1. The e-paper switches to a large "Wi-Fi setup" screen (a full refresh, about 10 s) showing the network name
   `Inkplate-Weather-XXXX` (the last two bytes of the MAC address) and an 8-digit **password**. The password is
   new each time the portal starts and is only shown on the screen, so only someone who can see the display can
   join (the access point uses WPA2; the setup form would otherwise send your Wi-Fi password in the clear).
2. Join that network with a phone or laptop. The setup page opens by itself (captive portal); if it doesn't,
   browse to `http://192.168.4.1`.
3. Pick your network from the scanned list (or type its name), enter the password, and enter the weather server as
   `host:port`, for example `192.168.1.20:8080` (the port defaults to 8080 if omitted). Tap **Save and restart**.
   - Leaving the password blank keeps the saved one when the network name is unchanged.
   - **Scan for networks again** rescans. **Forget saved settings** clears NVS so the `config.h` defaults apply.
4. The display restarts, joins the network and fetches the picture.

**Timeout.** If nobody saves within **10 minutes** the portal closes. With a network already configured the
device carries on with a normal wake (and fetches the picture); with none it shows "Wi-Fi setup closed", sleeps
6 h and waits for a button press. Settings are stored with the Arduino `Preferences` library (NVS namespace
`inkplate`: `ssid`, `pass`, `host`, `port`); only libraries that ship with the ESP32 core are used (WiFi,
WebServer, DNSServer, Preferences).

## Over-the-air updates

The ESP32 sleeps almost all the time, so it can't be pushed to; it **pulls**:

1. Every `/display.png` request carries `fw=<FW_VERSION>` (from `version.h`).
2. If the server's `cache/firmware/` folder holds a newer build, the reply adds `X-Firmware-Update: <n>`.
3. After the new picture is drawn, and only if the battery is at least 40 % (or no battery is detected, i.e. USB
   power), the device shows "Updating firmware to version n. Please wait..." on a strip at the bottom of the
   screen, reconnects, and downloads `/firmware/inkplate_weather-<n>.bin`. The ESP32 updater checks the `x-MD5`
   header the server sends and the image header, writes the other app slot and restarts into it; the new build
   then fetches and draws a fresh picture.
4. If anything fails the strip changes to "Firmware update failed. It will try again later." and the device sleeps
   normally. A failed or interrupted attempt is recorded in NVS (`ota_try`); **it won't try again for 6 hours**.
   (A new build published after a *successful* update is picked up at the next wake.)

There is no automatic rollback (the Arduino core ships without the bootloader rollback option), so a build that
crashes on start needs a USB flash to recover. Test a build by flashing it over USB before publishing it.

### Publishing an update

1. In `firmware/inkplate_weather/version.h` raise `FW_VERSION` (plain integers: 1, 2, 3, ...). **Always bump it**:
   the device only updates when the server's number is higher than the one it reports, and a build that forgets to
   bump would be offered again and again (once per 6 h at most).
2. Build and export: `.\firmware\build.ps1 -Export`. This writes `firmware\dist\inkplate_weather-<ver>.bin` and
   `firmware\dist\manifest.json` (version, file name, MD5, SHA-256, size, board). Use the same `-Board` the device
   has; the manifest records the FQBN, but nothing on the device checks it.
3. Copy both files to the server's cache folder (on the Pi: the `./cache` volume of the compose project):

   ```
   ssh pi@192.168.1.254 "mkdir -p ~/inkplate-weather/cache/firmware"
   scp firmware/dist/inkplate_weather-<ver>.bin firmware/dist/manifest.json pi@192.168.1.254:~/inkplate-weather/cache/firmware/
   ```

   The server needs no restart. Without `manifest.json` it offers the highest-numbered `inkplate_weather-<n>.bin`
   in the folder; with one it offers the build the manifest names and refuses to offer a file whose MD5 doesn't
   match (a damaged upload). Delete the files (or the folder) to stop offering updates.
4. Check `http://<server>:8080/status`: the **Firmware** line shows the version on offer and the one the display last
   reported ("update pending" until the next wake installs it). The device installs it on its next wake that is
   not low on battery; press the wake button to make that happen now.

### The first flash (once, over USB)

```powershell
.\firmware\build.ps1 -Upload -Port COM3     # includes the OTA partition table; keep the Wi-Fi in config.h as before
```

The Wi-Fi settings already in `config.h` keep working (they are the defaults), so nothing needs to be set up in the
portal. After this, publish updates as above.

## Find the COM port

- Plug the Inkplate in over USB and switch it on.
- Run `.\.tools\arduino-cli\arduino-cli.exe --config-file .\.tools\arduino-cli.yaml board list`. The Inkplate
  appears as a "USB-SERIAL CH340" port, e.g. `COM5`.
- Or open **Device Manager → Ports (COM & LPT) → USB-SERIAL CH340 (COMx)**.
- If no CH340 port appears, install the CH340 driver: <https://soldered.com/blogs/learn/ch340-driver-installation>.
  Also try another cable; some USB cables carry power only.

## Read the serial log

```powershell
& .\.tools\arduino-cli\arduino-cli.exe --config-file .\.tools\arduino-cli.yaml monitor -p COM5 -c baudrate=115200
```

The device is only awake for about 5–20 s per wake, so logs appear only then. Opening the port resets the
board, so you'll see a `reason=boot` run. Press the wake button to see another. A typical successful run
looks like this:

```
[wake] #3 reason=button retry=0 clock=set fw=1
[config] ssid set, server 192.168.1.20:8080 (built-in defaults)
[battery] 4021 mV -> 80%
[wifi] connected in 1830 ms, ip 192.168.1.57, rssi -61
[fetch] GET http://192.168.1.20:8080/display.png?batt=80&mv=4021&reason=button&fw=1
[fetch] HTTP 200, 98213 bytes in 2410 ms
[draw] ok; X-Sleep-Seconds 1127, X-Firmware-Update -1
[sleep] 1127 s (retry 0/3, next wake is scheduled)
```

Close the monitor (Ctrl+C) before you upload again; only one program can use the port at a time.

## Power

- Wi-Fi is switched off (`WiFi.disconnect(true)` + `WIFI_OFF`) before decoding and refreshing, and again
  before sleeping. (An OTA update turns it back on for the download, only when one is due.)
- The setup portal's radio runs only while it is open, at most 10 minutes.
- Wi-Fi credentials aren't written to flash on every wake (`WiFi.persistent(false)`).
- The e-paper power supply turns off after each refresh (the library's `display()` default). Deep sleep keeps
  only the RTC timer and the GPIO 36 wake circuit alive.
- One full 3-bit refresh plus a request is roughly 5–20 s awake, 48 times a day.

## Troubleshooting

| Symptom | Likely cause / fix |
|---|---|
| Upload fails with "Failed to connect to ESP32" | The port is busy or the auto-reset missed. Close the serial monitor. Press the wake button and start the upload straight away, or unplug and replug USB, then retry. |
| Screen never changes after flashing | You picked the wrong board variant (Soldered vs e-radionica). Rebuild with the other FQBN. |
| Error screen: "Wi-Fi … wasn't found" | Wrong SSID, a 5 GHz-only network, or out of range. |
| Error screen: "Couldn't join Wi-Fi … Check the password." | Wrong password. Hold the wake button for 5 s and use the [setup portal](#wi-fi-setup-portal), or fix `WIFI_PASSWORD` and re-flash. |
| Error screen: "No answer from the server (connection refused)" | The container isn't running, or the wrong `SERVER_PORT`/`SERVER_HOST`. Test from a PC with `http://<host>:8080/display.png`. |
| Error screen: "No answer within 20 seconds." | The server is too slow to render. Check `GET /status` on the server. |
| Error screen: "The image is WxH, expected 1200x825." / "isn't a PNG image" | The server isn't following the `/display.png` contract. Check the server logs. |
| "Will retry in 5 minutes." instead of a clock time | The clock isn't set yet: there has been no successful request since power-on, and NTP isn't reachable. This is harmless. |
| Battery % looks stuck at 100 % | USB is plugged in; the charger holds about 4.2 V. |
| No `batt`/`mv` in server logs | The battery reading was outside 2.5–4.6 V (no battery connected). |
| Setup screen on power-on | No Wi-Fi network is configured (`WIFI_SSID` blank or still `your-wifi-name`, nothing saved). Use the portal. |
| Phone says the setup network has "no internet" | Expected. Stay connected and open `http://192.168.4.1`. |
| "Firmware update failed" strip | See the serial log (`[ota]`). Common: not enough free flash (build made for another partition scheme), MD5 mismatch (re-copy the files), server unreachable. It retries after 6 h. |
| Display never updates to the new build | `FW_VERSION` wasn't raised, battery is below 40 %, or the last attempt failed less than 6 h ago. `/status` shows what the server sees. |
| Wakes at odd times | The server sent no valid `X-Sleep-Seconds` and the clock was unknown, so it used plain 1800 s sleeps. One successful request fixes it. |

## Files

| File | Purpose |
|---|---|
| `inkplate_weather/inkplate_weather.ino` | Wake → battery → Wi-Fi → fetch → draw → (OTA) → sleep; error and setup screens; retry bookkeeping (RTC memory) |
| `inkplate_weather/fetch.{h,cpp}` | HTTP GET with a 20 s deadline, sends `fw`, reads `X-Sleep-Seconds`, `Date` and `X-Firmware-Update`, checks the PNG (signature, IHDR size, IEND) |
| `inkplate_weather/settings.{h,cpp}` | Wi-Fi/server settings in NVS with `config.h` defaults; server-address parsing; OTA attempt timestamp |
| `inkplate_weather/portal.{h,cpp}` | Captive-portal setup: SoftAP, DNS catch-all, web form with network scan |
| `inkplate_weather/ota.{h,cpp}` | Decides whether to update (battery, 6 h limit) and runs `HTTPUpdate` |
| `inkplate_weather/version.h` | `FW_VERSION`, the one place to bump a build number |
| `inkplate_weather/battery.{h,cpp}` | Voltage → percent curve |
| `inkplate_weather/timekeeping.{h,cpp}` | Timezone, HTTP-date parsing, :00/:30 alignment, "3:30 PM" formatting |
| `inkplate_weather/fonts/` | FreeSans 18 pt and FreeSans Bold 24 pt GFX fonts (copied from InkplateLibrary) for the error screen |
| `inkplate_weather/config.example.h` | Settings template; copy to `config.h` (gitignored) |
| `build.ps1` | Project-local Arduino CLI setup, compile, upload and `-Export` (OTA binary + manifest into `dist/`, gitignored) |
