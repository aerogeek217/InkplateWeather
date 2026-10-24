#include "battery.h"

// Typical open-circuit discharge curve of a LiPo cell at light load (room temperature).
// The voltage is read right after wake, before Wi-Fi starts, so it is close to the resting voltage.
// The curve is flat between ~3.75 and ~3.95 V, so the middle percentages are approximate (+/- 10 %).
struct CurvePoint
{
    int mv;
    int pct;
};

static const CurvePoint CURVE[] = {
    {4200, 100}, {4150, 95}, {4110, 90}, {4080, 85}, {4020, 80}, {3980, 75}, {3950, 70},
    {3910, 65},  {3870, 60}, {3850, 55}, {3840, 50}, {3820, 45}, {3800, 40}, {3790, 35},
    {3770, 30},  {3750, 25}, {3730, 20}, {3710, 15}, {3690, 10}, {3610, 5},  {3270, 0},
};

int batteryPercent(int mv)
{
    const int n = sizeof(CURVE) / sizeof(CURVE[0]);
    if (mv >= CURVE[0].mv)
        return 100;
    if (mv <= CURVE[n - 1].mv)
        return 0;
    for (int i = 1; i < n; ++i)
    {
        if (mv >= CURVE[i].mv)
        {
            const CurvePoint &hi = CURVE[i - 1];
            const CurvePoint &lo = CURVE[i];
            // Linear interpolation, rounded to the nearest percent.
            int num = (mv - lo.mv) * (hi.pct - lo.pct);
            int den = hi.mv - lo.mv;
            return lo.pct + (num + den / 2) / den;
        }
    }
    return 0;
}
