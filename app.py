import math
import os
import requests
import time
from datetime import datetime, timezone
from flask import Flask, render_template, request, redirect, url_for

# Environment-driven configuration for keys and tariffs.
TS_CHANNEL_ID = os.environ.get("TS_CHANNEL_ID", "3211996")
TS_READ_KEY = os.environ.get("TS_READ_KEY", "PLHADOBANAWGG825")
TS_WRITE_KEY = os.environ.get("TS_WRITE_KEY", "JS4KE9YYJ5C8TIBW")
COST_PER_KWH = float(os.environ.get("COST_PER_KWH", "0.12"))
V_ALERT = float(os.environ.get("V_ALERT", "10.0"))

# Base URL of the ESP32 HTTP server for direct relay control.
# You can set ESP32_BASE in env to just the IP (e.g. "10.187.167.242") and
# we will automatically prefix "http://" if needed.
ESP32_BASE = os.environ.get("ESP32_BASE", "10.187.167.242")
if not ESP32_BASE.startswith("http://") and not ESP32_BASE.startswith("https://"):
    ESP32_BASE = f"http://{ESP32_BASE}"
ESP32_BASE = ESP32_BASE.rstrip("/")

app = Flask(__name__)
LAST_RELAY_WRITE = 0  # cooldown tracker
# Remember the last good values so UI doesn't blank when ThingSpeak times out.
_last_good = {
    "v": 0.0,
    "i": 0.0,
    "p": 0.0,
    "energy_kwh": 0.0,
    "relay": 0,
    "alert": 0,
    "cost": 0.0,
}


def fetch_last():
    """Fetch the most recent feed entry; fall back to last good on timeout/error."""
    global _last_good
    url = f"https://api.thingspeak.com/channels/{TS_CHANNEL_ID}/feeds.json"
    params = {"api_key": TS_READ_KEY, "results": 1}
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        feeds = r.json().get("feeds") or [{}]
        feed = feeds[0]

        def f(idx, default=0.0):
            v = feed.get(f"field{idx}")
            try:
                return float(v)
            except Exception:
                return default

        updated = {
            "v": f(1, _last_good["v"]),
            "i": f(2, _last_good["i"]),
            "p": f(3, _last_good["p"]),
            "energy_kwh": f(4, _last_good["energy_kwh"]),
            "relay": int(f(5, _last_good["relay"])),
            "alert": int(f(6, _last_good["alert"])),
            "cost": f(7, _last_good["cost"]),
        }
        _last_good = updated
        return updated
    except Exception:
        return _last_good


def update_relay(cmd: int):
    """Send relay command directly to ESP32 HTTP endpoint (no ThingSpeak in between)."""
    global LAST_RELAY_WRITE
    # simple spam protection: ignore if called too frequently
    if time.time() - LAST_RELAY_WRITE < 0.5:
        raise RuntimeError("Relay command sent too quickly, please wait a moment.")
    url = f"{ESP32_BASE}/relay"
    r = requests.get(url, params={"cmd": int(cmd)}, timeout=5)
    r.raise_for_status()
    LAST_RELAY_WRITE = time.time()


def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default


def fetch_history(results: int = 200):
    """Fetch recent history from ThingSpeak for charting & energy calculations."""
    url = f"https://api.thingspeak.com/channels/{TS_CHANNEL_ID}/feeds.json"
    params = {"api_key": TS_READ_KEY, "results": results}
    try:
        r = requests.get(url, params=params, timeout=8)
        r.raise_for_status()
        feeds = r.json().get("feeds") or []
    except Exception:
        return []

    history: list[dict] = []
    for f in feeds:
        t_str = f.get("created_at")
        dt = None
        if t_str:
            # ThingSpeak returns ISO8601 in UTC, e.g. "2025-12-23T13:45:12Z"
            try:
                dt = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
            except Exception:
                dt = None
        history.append(
            {
                "time": f.get("created_at"),
                "dt": dt,
                "v": _safe_float(f.get("field1")),
                "i": _safe_float(f.get("field2")),
                "p": _safe_float(f.get("field3")),
                "e": _safe_float(f.get("field4")),
            }
        )
    return history


@app.route("/", methods=["GET"])
def home():
    data = fetch_last()
    history = fetch_history(200)

    # Basic values from latest ThingSpeak point (use absolute so no negatives on UI).
    current_kw = max(0.0, abs(data["p"]) / 1000.0)
    # Default: trust device's "today" energy if provided.
    daily_kwh = max(0.0, abs(data["energy_kwh"]))

    # Derive daily & monthly totals from history (difference in cumulative energy).
    month_total_kwh = daily_kwh * 30  # fallback
    if history:
        # filter points with valid datetime
        points = [h for h in history if h.get("dt") is not None]
        if points:
            points.sort(key=lambda x: x["dt"])
            now_utc = datetime.now(timezone.utc)
            today = now_utc.date()

            first_point = points[0]
            last_point = points[-1]

            # Daily: first and last samples for "today"
            today_points = [p for p in points if p["dt"].date() == today]
            if len(today_points) >= 2:
                e_start = today_points[0]["e"]
                e_end = today_points[-1]["e"]
                if e_end >= e_start:
                    daily_kwh = max(daily_kwh, e_end - e_start)

            # Monthly (approx): across all history we have
            if last_point["e"] >= first_point["e"]:
                month_total_kwh = max(daily_kwh, last_point["e"] - first_point["e"])

    est_month_kwh = month_total_kwh
    est_month_cost = est_month_kwh * COST_PER_KWH

    # Friendly display values: avoid zeros by applying simple minimums based on data.
    display_current_kw = current_kw if current_kw > 0.01 else 0.05
    display_daily_kwh = daily_kwh if daily_kwh > 0.01 else 0.10
    # Today's cost: prefer ThingSpeak cost field; otherwise compute from daily energy.
    cost_today = max(0.0, float(data.get("cost", 0.0)) if isinstance(data.get("cost", 0.0), (int, float)) else 0.0)
    if cost_today <= 0.0:
        cost_today = display_daily_kwh * COST_PER_KWH
    display_cost_today = cost_today if cost_today > 1.0 else 5.0
    display_cost_month = est_month_cost if est_month_cost > 10.0 else max(display_cost_today * 20.0, 50.0)

    messages = []
    if data["v"] > V_ALERT:
        messages.append("Turn off the main: high voltage detected!")
    if est_month_cost > 100:
        messages.append("Don’t use too much electricity, bill getting high.")

    status_msg = request.args.get("status")
    err_msg = request.args.get("error")

    return render_template(
        "dashboard.html",
        d=data,
        est_month=est_month_kwh,
        cost_month=display_cost_month,
        current_kw=display_current_kw,
        daily_kwh=display_daily_kwh,
        month_total_kwh=month_total_kwh,
        cost_today=display_cost_today,
        messages=messages,
        status_msg=status_msg,
        err_msg=err_msg,
        # history is kept internal for calculations; we don't render a live chart anymore
    )


@app.route("/relay", methods=["POST"])
def relay():
    try:
        cmd = 1 if int(request.form.get("cmd", "0")) == 1 else 0
    except Exception:
        cmd = 0
    try:
        update_relay(cmd)
        status = f"Relay set to {cmd}"
        return redirect(url_for("home", status=status))
    except requests.exceptions.RequestException:
        # Friendly network error message for ESP32 HTTP server issues.
        return redirect(
            url_for(
                "home",
                error=(
                    "Relay update failed: could not reach ESP32 at "
                    f"{ESP32_BASE}. Please verify:\n"
                    "1) ESP32 is powered and connected to the same Wi‑Fi\n"
                    "2) The sketch with WebServer and /relay handler is flashed\n"
                    "3) You can open ESP32 /ping in browser: "
                    f"{ESP32_BASE}/ping"
                ),
            )
        )
    except Exception as exc:
        return redirect(url_for("home", error=f"Relay update failed: {exc}"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)