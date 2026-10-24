// Battery voltage -> state-of-charge estimate for a single-cell Li-ion/LiPo (3.7 V nominal).
#pragma once
#include <stdint.h>

// Readings outside this window mean "no battery / not measurable"; the request then omits batt and mv.
static const int BATTERY_MV_MIN_PLAUSIBLE = 2500;
static const int BATTERY_MV_MAX_PLAUSIBLE = 4600;

// Percentage 0-100 from the resting cell voltage in millivolts.
// Piecewise-linear over the table in battery.cpp; clamps below 3270 mV (0 %) and above 4200 mV (100 %).
int batteryPercent(int millivolts);
