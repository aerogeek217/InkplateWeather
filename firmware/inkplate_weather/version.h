// Firmware build number. The ONE place to bump it (see firmware/README.md, "Publishing an update").
//
// It is sent to the server as `fw=<FW_VERSION>` on every /display.png request. The server answers with
// `X-Firmware-Update: <n>` when it holds a newer build, and the device then downloads
// /firmware/inkplate_weather-<n>.bin. Plain increasing integers: 1, 2, 3, ...
#pragma once

#define FW_VERSION 1
