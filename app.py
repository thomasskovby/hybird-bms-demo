#!/usr/bin/env python3
"""
Hybird Mock CTS/BMS Server
===========================
Virtual Building Management System til demo og integration-test.
Konfigurer Hybird API-forbindelsen direkte fra dashboardet.
"""

from flask import Flask, request, jsonify, render_template
from datetime import datetime, timedelta
import requests as req
import random
import threading
import time
import os

app = Flask(__name__)

# ── In-memory store ───────────────────────────────────────────────────
devices   = {}   # breaker_id -> device info + latest reading
history   = {}   # breaker_id -> [readings]
alerts    = []
sync_log  = []   # log af seneste API-kald

# ── Live config (kan aendres fra UI uden restart) ─────────────────────
config = {
    "hybird_base_url":  "https://copi.hybird.energy",
    "hybird_api_token": "",        # Basic auth token (base64)
    "site_id":          "",        # site id
    "breaker_set_id":   "",        # breaker_set_id
    "poll_interval_s":  30,
    "auto_poll":        False,
    "last_sync":        None,
    "sync_status":      "idle",    # idle | syncing | ok | error
    "sync_message":     "",
}

MAX_HISTORY = 500   # pr. device
MAX_ALERTS  = 100

# ── Demo seed ─────────────────────────────────────────────────────────
DEMO = [
    {"id":"demo-001","name":"Ventilation Hoved","phase":"L1","location":"Kaelder","base_w":1800},
    {"id":"demo-002","name":"Belysning Kontor","phase":"L2","location":"1. sal","base_w":450},
    {"id":"demo-003","name":"Koekken Ovn","phase":"L3","location":"Koekken","base_w":3200},
    {"id":"demo-004","name":"Fryserum","phase":"L1","location":"Lager","base_w":800},
    {"id":"demo-005","name":"Varmepumpe","phase":"L2","location":"Tag","base_w":2400},
]

def _seed():
    now = datetime.utcnow()
    for d in DEMO:
        devices[d["id"]] = {
            "id":       d["id"],
            "name":     d["name"],
            "phase":    d["phase"],
            "location": d["location"],
            "source":   "demo",
            "online":   True,
        }
        history[d["id"]] = []
        for i in range(144):   # 10-min intervals, 24h
            ts = now - timedelta(minutes=10*(144-i))
            hr = ts.hour
            fac = 0.25 if hr < 6 or hr > 22 else (1.0 if 8 <= hr <= 18 else 0.55)
            pw = round(d["base_w"] * fac * random.uniform(0.88,1.12), 1)
            history[d["id"]].append({
                "timestamp": ts.isoformat(),
                "power_w":   pw,
                "voltage_v": round(random.uniform(228,232),1),
                "current_a": round(pw/230,2),
                "temp_c":    round(random.uniform(32,52),1),
            })

_seed()

# ── Hybird API fetch ──────────────────────────────────────────────────
def fetch_from_hybird():
    """Hent live data fra Hybird API og gem i devices/history."""
    base      = config["hybird_base_url"].rstrip("/")
    token     = config["hybird_api_token"]
    site_id   = config["site_id"].strip()
    bs_id     = config["breaker_set_id"].strip()

    if not token or not bs_id:
        return False, "API-token eller Breaker Set ID mangler"

    # Detect auth type: if token looks like base64-encoded email:token, use Basic
    # Otherwise use Bearer for raw API tokens
    import base64
    try:
        decoded = base64.b64decode(token).decode('utf-8')
        is_basic = ':' in decoded and '@' in decoded
    except Exception:
        is_basic = False

    if is_basic:
        auth_header = f"Basic {token}"
    else:
        auth_header = f"Bearer {token}"

    headers = {
        "Authorization": auth_header,
        "Accept":        "application/json",
        "Content-Type":  "application/json",
    }

    config["sync_status"]  = "syncing"
    config["sync_message"] = "Kontakter Hybird API..."

    try:
        # Hent breakers for dette breaker set (JSON:API format)
        url = f"{base}/api/v1/breaker_sets/{bs_id}/breakers.json"

        r = req.get(url, headers=headers, timeout=10)
        r.raise_for_status()
        resp = r.json()

        # JSON:API: data[] = breakers, included[] = latest_measurements
        breaker_list = resp.get("data", [])
        included = resp.get("included", [])

        # Byg lookup for measurements: breaker_id -> measurement attributes
        measurements = {}
        for inc in included:
            if inc.get("type") == "breaker_latest_measurement":
                measurements[str(inc.get("id", ""))] = inc.get("attributes", {})

        fetched = 0
        now = datetime.utcnow().isoformat()

        for b in breaker_list:
            bid  = str(b.get("id", ""))
            attrs = b.get("attributes", {})
            name = attrs.get("name") or f"Breaker {bid}"
            if not bid:
                continue

            # Hent measurement data fra included
            meas = measurements.get(bid, {})
            pw   = float(meas.get("total_active_power_w") or 0)
            volt = float(meas.get("phase_a_voltage_v") or 230)
            amp  = float(meas.get("phase_a_current_a") or pw/max(volt,1))
            temp = float(meas.get("temperature_c") or 0)
            state = meas.get("state", "unknown")

            if bid not in devices:
                devices[bid] = {
                    "id":       bid,
                    "name":     name,
                    "phase":    "L1",
                    "location": bs_id,
                    "source":   "hybird",
                    "online":   state != "offline",
                }
                history[bid] = []

            reading = {
                "timestamp": meas.get("measured_at", now),
                "power_w":   pw,
                "voltage_v": volt,
                "current_a": amp,
                "temp_c":    temp,
            }
            devices[bid].update({"online": state != "offline", "last_seen": now, "name": name})
            history[bid].append(reading)
            if len(history[bid]) > MAX_HISTORY:
                history[bid] = history[bid][-MAX_HISTORY:]
            if temp > 70:
                alerts.append({"time":now,"device":name,"msg":f"Hoej temp: {temp} grader C","level":"warning"})
            fetched += 1

        msg = f"OK — {fetched} maelere hentet fra {url}"
        sync_log.insert(0, {"time":now,"msg":msg,"ok":True})
        if len(sync_log) > 50:
            sync_log.pop()
        config["sync_status"]  = "ok"
        config["sync_message"] = msg
        config["last_sync"]    = now
        return True, msg

    except Exception as e:
        msg = f"Fejl: {e}"
        now = datetime.utcnow().isoformat()
        sync_log.insert(0, {"time":now,"msg":msg,"ok":False})
        config["sync_status"]  = "error"
        config["sync_message"] = msg
        return False, msg

# ── Background poller ─────────────────────────────────────────────────
def _poller():
    while True:
        if config["auto_poll"] and config["hybird_api_token"] and config["breaker_set_id"]:
            fetch_from_hybird()
        time.sleep(max(config["poll_interval_s"], 10))

threading.Thread(target=_poller, daemon=True).start()

# ── REST API ──────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("dashboard.html")

@app.route("/api/config", methods=["GET"])
def get_config():
    safe = {k: v for k, v in config.items() if k != "hybird_api_token"}
    safe["has_token"] = bool(config["hybird_api_token"])
    return jsonify(safe)

@app.route("/api/config", methods=["POST"])
def set_config():
    data = request.get_json() or {}
    allowed = ["hybird_base_url","hybird_api_token","site_id","breaker_set_id","poll_interval_s","auto_poll"]
    for k in allowed:
        if k in data:
            config[k] = data[k]
    return jsonify({"ok": True})

@app.route("/api/sync", methods=["POST"])
def manual_sync():
    ok, msg = fetch_from_hybird()
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/devices")
def get_devices():
    out = []
    for did, dev in devices.items():
        h = history.get(did, [])
        latest = h[-1] if h else {}
        out.append({**dev, **latest, "readings_count": len(h)})
    return jsonify(out)

@app.route("/api/devices/<device_id>/history")
def get_device_history(device_id):
    limit = int(request.args.get("limit", 60))
    return jsonify(history.get(device_id, [])[-limit:])

@app.route("/api/summary")
def get_summary():
    total_w = 0
    online  = 0
    for did, dev in devices.items():
        h = history.get(did,[])
        if h:
            total_w += h[-1].get("power_w",0)
            online  += 1
    total_kwh = sum(
        sum(r.get("power_w",0) for r in history.get(did,[]))*(10/60/1000)
        for did in devices
    )
    return jsonify({
        "total_power_w":  round(total_w,1),
        "total_kwh":      round(total_kwh,2),
        "devices_online": online,
        "devices_total":  len(devices),
        "alerts_count":   len(alerts),
        "sync_status":    config["sync_status"],
        "sync_message":   config["sync_message"],
        "last_sync":      config["last_sync"],
    })

@app.route("/api/alerts")
def get_alerts():
    return jsonify(alerts[-50:])

@app.route("/api/synclog")
def get_synclog():
    return jsonify(sync_log[:20])

# Modtag data fra eksternt script (hybird_bridge.py)
@app.route("/api/push", methods=["POST"])
def push_readings():
    data  = request.get_json() or {}
    items = data.get("readings",[])
    now   = datetime.utcnow().isoformat()
    for r in items:
        bid  = str(r.get("breaker_id","unknown"))
        name = r.get("name", bid)
        if bid not in devices:
            devices[bid] = {"id":bid,"name":name,"phase":r.get("phase","L?"),
                            "location":r.get("location",""),"source":"push","online":True}
            history[bid] = []
        reading = {"timestamp":now,"power_w":r.get("power_w",0),"voltage_v":r.get("voltage_v",230),
                   "current_a":r.get("current_a",0),"temp_c":r.get("temp_c",0)}
        history[bid].append(reading)
        if len(history[bid]) > MAX_HISTORY:
            history[bid] = history[bid][-MAX_HISTORY:]
        devices[bid]["last_seen"] = now
    return jsonify({"ok":True,"stored":len(items)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
