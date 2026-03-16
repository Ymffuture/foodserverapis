# utils/business_hours.py
from datetime import datetime
import pytz

SAST = pytz.timezone("Africa/Johannesburg")

# Business hours in SAST
HOURS = {
    0: ("09:00", "17:00"),   # Monday
    1: ("09:00", "17:00"),   # Tuesday
    2: ("09:00", "17:00"),   # Wednesday
    3: ("09:00", "17:00"),   # Thursday
    4: ("09:00", "17:00"),   # Friday
    5: ("09:00", "14:00"),   # Saturday  (9am + 5 hours)
    6: None,                  # Sunday    (closed)
}

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _parse_time(t: str, date: datetime.date) -> datetime:
    h, m = map(int, t.split(":"))
    return SAST.localize(datetime(date.year, date.month, date.day, h, m))


def get_status() -> dict:
    """
    Returns the current open/closed status and today's hours.
    All times are in SAST (UTC+2).
    """
    now_sast = datetime.now(SAST)
    weekday  = now_sast.weekday()        # 0=Mon, 6=Sun
    today    = now_sast.date()
    hours    = HOURS.get(weekday)

    if hours is None:
        return {
            "is_open":    False,
            "day":        DAY_NAMES[weekday],
            "open_time":  None,
            "close_time": None,
            "message":    "We are closed on Sundays. See you Monday!",
            "next_open":  "Monday at 09:00",
        }

    open_dt  = _parse_time(hours[0], today)
    close_dt = _parse_time(hours[1], today)
    is_open  = open_dt <= now_sast < close_dt

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
        msg = f"We are closed for today. Opening tomorrow at 09:00."
        # Find next open weekday
        for delta in range(1, 8):
            next_wd = (weekday + delta) % 7
            if HOURS.get(next_wd) is not None:
                next_day  = DAY_NAMES[next_wd]
                next_open = HOURS[next_wd][0]
                msg = f"Closed for today. Opening {next_day} at {next_open}."
                break

    return {
        "is_open":    is_open,
        "day":        DAY_NAMES[weekday],
        "open_time":  hours[0],
        "close_time": hours[1],
        "now_sast":   now_sast.strftime("%H:%M"),
        "message":    msg,
        "schedule": {
            "Monday":    "09:00 – 17:00",
            "Tuesday":   "09:00 – 17:00",
            "Wednesday": "09:00 – 17:00",
            "Thursday":  "09:00 – 17:00",
            "Friday":    "09:00 – 17:00",
            "Saturday":  "09:00 – 14:00",
            "Sunday":    "Closed",
        },
    }
