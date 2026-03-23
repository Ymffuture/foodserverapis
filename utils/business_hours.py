# utils/business_hours.py
from datetime import datetime, timezone, timedelta

# SAST is UTC+2, no daylight saving time
SAST_OFFSET = timedelta(hours=2)
SAST = timezone(SAST_OFFSET)

# Business hours (24h format)
# None = closed all day
HOURS = {
    0: ("09:00", "17:00"),   # Monday
    1: ("09:00", "17:00"),   # Tuesday
    2: ("09:00", "17:00"),   # Wednesday
    3: ("09:00", "17:00"),   # Thursday
    4: ("09:00", "17:00"),   # Friday
    5: ("09:00", "14:00"),   # Saturday
    6: None,                 # Sunday — CLOSED (was wrongly set to ("09:00","20:00"))
}

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def get_status() -> dict:
    """
    Returns the current open/closed status and today's hours.
    Uses stdlib only — no pytz needed. SAST = UTC+2, no DST.
    """
    now_sast = datetime.now(SAST)
    weekday  = now_sast.weekday()   # 0=Mon, 6=Sun
    hours    = HOURS.get(weekday)

    schedule = {
        "Monday":    "09:00 – 17:00",
        "Tuesday":   "09:00 – 17:00",
        "Wednesday": "09:00 – 17:00",
        "Thursday":  "09:00 – 17:00",
        "Friday":    "09:00 – 17:00",
        "Saturday":  "09:00 – 14:00",
        "Sunday":    "Closed",
    }

    if hours is None:
        # Find next open day
        next_day  = None
        next_open = None
        for delta in range(1, 8):
            next_wd = (weekday + delta) % 7
            if HOURS.get(next_wd) is not None:
                next_day  = DAY_NAMES[next_wd]
                next_open = HOURS[next_wd][0]
                break
        return {
            "is_open":    False,
            "day":        DAY_NAMES[weekday],
            "open_time":  None,
            "close_time": None,
            "now_sast":   now_sast.strftime("%H:%M"),
            "message":    f"Closed today ({DAY_NAMES[weekday]}). We reopen {next_day} at {next_open}.",
            "schedule":   schedule,
        }

    # Build open/close datetimes in SAST for today
    oh, om = map(int, hours[0].split(":"))
    ch, cm = map(int, hours[1].split(":"))
    open_dt  = now_sast.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_dt = now_sast.replace(hour=ch, minute=cm, second=0, microsecond=0)

    is_open = open_dt <= now_sast < close_dt

    if now_sast < open_dt:
        msg = f"We open at {hours[0]} today. See you soon!"
    elif is_open:
        mins_left = int((close_dt - now_sast).total_seconds() / 60)
        if mins_left <= 30:
            msg = f"Closing soon at {hours[1]}! Order quickly."
        else:
            msg = f"We are open until {hours[1]} today."
    else:
        # After closing — find next open day
        msg = "Closed for today."
        for delta in range(1, 8):
            next_wd = (weekday + delta) % 7
            if HOURS.get(next_wd) is not None:
                next_day  = DAY_NAMES[next_wd]
                next_open = HOURS[next_wd][0]
                label     = "tomorrow" if delta == 1 else next_day
                msg = f"Closed for today. Opens {label} at {next_open}."
                break

    return {
        "is_open":    is_open,
        "day":        DAY_NAMES[weekday],
        "open_time":  hours[0],
        "close_time": hours[1],
        "now_sast":   now_sast.strftime("%H:%M"),
        "message":    msg,
        "schedule":   schedule,
    }
