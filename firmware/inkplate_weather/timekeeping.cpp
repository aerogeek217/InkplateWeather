#include "timekeeping.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/time.h>

static const time_t VALID_AFTER = 1704067200; // 2024-01-01T00:00:00Z

void timeInitZone(const char *posixTz)
{
    setenv("TZ", posixTz, 1);
    tzset();
}

bool timeIsValid()
{
    return time(nullptr) > VALID_AFTER;
}

void timeSet(time_t utc)
{
    struct timeval tv = {utc, 0};
    settimeofday(&tv, nullptr);
}

// Days since 1970-01-01 for a proleptic Gregorian date (Howard Hinnant's days_from_civil).
static long daysFromCivil(int y, unsigned m, unsigned d)
{
    y -= m <= 2;
    const long era = (y >= 0 ? y : y - 399) / 400;
    const unsigned yoe = (unsigned)(y - era * 400);
    const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
    const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    return era * 146097 + (long)doe - 719468;
}

bool timeParseHttpDate(const char *s, time_t *out)
{
    if (!s)
        return false;
    int day, year, hh, mm, ss;
    char mon[4] = {0};
    // "Sat, 24 Oct 2026 15:21:03 GMT"
    if (sscanf(s, "%*3s, %d %3s %d %d:%d:%d", &day, mon, &year, &hh, &mm, &ss) != 6)
        return false;
    static const char *MONTHS[] = {"Jan", "Feb", "Mar", "Apr", "May", "Jun",
                                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"};
    int month = 0;
    for (int i = 0; i < 12; ++i)
        if (strncmp(mon, MONTHS[i], 3) == 0)
            month = i + 1;
    if (!month || day < 1 || day > 31 || year < 2024 || hh > 23 || mm > 59 || ss > 60)
        return false;
    *out = (time_t)(daysFromCivil(year, month, day) * 86400L + hh * 3600L + mm * 60L + ss);
    return true;
}

uint32_t timeSecondsToNextSlot(int periodMinutes, int offsetSeconds)
{
    if (periodMinutes < 1 || periodMinutes > 60)
        periodMinutes = 30;
    const time_t now = time(nullptr);
    struct tm local;
    localtime_r(&now, &local);
    const int period = periodMinutes * 60;
    const int intoHour = local.tm_min * 60 + local.tm_sec;
    // Slots restart at the top of each hour, so 60 must be divisible by the period for even spacing.
    int toNext = period - (intoHour % period);
    int sleep = toNext + offsetSeconds;
    if (sleep < 60)
        sleep += period;
    return (uint32_t)sleep;
}

void timeFormatClock(time_t utc, char *out, size_t outLen)
{
    struct tm local;
    localtime_r(&utc, &local);
    int h12 = local.tm_hour % 12;
    if (h12 == 0)
        h12 = 12;
    snprintf(out, outLen, "%d:%02d %s", h12, local.tm_min, local.tm_hour < 12 ? "AM" : "PM");
}
