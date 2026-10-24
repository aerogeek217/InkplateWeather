# Inkplate 10 firmware

This firmware runs the Inkplate 10 as a thin client for the weather server. The server renders the whole screen.
The Inkplate wakes, downloads one PNG, shows it, and goes back to sleep.

## What it does

On every wake (from the timer, the wake button, or power-on):

1. **Reads the battery** before Wi-Fi starts, because radio current makes the voltage sag. See [Battery reporting](#battery-reporting).
2. **Joins Wi-Fi.** It gives up after 15 s (`WIFI_TIMEOUT_MS`).
3. **Requests the screen** with `GET http://SERVER_HOST:SERVER_PORT/display.png?batt=<pct>&mv=<millivolts>&reason=<timer|button|retry|boot>`.
   This follows the frozen contract in [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md#displaypng-contract-frozen-2026-10-24-firmware-builds-against-this).
   The whole request has a 20 s budget covering connect, render and download.
4. **Checks the reply.** It must be HTTP 200 and a complete PNG of exactly 1200 × 825.
5. **Turns Wi-Fi off**, decodes the PNG into the 3-bit (8-gray) framebuffer and does a **full refresh**.
   Dithering is off: the server already uses only the panel's 8 levels (0, 36, 73 … 255), and the
   library maps each of those exactly onto one of the panel's 8 grays.
6. **Deep-sleeps.** The sleep length comes from the server's `X-Sleep-Seconds` header (the next :00 or :30 plus about
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
screen with `reason=button`. If the button is still held when the device is about to sleep, it waits (up to
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

Compile result for Inkplate10V2:

| Memory | Used | Limit |
|---|---|---|
| Flash | 1,051,740 bytes (33 %) | 3,145,728 |
| Static RAM | 93,944 bytes (28 %) | 327,680 |

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

& $cli --config-file $cfg compile --fqbn soldered-inkplate-boards:esp32:Inkplate10V2 firmware\inkplate_weather
& $cli --config-file $cfg upload  --fqbn soldered-inkplate-boards:esp32:Inkplate10V2 --port COM5 firmware\inkplate_weather
```

To do compile and upload in one step, use `compile --upload --port COM5 …`.

## Build (Arduino IDE 2.x)

1. **File → Preferences → Additional boards manager URLs**: add the board manager URL from the table above.
2. **Tools → Board → Boards Manager**: search for "Soldered Inkplate Boards" and install **4.0.0**.
3. **Tools → Manage Libraries**: search for "InkplateLibrary" and install **11.2.1**.
4. Open `firmware/inkplate_weather/inkplate_weather.ino`. The other `.cpp`/`.h` files open as tabs.
5. **Tools → Board → Inkplate Boards → Soldered Inkplate 10**, or e-radionica Inkplate 10 for the old board.
   Leave the other Tools options at their defaults (PSRAM enabled, huge_app partition).
6. **Tools → Port**: pick the Inkplate's COM port. Then click **Upload**.

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
[wake] #3 reason=button retry=0 clock=set
[battery] 4021 mV -> 80%
[wifi] connected in 1830 ms, ip 192.168.1.57, rssi -61
[fetch] GET http://192.168.1.20:8080/display.png?batt=80&mv=4021&reason=button
[fetch] HTTP 200, 98213 bytes in 2410 ms
[draw] ok; X-Sleep-Seconds 1127
[sleep] 1127 s (retry 0/3, next wake is scheduled)
```

Close the monitor (Ctrl+C) before you upload again; only one program can use the port at a time.

## Power

- Wi-Fi is switched off (`WiFi.disconnect(true)` + `WIFI_OFF`) before decoding and refreshing, and again
  before sleeping.
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
| Error screen: "Couldn't join Wi-Fi … Check the password." | Wrong `WIFI_PASSWORD`. |
| Error screen: "No answer from the server (connection refused)" | The container isn't running, or the wrong `SERVER_PORT`/`SERVER_HOST`. Test from a PC with `http://<host>:8080/display.png`. |
| Error screen: "No answer within 20 seconds." | The server is too slow to render. Check `GET /status` on the server. |
| Error screen: "The image is WxH, expected 1200x825." / "isn't a PNG image" | The server isn't following the `/display.png` contract. Check the server logs. |
| "Will retry in 5 minutes." instead of a clock time | The clock isn't set yet: there has been no successful request since power-on, and NTP isn't reachable. This is harmless. |
| Battery % looks stuck at 100 % | USB is plugged in; the charger holds about 4.2 V. |
| No `batt`/`mv` in server logs | The battery reading was outside 2.5–4.6 V (no battery connected). |
| Wakes at odd times | The server sent no valid `X-Sleep-Seconds` and the clock was unknown, so it used plain 1800 s sleeps. One successful request fixes it. |

## Files

| File | Purpose |
|---|---|
| `inkplate_weather/inkplate_weather.ino` | Wake → battery → Wi-Fi → fetch → draw → sleep; error screen; retry bookkeeping (RTC memory) |
| `inkplate_weather/fetch.{h,cpp}` | HTTP GET with a 20 s deadline, reads `X-Sleep-Seconds` and `Date`, checks the PNG (signature, IHDR size, IEND) |
| `inkplate_weather/battery.{h,cpp}` | Voltage → percent curve |
| `inkplate_weather/timekeeping.{h,cpp}` | Timezone, HTTP-date parsing, :00/:30 alignment, "3:30 PM" formatting |
| `inkplate_weather/fonts/` | FreeSans 18 pt and FreeSans Bold 24 pt GFX fonts (copied from InkplateLibrary) for the error screen |
| `inkplate_weather/config.example.h` | Settings template; copy to `config.h` (gitignored) |
| `build.ps1` | Project-local Arduino CLI setup, compile and upload |
