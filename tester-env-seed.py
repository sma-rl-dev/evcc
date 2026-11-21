#!/usr/bin/env python3
"""Deterministic seed and check for the evcc tester environment.

tester-env.yaml supplies site, meters, chargers, vehicles, loadpoints and
tariffs. This script adds what a YAML config cannot: a database-configurable
loadpoint (needed for the Configuration view), completed charging sessions and
the 15 minute energy/tariff history of the previous week.

  tester-env-seed.py sql                    print the seed SQL (stdout)
  tester-env-seed.py check --base-url URL   assert the seeded state via /api

Dates are anchored to the local Europe/Berlin date (the container TZ): sessions
sit on fixed days of the current and two previous months clipped to today, the
history covers the seven complete days before today plus today's completed
slots. Same rows on every reset, never future-dated, and evcc's own rows (open
sessions, post-boot slots) are untouched.
"""

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Berlin")
HISTORY_DAYS = 7

# day of month, start (hh, mm), minutes, loadpoint, vehicle, kWh, soc start,
# soc end, odometer, solar %, price/kWh, added range
SESSIONS = [
    (4, (18, 10), 270, "Garage", "white Model 3", 24.6, 22, 80, 38120.0, 35, 0.20, 168),
    (9, (9, 25), 220, "Carport", "blue e-Golf", 15.2, 41, 86, 48210.0, 82, 0.11, 96),
    (15, (12, 0), 130, "Carport", "green Honda e", 6.4, 28, 100, 18460.0, 91, 0.09, 46),
    (21, (20, 0), 620, "Garage", "white Model 3", 32.0, 15, 92, 38244.0, 0, 0.22, 232),
    (26, (10, 15), 85, "Carport", "black VanMoof", 0.38, 45, 92, None, 70, 0.13, 11),
]

# Ids live above evcc's own rows (open sessions get autoincrement ids 1, 2, ...)
FIRST_SESSION_ID = 101

# Heat pump loadpoint as a configs-table row (class 5 = templates.Loadpoint).
# A yaml loadpoint is a static device without an id, so it cannot be opened and
# saved in the Configuration view; a db loadpoint can. minSoc (the dashboard
# "Min. temperature" for heating chargers) is absent on purpose: the settings
# dialog then shows "---"/x °C. The charger stays in tester-env.yaml and is
# referenced by name.
LOADPOINT_CONFIG_ID = 1
LOADPOINT_CONFIG = {
    "charger": "charger_3",
    "title": "Heat pump",
    "defaultMode": "pv",
    "ui": {"minTemp": 30, "maxTemp": 70},
}


def local_now():
    return datetime.now(TZ)


def month_shift(d, months):
    y, m = d.year, d.month + months
    while m < 1:
        y, m = y - 1, m + 12
    while m > 12:
        y, m = y + 1, m - 12
    return y, m


def go_time(dt):
    """Timestamp in the layout evcc's sqlite driver writes."""
    offset = dt.strftime("%z")
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f") + "000" + offset[:3] + ":" + offset[3:]


def expected_sessions(now=None):
    """Session rows as (id, created, minutes, loadpoint, ...) tuples in insert
    order; the last row is the seed-time anchored one."""
    now = now or local_now()
    today = now.date()
    rows = []
    next_id = FIRST_SESSION_ID
    for offset in (-2, -1, 0):
        year, month = month_shift(today, offset)
        for day, start, minutes, loadpoint, vehicle, kwh, soc_start, soc_end, odometer, solar, ppk, added_range in SESSIONS:
            created = datetime(year, month, day, start[0], start[1], tzinfo=TZ)
            # never future-dated: same-day sessions whose hour has not come yet are skipped
            if created > now:
                continue
            rows.append((next_id, created, minutes, loadpoint, vehicle, kwh, soc_start, soc_end, odometer, solar, ppk, added_range))
            next_id += 1

    # Always one session in the recent past, so the default month view is
    # populated even on the first days of a month. On the 1st just after
    # midnight it snaps to the month start instead of the previous month.
    created = (now - timedelta(minutes=165)).replace(second=0, microsecond=0)
    if (created.year, created.month) != (now.year, now.month):
        created = now.replace(hour=0, minute=0, second=0, microsecond=0)
    daytime = 9 <= created.hour < 16
    rows.append((next_id, created, 45, "Carport", "blue e-Golf", 3.2, 58, 66, 48266.0, 64 if daytime else 0, 0.12 if daytime else 0.31, 22))
    return rows


def session_record(created, minutes, loadpoint, vehicle, kwh, soc_start, soc_end, odometer, solar, ppk, added_range, index):
    meter_start = round(900.0 + index * 41.5, 1)
    return {
        "created": created,
        "finished": created + timedelta(minutes=minutes),
        "loadpoint": loadpoint,
        "vehicle": vehicle,
        "charged_kwh": kwh,
        "charge_duration": minutes * 60 * 10**9,
        "meter_start_kwh": meter_start,
        "meter_end_kwh": round(meter_start + kwh, 2),
        "soc_start": float(soc_start),
        "soc_end": float(soc_end),
        "odometer": odometer,
        "added_range": float(added_range),
        "solar_percentage": float(solar),
        "price_per_kwh": ppk,
        "price": round(kwh * ppk, 2),
        "co2_per_kwh": float(round(390 - 3.0 * solar)),
    }


def sql_num(v):
    if v is None:
        return "NULL"
    if isinstance(v, float):
        return repr(v)
    return str(v)


def sessions_sql(now=None):
    out = ["DELETE FROM sessions WHERE id BETWEEN %d AND %d;" % (FIRST_SESSION_ID, FIRST_SESSION_ID + 99)]
    for index, row in enumerate(expected_sessions(now)):
        sid, created, minutes, loadpoint, vehicle, kwh, soc_start, soc_end, odometer, solar, ppk, added_range = row
        rec = session_record(created, minutes, loadpoint, vehicle, kwh, soc_start, soc_end, odometer, solar, ppk, added_range, index)
        out.append(
            "INSERT INTO sessions (id, created, finished, loadpoint, identifier, vehicle, odometer,"
            " meter_start_kwh, meter_end_kwh, charged_kwh, charge_duration, soc_start, soc_end, added_range,"
            " solar_percentage, price, price_per_kwh, co2_per_kwh, reference_price_per_kwh, reference_co2_per_kwh)"
            " VALUES (%d, '%s', '%s', '%s', '', '%s', %s, %s, %s, %s, %d, %s, %s, %s, %s, %s, %s, %s, 0.3, 380.0);"
            % (
                sid,
                go_time(rec["created"]),
                go_time(rec["finished"]),
                rec["loadpoint"],
                rec["vehicle"],
                sql_num(rec["odometer"]),
                sql_num(rec["meter_start_kwh"]),
                sql_num(rec["meter_end_kwh"]),
                sql_num(rec["charged_kwh"]),
                rec["charge_duration"],
                sql_num(rec["soc_start"]),
                sql_num(rec["soc_end"]),
                sql_num(rec["added_range"]),
                sql_num(rec["solar_percentage"]),
                sql_num(rec["price"]),
                sql_num(rec["price_per_kwh"]),
                sql_num(rec["co2_per_kwh"]),
            )
        )
    return out


def day_factor(day):
    return (0.55, 0.75, 0.9, 1.0, 0.62, 0.48, 0.8)[day.toordinal() % 7]


def house_kw(h):
    kw = 0.32
    if 6 <= h < 8:
        kw += 0.55
    if 12 <= h < 13.5:
        kw += 0.6
    if 17 <= h < 21.5:
        kw += 0.95
    if h >= 22 or h < 5.5:
        kw += 0.15
    return kw


def pv_kw(day, h):
    if h < 7.5 or h > 17.0:
        return 0.0
    return 6.6 * math.sin(math.pi * (h - 7.5) / 9.5) * day_factor(day)


def loadpoint_kw(days_before, h):
    # Carport tops up two days back, Garage charges overnight four days back
    if days_before == 2 and 10 <= h < 12:
        return 3.8
    if days_before == 4 and 19 <= h < 21.5:
        return 4.3
    return 0.0


def grid_price(h):
    if 17 <= h < 21:
        return 0.336
    if h < 6:
        return 0.196
    return 0.268


def history(now=None, include_today=True):
    """Slot rows for the last HISTORY_DAYS complete days (plus today's completed
    slots when include_today), keyed by entity."""
    now = now or local_now()
    today = now.date()
    rows = {"home": [], "grid": [], "pv": [], "battery": [], "lp-1": [], "lp-2": []}
    tariffs = []
    first_day = HISTORY_DAYS
    last_day = 0 if include_today else 1
    for days_before in range(first_day, last_day - 1, -1):
        day = today - timedelta(days=days_before)
        soc = 45.0 + (day.toordinal() % 12)
        start = datetime(day.year, day.month, day.day, tzinfo=TZ)
        ts = int(start.timestamp())
        end = int((start + timedelta(days=1)).timestamp())
        if days_before == 0:
            # today: only completed slots, so evcc's own writes stay untouched
            end = min(end, int(now.timestamp()) // 900 * 900)
        while ts < end:
            local = datetime.fromtimestamp(ts, TZ)
            h = local.hour + local.minute / 60.0
            pv = pv_kw(day, h) * 0.25
            carport = loadpoint_kw(days_before, h) if days_before == 2 else 0.0
            garage = loadpoint_kw(days_before, h) if days_before == 4 else 0.0
            lp1 = carport * 0.25
            lp2 = garage * 0.25
            house = (house_kw(h) + carport + garage) * 0.25
            surplus = pv - house
            if surplus > 0.1:
                charge = min(0.6, surplus)
                discharge = 0.0
            elif (h >= 17 or h < 6) and soc > 22:
                charge = 0.0
                discharge = min(0.3, house * 0.7)
            else:
                charge = discharge = 0.0
            soc = max(15.0, min(98.0, soc + (charge - discharge) / 13.4 * 100))
            grid_import = max(0.0, house + charge - pv - discharge)
            grid_export = max(0.0, pv + discharge - house - charge)
            rows["home"].append((ts, round(house, 3), 0.0, None))
            rows["grid"].append((ts, round(grid_import, 3), round(grid_export, 3), None))
            rows["pv"].append((ts, round(pv, 3), 0.0, None))
            rows["battery"].append((ts, round(charge, 3), round(discharge, 3), round(soc, 1)))
            rows["lp-1"].append((ts, round(lp1, 3), 0.0, None))
            rows["lp-2"].append((ts, round(lp2, 3), 0.0, None))
            daylight = max(0.0, math.sin(math.pi * (h - 7.5) / 9.5))
            tariffs.append(
                (
                    ts,
                    grid_price(h),
                    0.082,
                    round(390 - 120 * daylight, 1),
                    round(9.0 + 4.0 * math.sin(math.pi * (h - 9) / 12) - (days_before - 4) * 0.3, 1),
                )
            )
            ts += 900
    return rows, tariffs


ENTITY_KEYS = {
    "home": ("home", "home"),
    "grid": ("grid", "grid"),
    "pv": ("pv", "pv"),
    "battery": ("battery", "battery"),
    "lp-1": ("loadpoint", "lp-1"),
    "lp-2": ("loadpoint", "lp-2"),
}


def meter_insert(name, values):
    group, entity = ENTITY_KEYS[name]
    out = []
    # SQLite caps a compound SELECT at 500 terms, so keep chunks well below
    for offset in range(0, len(values), 400):
        selects = []
        for ts, energy, return_energy, soc in values[offset : offset + 400]:
            if not selects:
                selects.append("SELECT %d AS ts, %.3f AS energy, %.3f AS return_energy, %s AS soc" % (ts, energy, return_energy, sql_num(soc)))
            else:
                selects.append("SELECT %d, %.3f, %.3f, %s" % (ts, energy, return_energy, sql_num(soc)))
        out.append(
            "INSERT OR REPLACE INTO meters (meter, ts, energy, return_energy, soc_temp, recovered)\n"
            "SELECT e.id, v.ts, v.energy, v.return_energy, v.soc, 0\n"
            "FROM entities e\n"
            "JOIN (\n  " + "\n  UNION ALL ".join(selects) + "\n) v\n"
            "WHERE e.\"group\" = '%s' AND e.name = '%s';" % (group, entity)
        )
    return out


def history_sql(now=None):
    rows, tariffs = history(now)
    out = ["BEGIN;"]
    for name, values in rows.items():
        out.extend(meter_insert(name, values))
    for offset in range(0, len(tariffs), 400):
        selects = []
        for ts, grid, feedin, co2, temperature in tariffs[offset : offset + 400]:
            if not selects:
                selects.append("SELECT %d AS ts, %s AS grid, %s AS feedin, %s AS co2, %s AS temperature" % (ts, sql_num(grid), sql_num(feedin), sql_num(co2), sql_num(temperature)))
            else:
                selects.append("SELECT %d, %s, %s, %s, %s" % (ts, sql_num(grid), sql_num(feedin), sql_num(co2), sql_num(temperature)))
        out.append(
            "INSERT OR REPLACE INTO tariffs (ts, grid, feedin, co2, temperature)\n"
            "SELECT v.ts, v.grid, v.feedin, v.co2, v.temperature FROM (\n  "
            + "\n  UNION ALL ".join(selects)
            + "\n) v;"
        )
    out.append("COMMIT;")
    return out


def loadpoint_config_sql():
    """Replace all db loadpoints with the single fixed Heat pump row."""
    value = json.dumps(LOADPOINT_CONFIG, sort_keys=True, separators=(",", ":"))
    return [
        "BEGIN;",
        "DELETE FROM configs WHERE class = 5;",
        "INSERT INTO configs (id, class, type, disable, title, icon, product, value)"
        " VALUES (%d, 5, '', 0, '', '', '', '%s');" % (LOADPOINT_CONFIG_ID, value.replace("'", "''")),
        "COMMIT;",
    ]


def get_json(base_url, path):
    with urllib.request.urlopen(base_url.rstrip("/") + path, timeout=30) as response:
        return json.load(response)


def check(base_url):
    now = local_now()
    errors = []

    def fail(message):
        errors.append(message)

    state = get_json(base_url, "/api/state")
    if not state.get("apiReady"):
        fail("apiReady is not true")
    if state.get("siteTitle") != "Home Energy":
        fail("site title is not 'Home Energy'")
    lps = state.get("loadpoints", [])
    if len(lps) != 3:
        fail("expected 3 loadpoints")

    # Heat pump: exactly one, heating charger, fixed temperature range, unset min
    heat = [lp for lp in lps if lp.get("title") == "Heat pump"]
    if len(heat) != 1:
        fail("expected exactly 1 loadpoint titled 'Heat pump', got %d" % len(heat))
    else:
        lp = heat[0]
        if not lp.get("chargerFeatureHeating"):
            fail("Heat pump is not driven by a heating charger")
        if lp.get("ui") != {"minTemp": 30, "maxTemp": 70}:
            fail("Heat pump ui: expected minTemp 30 / maxTemp 70, got %r" % (lp.get("ui"),))
        if lp.get("minSoc"):
            fail("Heat pump minimum temperature: expected unset, got %r" % lp.get("minSoc"))

    # Heat pump must be database-configurable so the Configuration view can edit it
    db_name = "db:%d" % LOADPOINT_CONFIG_ID
    if not any(c.get("name") == db_name for c in get_json(base_url, "/api/config/loadpoints")):
        fail("loadpoint config %s missing from /api/config/loadpoints" % db_name)
    else:
        row = get_json(base_url, "/api/config/loadpoints/%d" % LOADPOINT_CONFIG_ID)
        for field, want in (("id", LOADPOINT_CONFIG_ID), ("title", "Heat pump"), ("charger", "charger_3")):
            if row.get(field) != want:
                fail("loadpoint config %s: expected %r, got %r" % (field, want, row.get(field)))
        if row.get("minSoc"):
            fail("loadpoint config minSoc: expected unset, got %r" % row.get("minSoc"))
        if row.get("ui") != {"minTemp": 30, "maxTemp": 70}:
            fail("loadpoint config ui: expected minTemp 30 / maxTemp 70, got %r" % (row.get("ui"),))

    sessions = get_json(base_url, "/api/sessions")
    expected = expected_sessions(now)
    if len(sessions) != len(expected):
        fail("sessions: expected %d, got %d" % (len(expected), len(sessions)))
    by_id = {s["id"]: s for s in sessions}

    def created_of(actual):
        return datetime.fromisoformat(actual["created"].replace("Z", "+00:00"))

    def compare_fields(sid, actual, rec):
        for field, want in (
            ("loadpoint", rec["loadpoint"]),
            ("vehicle", rec["vehicle"]),
            ("solarPercentage", rec["solar_percentage"]),
            ("co2PerKWh", rec["co2_per_kwh"]),
        ):
            if actual.get(field) != want:
                fail("session %d %s: expected %r, got %r" % (sid, field, want, actual.get(field)))
        for field, want in (("chargedEnergy", rec["charged_kwh"]), ("price", rec["price"])):
            got = actual.get(field)
            if got is None or abs(got - want) > 1e-6:
                fail("session %d %s: expected %r, got %r" % (sid, field, want, got))

    for index, row in enumerate(expected):
        sid, created, minutes, loadpoint, vehicle, kwh, soc_start, soc_end, odometer, solar, ppk, added_range = row
        actual = by_id.get(sid)
        if actual is None:
            fail("session %d (%s) missing" % (sid, vehicle))
            continue
        if index == len(expected) - 1:
            # The freshest session is anchored to seed time. Compare its fields
            # (solar share follows the created hour) and only bound the date.
            created = created_of(actual)
            daytime = 9 <= created.hour < 16
            rec = session_record(created, minutes, loadpoint, vehicle, kwh, soc_start, soc_end, odometer, 64 if daytime else 0, 0.12 if daytime else 0.31, added_range, index)
            compare_fields(sid, actual, rec)
            if created > now or (now - created) > timedelta(hours=4):
                fail("session %d created %s is not within the last 4 hours" % (sid, created.isoformat()))
            if (created.year, created.month) != (now.year, now.month):
                fail("session %d created %s is outside the current month" % (sid, created.isoformat()))
            continue
        rec = session_record(created, minutes, loadpoint, vehicle, kwh, soc_start, soc_end, odometer, solar, ppk, added_range, index)
        compare_fields(sid, actual, rec)
        # created/finished are RFC3339 with the same offset the seed wrote
        for field in ("created", "finished"):
            parsed = datetime.fromisoformat(actual[field].replace("Z", "+00:00"))
            if parsed != rec[field]:
                fail("session %d %s: expected %s, got %s" % (sid, field, rec[field].isoformat(), parsed.isoformat()))

    months = {}
    for session in sessions:
        created = created_of(session)
        if created > now:
            fail("session %d is future-dated" % session["id"])
        months[(created.year, created.month)] = months.get((created.year, created.month), 0) + 1
    for (year, month), count in sorted(months.items()):
        got = get_json(base_url, "/api/sessions?year=%d&month=%02d" % (year, month))
        if len(got) != count:
            fail("sessions %d-%02d: expected %d, got %d" % (year, month, count, len(got)))
    if (now.year, now.month) not in months:
        fail("no session in the current month")

    today = now.date()
    start = datetime(today.year, today.month, today.day, tzinfo=TZ) - timedelta(days=HISTORY_DAYS)
    end = datetime(today.year, today.month, today.day, tzinfo=TZ)
    query = "/api/history/energy?aggregate=day&from=%s&to=%s" % (
        urllib.parse.quote(start.isoformat()),
        urllib.parse.quote(end.isoformat()),
    )
    series = get_json(base_url, query)
    rows, _ = history(now, include_today=False)
    titles = {s.get("title") or s.get("group"): s for s in series}
    want_titles = {"home": "home", "grid": "grid", "pv": "pv", "battery": "battery"}
    for key, title in want_titles.items():
        s = titles.get(title)
        if s is None:
            fail("energy history: %s series missing" % title)
            continue
        if len(s["data"]) != HISTORY_DAYS:
            fail("energy history: %s has %d day buckets, expected %d" % (title, len(s["data"]), HISTORY_DAYS))
        want = sum(v[1] for v in rows[key])
        got = sum(d["energy"] for d in s["data"])
        if abs(got - want) > 1e-6:
            fail("energy history: %s energy expected %.3f, got %.3f" % (title, want, got))
    grid = titles.get("grid")
    if grid is not None and sum(d.get("cost") or 0 for d in grid["data"]) <= 0:
        fail("energy history: grid cost missing (tariff join empty)")

    if errors:
        for message in errors:
            print("FAIL: " + message, file=sys.stderr)
        return 1
    print("seed check: %d sessions across %d months, %d days of energy history" % (len(expected), len(months), HISTORY_DAYS))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("sql", help="print the seed SQL")
    check_parser = sub.add_parser("check", help="assert the seeded state via the API")
    check_parser.add_argument("--base-url", required=True)
    args = parser.parse_args()

    if args.command == "sql":
        for line in loadpoint_config_sql():
            print(line)
        for line in sessions_sql():
            print(line)
        for line in history_sql():
            print(line)
        return 0
    if args.command == "check":
        return check(args.base_url)
    return 1


if __name__ == "__main__":
    sys.exit(main())
