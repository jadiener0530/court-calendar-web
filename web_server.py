"""
web_server.py  —  Read-only Flask web calendar for Court Scheduler
Reads schedule data from Firebase Realtime Database.
Runs as a local daemon thread (via court_scheduler.py) AND as a
standalone Render.com deployment (gunicorn web_server:app).
"""

import os
import json
import sqlite3
from datetime import date, timedelta
from functools import wraps

from flask import (
    Flask, render_template, redirect, url_for, request, session
)
from werkzeug.security import check_password_hash, generate_password_hash

# ── Paths ──────────────────────────────────────────────────────────────────────
WORK_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE  = os.path.join(WORK_DIR, "court_schedule.db")
TMPL_DIR = os.path.join(WORK_DIR, "templates")

FIREBASE_DB_URL = "https://courts-calendar-default-rtdb.firebaseio.com"

LOCATIONS = [
    "Circuit Court",
    "Front Desk",
    "General District Court",
    "Juvenile & Domestic Relations",
    "Holding",
    "Transports",
    "Open",
    "Close",
]

# ── Flask app ──────────────────────────────────────────────────────────────────
app = Flask(__name__, template_folder=TMPL_DIR)
app.secret_key = os.environ.get("COURT_SECRET_KEY", "court-schedule-secret-key-2024")


# ── Firebase init ──────────────────────────────────────────────────────────────

def _init_firebase():
    import firebase_admin
    if firebase_admin._apps:
        return  # already initialised

    from firebase_admin import credentials

    # Try environment variable first (Render deployment)
    key_json_env = os.environ.get("FIREBASE_KEY_JSON", "").strip()
    if key_json_env:
        try:
            key_dict = json.loads(key_json_env)
            cred = credentials.Certificate(key_dict)
            firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
            return
        except Exception as exc:
            raise RuntimeError(f"FIREBASE_KEY_JSON env var is set but could not be parsed: {exc}")

    # Fall back to local key file (desktop app / local testing)
    key_file = os.path.join(WORK_DIR, "firebase_key.json")
    if os.path.exists(key_file):
        cred = credentials.Certificate(key_file)
        firebase_admin.initialize_app(cred, {"databaseURL": FIREBASE_DB_URL})
        return

    raise RuntimeError(
        "Firebase credentials not found. "
        "Set FIREBASE_KEY_JSON env var or place firebase_key.json in the app folder."
    )


# Initialise on import
_init_firebase()


# ── Firebase data helpers ──────────────────────────────────────────────────────

def _get_fb_data():
    """Fetch the full schedule payload from Firebase. Returns {} if empty."""
    from firebase_admin import db as _fb
    data = _fb.reference("/").get()
    return data or {}


def get_schedule_for_days(day_strings):
    """Return {day_str: {location: [deputies]}} for the given list of day strings."""
    day_set = set(day_strings)
    raw = _get_fb_data().get("schedule") or {}
    result = {}
    for key, deputies in raw.items():
        parts = key.split("|", 1)
        if len(parts) != 2 or parts[0] not in day_set:
            continue
        d, loc = parts
        if isinstance(deputies, dict):
            deputies = [deputies[k] for k in sorted(deputies, key=lambda x: int(x))]
        result.setdefault(d, {})[loc] = deputies or []
    return result


def get_events_for_days(day_strings):
    """Return {day_str: {location: [events]}} for the given list of day strings."""
    day_set = set(day_strings)
    raw = _get_fb_data().get("cell_events") or {}
    result = {}
    for key, evs in raw.items():
        parts = key.split("|", 1)
        if len(parts) != 2 or parts[0] not in day_set:
            continue
        d, loc = parts
        if isinstance(evs, dict):
            evs = [evs[k] for k in sorted(evs, key=lambda x: int(x))]
        result.setdefault(d, {})[loc] = evs or []
    return result


def get_notes_for_days(day_strings):
    """Return {day_str: {location: note}} for the given list of day strings."""
    day_set = set(day_strings)
    raw = _get_fb_data().get("cell_notes") or {}
    result = {}
    for key, note in raw.items():
        parts = key.split("|", 1)
        if len(parts) != 2 or parts[0] not in day_set:
            continue
        d, loc = parts
        if note:
            result.setdefault(d, {})[loc] = note
    return result


def check_login(username, password):
    """Validate credentials against Firebase web_users node."""
    users = _get_fb_data().get("web_users") or {}
    user  = users.get(username)
    if user and check_password_hash(user.get("password_hash", ""), password):
        return True
    return False


# ── Date helpers ───────────────────────────────────────────────────────────────

def week_days(anchor: date):
    monday = anchor - timedelta(days=anchor.weekday())
    return [monday + timedelta(days=i) for i in range(7)]


def month_weeks(year: int, month: int):
    first = date(year, month, 1)
    start = first - timedelta(days=first.weekday())
    if month == 12:
        last = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        last = date(year, month + 1, 1) - timedelta(days=1)
    end = last + timedelta(days=(6 - last.weekday()))
    weeks, cur = [], start
    while cur <= end:
        weeks.append([cur + timedelta(days=i) for i in range(7)])
        cur += timedelta(weeks=1)
    return weeks


# ── Auth helper ────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login", next=request.path))
        return f(*args, **kwargs)
    return decorated


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return redirect(url_for("week_view", date_str=date.today().isoformat()))


@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if check_login(username, password):
            session.clear()
            session["username"] = username
            return redirect(request.args.get("next") or url_for("index"))
        error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/week/")
@app.route("/week/<date_str>")
@login_required
def week_view(date_str=None):
    try:
        anchor = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        anchor = date.today()

    days        = week_days(anchor)
    day_strings = [d.isoformat() for d in days]
    schedule    = get_schedule_for_days(day_strings)
    events      = get_events_for_days(day_strings)
    notes       = get_notes_for_days(day_strings)

    return render_template(
        "schedule.html",
        view="week",
        days=days,
        day_strings=day_strings,
        schedule=schedule,
        events=events,
        notes=notes,
        locations=LOCATIONS,
        prev_nav=(days[0] - timedelta(weeks=1)).isoformat(),
        next_nav=(days[0] + timedelta(weeks=1)).isoformat(),
        nav_label=f"Week of {days[0].strftime('%B %d, %Y')}",
        username=session.get("username", ""),
        today=date.today().isoformat(),
        year=days[0].year,
        month=days[0].month,
    )


@app.route("/month/")
@app.route("/month/<int:year>/<int:month>")
@login_required
def month_view(year=None, month=None):
    today = date.today()
    year  = year  or today.year
    month = max(1, min(12, month or today.month))

    weeks       = month_weeks(year, month)
    day_strings = [d.isoformat() for w in weeks for d in w]
    schedule    = get_schedule_for_days(day_strings)
    events      = get_events_for_days(day_strings)

    prev_y, prev_m = (year - 1, 12) if month == 1  else (year, month - 1)
    next_y, next_m = (year + 1,  1) if month == 12 else (year, month + 1)

    return render_template(
        "schedule.html",
        view="month",
        weeks=weeks,
        schedule=schedule,
        events=events,
        locations=LOCATIONS,
        year=year,
        month=month,
        month_name=date(year, month, 1).strftime("%B %Y"),
        prev_nav=f"/month/{prev_y}/{prev_m}",
        next_nav=f"/month/{next_y}/{next_m}",
        nav_label=date(year, month, 1).strftime("%B %Y"),
        username=session.get("username", ""),
        today=today.isoformat(),
        week_anchor=today.isoformat(),
    )


# ── User management helpers (called from ManageWebUsersDialog) ─────────────────

def add_user(username: str, password: str, display_name: str = ""):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    ph   = generate_password_hash(password)
    conn.execute(
        "INSERT OR REPLACE INTO web_users(username, password_hash, display_name) VALUES(?,?,?)",
        (username, ph, display_name)
    )
    conn.commit(); conn.close()


def remove_user(username: str):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute("DELETE FROM web_users WHERE username = ?", (username,))
    conn.commit(); conn.close()


def list_users():
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT username, display_name FROM web_users ORDER BY username"
    ).fetchall()
    conn.close()
    return [(r["username"], r["display_name"] or "") for r in rows]


def change_password(username: str, new_password: str):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    conn.execute(
        "UPDATE web_users SET password_hash=? WHERE username=?",
        (generate_password_hash(new_password), username)
    )
    conn.commit(); conn.close()


# ── Standalone / Render entry point ───────────────────────────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
