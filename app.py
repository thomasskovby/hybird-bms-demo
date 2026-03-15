#!/usr/bin/env python3
"""
Hybird BMS Dashboard
====================
Building Management System dashboard der viser live data fra Hybird API.
Henter sites, controllers, breaker sets og consumption data.
"""

from flask import Flask, request, jsonify, render_template
from datetime import datetime
import requests as req
import threading
import time
import os
import base64

app = Flask(__name__)

# ── In-memory store ───────────────────────────────────────────────────
sites       = {}   # site_id -> {name, address, lat, lng, controllers: [...]}
controllers = {}   # ctrl_id -> {name, site_id, last_seen}
breaker_sets = {}  # bs_id -> {name, virtual_meter, breakers: [...]}
breakers    = {}   # breaker_id -> {name, controller_id, bs_ids: [...]}
readings    = {}   # breaker_id -> latest consumption data (per-fase)
bs_readings = {}   # bs_id -> latest consumption data (aggregeret)
history     = {}   # breaker_id -> [consumption entries]
alerts      = []
sync_log    = []

# ── Live config ───────────────────────────────────────────────────────
config = {
    "hybird_base_url":  "https://demo.hybird.energy",
    "hybird_api_token": "",
    "poll_interval_s":  60,
    "auto_poll":        False,
    "last_sync":        None,
    "sync_status":      "idle",
    "sync_message":     "",
}

MAX_HISTORY = 200
MAX_ALERTS  = 100

# ── Auth helper ───────────────────────────────────────────────────────
def _auth_header(token):
    try:
        decoded = base64.b64decode(token).decode('utf-8')
        if ':' in decoded and '@' in decoded:
            return f"Basic {token}"
    except Exception:
        pass
    return f"Bearer {token}"

def _headers(token):
    return {
        "Authorization": _auth_header(token),
        "Accept": "application/json",
    }

# ── API fetch ─────────────────────────────────────────────────────────
def fetch_all():
    """Hent alle data fra Hybird API."""
    base  = config["hybird_base_url"].rstrip("/")
    token = config["hybird_api_token"]
    if not token:
        return False, "API-token mangler"

    hdrs = _headers(token)
    config["sync_status"]  = "syncing"
    config["sync_message"] = "Henter data fra Hybird API..."
    now = datetime.utcnow().isoformat()

    try:
        # 1) Hent sites med GPS
        r = req.get(f"{base}/api/v1/sites.json", headers=hdrs, timeout=10)
        r.raise_for_status()
        for s in r.json().get("data", []):
            a = s.get("attributes", {})
            sites[s["id"]] = {
                "id":      s["id"],
                "name":    a.get("name", ""),
                "address": a.get("address", ""),
                "lat":     a.get("latitude"),
                "lng":     a.get("longitude"),
                "controllers": [],
            }

        # 2) Hent controllers (med online-status)
        r = req.get(f"{base}/api/v1/controllers.json", headers=hdrs, timeout=10)
        r.raise_for_status()
        for c in r.json().get("data", []):
            a = c.get("attributes", {})
            ctrl_id = c["id"]
            site_rel = c.get("relationships", {}).get("site", {}).get("data", {})
            site_id = str(site_rel.get("id", "")) if site_rel else ""
            controllers[ctrl_id] = {
                "id":        ctrl_id,
                "name":      a.get("name", ""),
                "site_id":   site_id,
                "last_seen": a.get("last_seen_at"),
            }
            if site_id in sites:
                if ctrl_id not in sites[site_id]["controllers"]:
                    sites[site_id]["controllers"].append(ctrl_id)

        # 3) Hent alle breaker sets
        r = req.get(f"{base}/api/v1/breaker_sets.json", headers=hdrs, timeout=10)
        r.raise_for_status()
        for bs in r.json().get("data", []):
            a = bs.get("attributes", {})
            breaker_sets[bs["id"]] = {
                "id":            bs["id"],
                "name":          a.get("name", ""),
                "virtual_meter": a.get("virtual_meter", False),
                "breakers":      [],
            }

        # 4) Hent breakers for hvert breaker set
        for bs_id in list(breaker_sets.keys()):
            try:
                r = req.get(f"{base}/api/v1/breaker_sets/{bs_id}/breakers.json",
                            headers=hdrs, timeout=10)
                r.raise_for_status()
                for b in r.json().get("data", []):
                    bid = b["id"]
                    a = b.get("attributes", {})
                    ctrl_rel = b.get("relationships", {}).get("controller", {}).get("data", {})
                    ctrl_id = str(ctrl_rel.get("id", "")) if ctrl_rel else ""

                    if bid not in breakers:
                        breakers[bid] = {
                            "id":            bid,
                            "name":          a.get("name", f"Breaker {bid}"),
                            "controller_id": ctrl_id,
                            "bs_ids":        [],
                        }
                    if bs_id not in breakers[bid]["bs_ids"]:
                        breakers[bid]["bs_ids"].append(bs_id)
                    if bid not in breaker_sets[bs_id]["breakers"]:
                        breaker_sets[bs_id]["breakers"].append(bid)
            except Exception:
                pass

        # 5) Hent consumption data for hvert breaker set (aggregeret per-fase)
        for bs_id in list(breaker_sets.keys()):
            try:
                r = req.get(f"{base}/api/v1/breaker_sets/{bs_id}/consumption.json",
                            headers=hdrs, timeout=10)
                if r.ok:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        bs_readings[bs_id] = data[0]
            except Exception:
                pass

        # 6) Hent consumption data for hver breaker (per-fase detaljer)
        fetched = 0
        for bid in list(breakers.keys()):
            try:
                r = req.get(f"{base}/api/v1/breakers/{bid}/consumption.json",
                            headers=hdrs, timeout=10)
                if r.ok:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        latest = data[0]
                        readings[bid] = latest
                        if bid not in history:
                            history[bid] = []
                        history[bid].append(latest)
                        if len(history[bid]) > MAX_HISTORY:
                            history[bid] = history[bid][-MAX_HISTORY:]

                        temp = latest.get("avg_temperature_c", 0) or 0
                        if temp > 70:
                            alerts.append({
                                "time": now,
                                "device": breakers[bid]["name"],
                                "msg": f"Hoej temp: {temp} C",
                                "level": "warning"
                            })
                        fetched += 1
            except Exception:
                pass

        if len(alerts) > MAX_ALERTS:
            alerts[:] = alerts[-MAX_ALERTS:]

        msg = f"OK — {len(sites)} sites, {len(breaker_sets)} breaker sets, {fetched} breakers med data"
        sync_log.insert(0, {"time": now, "msg": msg, "ok": True})
        if len(sync_log) > 50:
            sync_log.pop()
        config["sync_status"]  = "ok"
        config["sync_message"] = msg
        config["last_sync"]    = now
        return True, msg

    except Exception as e:
        msg = f"Fejl: {e}"
        sync_log.insert(0, {"time": now, "msg": msg, "ok": False})
        config["sync_status"]  = "error"
        config["sync_message"] = msg
        return False, msg

# ── Background poller ─────────────────────────────────────────────────
def _poller():
    while True:
        if config["auto_poll"] and config["hybird_api_token"]:
            fetch_all()
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
    allowed = ["hybird_base_url", "hybird_api_token", "poll_interval_s", "auto_poll"]
    for k in allowed:
        if k in data:
            config[k] = data[k]
    return jsonify({"ok": True})

@app.route("/api/sync", methods=["POST"])
def manual_sync():
    ok, msg = fetch_all()
    return jsonify({"ok": ok, "message": msg})

@app.route("/api/sites")
def get_sites():
    return jsonify(list(sites.values()))

@app.route("/api/breaker_sets")
def get_breaker_sets_api():
    out = []
    for bs_id, bs in breaker_sets.items():
        entry = dict(bs)
        entry["consumption"] = bs_readings.get(bs_id, {})
        out.append(entry)
    return jsonify(out)

@app.route("/api/breakers")
def get_breakers_api():
    out = []
    for bid, b in breakers.items():
        entry = dict(b)
        r = readings.get(bid, {})
        entry["consumption"] = r
        ctrl_id = b.get("controller_id", "")
        ctrl = controllers.get(ctrl_id, {})
        entry["controller_name"] = ctrl.get("name", "")
        site_id = ctrl.get("site_id", "")
        site = sites.get(site_id, {})
        entry["site_id"]   = site_id
        entry["site_name"] = site.get("name", "")
        entry["lat"]       = site.get("lat")
        entry["lng"]       = site.get("lng")
        out.append(entry)
    return jsonify(out)

@app.route("/api/breakers/<breaker_id>/history")
def get_breaker_history(breaker_id):
    limit = int(request.args.get("limit", 60))
    return jsonify(history.get(breaker_id, [])[-limit:])

@app.route("/api/summary")
def get_summary():
    total_w = sum(r.get("avg_total_active_power_w", 0) or 0 for r in readings.values())
    total_kwh = sum(r.get("consumption_kwh", 0) or 0 for r in readings.values())
    online_ctrls = sum(1 for c in controllers.values() if c.get("last_seen"))
    return jsonify({
        "total_power_w":      round(total_w, 1),
        "total_kwh":          round(total_kwh, 2),
        "sites_count":        len(sites),
        "controllers_count":  len(controllers),
        "controllers_online": online_ctrls,
        "breaker_sets_count": len(breaker_sets),
        "breakers_count":     len(breakers),
        "alerts_count":       len(alerts),
        "sync_status":        config["sync_status"],
        "sync_message":       config["sync_message"],
        "last_sync":          config["last_sync"],
    })

@app.route("/api/alerts")
def get_alerts():
    return jsonify(alerts[-50:])

@app.route("/api/synclog")
def get_synclog():
    return jsonify(sync_log[:20])

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
