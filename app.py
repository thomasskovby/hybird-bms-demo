#!/usr/bin/env python3
"""
Hybird BMS Dashboard - Multi-Account
=====================================
Building Management System dashboard der viser live data fra flere Hybird API-konti.
Understotter flere base URLs og API tokens samtidigt.
"""

from flask import Flask, request, jsonify, render_template
from datetime import datetime
import requests as req
import threading
import time
import os
import base64
import uuid

app = Flask(__name__)

# -- In-memory store (merged from all accounts) --
sites        = {}   # site_id -> {name, address, lat, lng, controllers, account_id}
controllers  = {}   # ctrl_id -> {name, site_id, last_seen, account_id}
breaker_sets = {}   # bs_id -> {name, virtual_meter, breakers, account_id}
breakers     = {}   # breaker_id -> {name, controller_id, bs_ids, account_id}
readings     = {}   # breaker_id -> latest consumption data
bs_readings  = {}   # bs_id -> latest consumption data
history      = {}   # breaker_id -> [consumption entries]
alerts       = []
sync_log     = []

# -- Multi-account config --
# accounts: list of {id, name, base_url, api_token, enabled}
accounts = []

config = {
    "poll_interval_s": 60,
    "auto_poll":       False,
    "last_sync":       None,
    "sync_status":     "idle",
    "sync_message":    "",
}

MAX_HISTORY = 200
MAX_ALERTS  = 100

# -- Auth helper --
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

# -- Prefix keys to avoid collisions between accounts --
def _key(account_id, entity_id):
    return f"{account_id}:{entity_id}"

# -- Fetch data for a single account --
def fetch_account(acct):
    """Hent alle data fra en enkelt Hybird API-konto."""
    base  = acct["base_url"].rstrip("/")
    token = acct["api_token"]
    acct_id   = acct["id"]
    acct_name = acct.get("name", base)

    if not token:
        return False, f"[{acct_name}] API-token mangler"

    hdrs = _headers(token)
    now  = datetime.utcnow().isoformat()
    fetch_mode     = acct.get("fetch_mode", "all")
    selected_sites = acct.get("selected_sites", [])
    selected_bs    = acct.get("selected_breaker_sets", [])

    try:
        # 1) Hent sites med GPS
        r = req.get(f"{base}/api/v1/sites.json", headers=hdrs, timeout=15)
        r.raise_for_status()
        for s in r.json().get("data", []):
            a = s.get("attributes", {})
            key = _key(acct_id, s["id"])
            sites[key] = {
                "id":          key,
                "raw_id":      s["id"],
                "name":        a.get("name", ""),
                "address":     a.get("address", ""),
                "lat":         a.get("latitude"),
                "lng":         a.get("longitude"),
                "controllers": [],
                "account_id":  acct_id,
                "account_name": acct_name,
            }

        # Filter sites if selected_sites is set
        if selected_sites:
            site_keys_to_keep = set()
            for s_id in selected_sites:
                sk = _key(acct_id, s_id)
                if sk in sites:
                    site_keys_to_keep.add(sk)
            # Remove sites not in selection
            for sk in list(sites.keys()):
                if sk.startswith(acct_id + ":") and sk not in site_keys_to_keep:
                    del sites[sk]

        # 2) Hent controllers
        r = req.get(f"{base}/api/v1/controllers.json", headers=hdrs, timeout=15)
        r.raise_for_status()
        for c in r.json().get("data", []):
            a = c.get("attributes", {})
            ctrl_id = c["id"]
            key = _key(acct_id, ctrl_id)
            site_rel = c.get("relationships", {}).get("site", {}).get("data", {})
            raw_site_id = str(site_rel.get("id", "")) if site_rel else ""
            site_key = _key(acct_id, raw_site_id)
            controllers[key] = {
                "id":        key,
                "name":      a.get("name", ""),
                "site_id":   site_key,
                "last_seen": a.get("last_seen_at"),
                "account_id": acct_id,
            }
            if site_key in sites:
                if key not in sites[site_key]["controllers"]:
                    sites[site_key]["controllers"].append(key)

        # 3) Hent breaker sets (filtreret efter fetch_mode)
        r = req.get(f"{base}/api/v1/breaker_sets.json", headers=hdrs, timeout=15)
        r.raise_for_status()
        local_bs_ids = []
        for bs in r.json().get("data", []):
            a = bs.get("attributes", {})
            is_virtual = a.get("virtual_meter", False)
            # Apply fetch_mode filter
            if fetch_mode == "virtual_only" and not is_virtual:
                continue
            if fetch_mode == "non_virtual" and is_virtual:
                continue
            # Apply selected_breaker_sets filter
            if selected_bs and str(bs["id"]) not in [str(x) for x in selected_bs]:
                continue
            key = _key(acct_id, bs["id"])
            breaker_sets[key] = {
                "id":            key,
                "raw_id":        bs["id"],
                "name":          a.get("name", ""),
                "virtual_meter": is_virtual,
                "breakers":      [],
                "account_id":    acct_id,
            }
            local_bs_ids.append((key, bs["id"]))

        # 4) Hent breakers for hvert breaker set
        local_breaker_ids = []
        for bs_key, raw_bs_id in local_bs_ids:
            try:
                r = req.get(f"{base}/api/v1/breaker_sets/{raw_bs_id}/breakers.json",
                            headers=hdrs, timeout=15)
                r.raise_for_status()
                for b in r.json().get("data", []):
                    bid = b["id"]
                    bkey = _key(acct_id, bid)
                    a = b.get("attributes", {})
                    ctrl_rel = b.get("relationships", {}).get("controller", {}).get("data", {})
                    ctrl_id = str(ctrl_rel.get("id", "")) if ctrl_rel else ""

                    if bkey not in breakers:
                        breakers[bkey] = {
                            "id":            bkey,
                            "raw_id":        bid,
                            "name":          a.get("name", f"Breaker {bid}"),
                            "controller_id": _key(acct_id, ctrl_id),
                            "bs_ids":        [],
                            "account_id":    acct_id,
                        }
                        local_breaker_ids.append((bkey, bid))
                    if bs_key not in breakers[bkey]["bs_ids"]:
                        breakers[bkey]["bs_ids"].append(bs_key)
                    if bkey not in breaker_sets[bs_key]["breakers"]:
                        breaker_sets[bs_key]["breakers"].append(bkey)
            except Exception:
                pass

        # 5) Hent consumption for breaker sets
        for bs_key, raw_bs_id in local_bs_ids:
            try:
                r = req.get(f"{base}/api/v1/breaker_sets/{raw_bs_id}/consumption.json",
                            headers=hdrs, timeout=15)
                if r.ok:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        bs_readings[bs_key] = data[0]
            except Exception:
                pass

        # 6) Hent consumption for breakers
        fetched = 0
        for bkey, raw_bid in local_breaker_ids:
            try:
                r = req.get(f"{base}/api/v1/breakers/{raw_bid}/consumption.json",
                            headers=hdrs, timeout=15)
                if r.ok:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        latest = data[0]
                        readings[bkey] = latest
                        if bkey not in history:
                            history[bkey] = []
                        history[bkey].append(latest)
                        if len(history[bkey]) > MAX_HISTORY:
                            history[bkey] = history[bkey][-MAX_HISTORY:]
                        temp = latest.get("avg_temperature_c", 0) or 0
                        if temp > 70:
                            alerts.append({
                                "time": now,
                                "device": breakers[bkey]["name"],
                                "account": acct_name,
                                "msg": f"Hoej temp: {temp} C",
                                "level": "warning",
                            })
                        fetched += 1
            except Exception:
                pass

        if len(alerts) > MAX_ALERTS:
            alerts[:] = alerts[-MAX_ALERTS:]

        acct_sites = sum(1 for s in sites.values() if s["account_id"] == acct_id)
        acct_bs = sum(1 for b in breaker_sets.values() if b["account_id"] == acct_id)
        msg = f"[{acct_name}] OK - {acct_sites} sites, {acct_bs} breaker sets, {fetched} breakers"
        sync_log.insert(0, {"time": now, "msg": msg, "ok": True})
        return True, msg

    except Exception as e:
        msg = f"[{acct_name}] Fejl: {e}"
        now = datetime.utcnow().isoformat()
        sync_log.insert(0, {"time": now, "msg": msg, "ok": False})
        return False, msg

# -- Fetch all accounts --
def fetch_all():
    """Hent data fra alle aktiverede konti."""
    enabled = [a for a in accounts if a.get("enabled", True) and a.get("api_token")]
    if not enabled:
        config["sync_status"] = "idle"
        config["sync_message"] = "Ingen konti konfigureret"
        return False, "Ingen konti konfigureret"

    config["sync_status"]  = "syncing"
    config["sync_message"] = f"Henter fra {len(enabled)} konti..."

    results = []
    for acct in enabled:
        ok, msg = fetch_account(acct)
        results.append((ok, msg))

    if len(sync_log) > 50:
        sync_log[:] = sync_log[:50]

    all_ok = all(r[0] for r in results)
    total_sites = len(sites)
    total_breakers = len(breakers)
    summary = f"Synced {len(enabled)} konti: {total_sites} sites, {total_breakers} breakers"

    config["sync_status"]  = "ok" if all_ok else "error"
    config["sync_message"] = summary
    config["last_sync"]    = datetime.utcnow().isoformat()
    return all_ok, summary

# -- Background poller --
def _poller():
    while True:
        if config["auto_poll"] and accounts:
            fetch_all()
        time.sleep(max(config["poll_interval_s"], 10))

threading.Thread(target=_poller, daemon=True).start()

# -- REST API --
@app.route("/")
def index():
    return render_template("dashboard.html")

# Account management
@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    safe = []
    for a in accounts:
        entry = {k: v for k, v in a.items() if k != "api_token"}
        entry["has_token"] = bool(a.get("api_token"))
        safe.append(entry)
    return jsonify(safe)

@app.route("/api/accounts", methods=["POST"])
deers for hvert breaker set
        local_breaker_ids = []
        for bs_key, raw_bs_id in local_bs_ids:
            try:
                r = req.get(f"{base}/api/v1/breaker_sets/{raw_bs_id}/breakers.json",
                            headers=hdrs, timeout=15)
                r.raise_for_status()
                for b in r.json().get("data", []):
                    bid = b["id"]
                    bkey = _key(acct_id, bid)
                    a = b.get("attributes", {})
                    ctrl_rel = b.get("relationships", {}).get("controller", {}).get("data", {})
                    ctrl_id = str(ctrl_rel.get("id", "")) if ctrl_rel else ""

                    if bkey not in breakers:
                        breakers[bkey] = {
                            "id":            bkey,
                            "raw_id":        bid,
                            "name":          a.get("name", f"Breaker {bid}"),
                            "controller_id": _key(acct_id, ctrl_id),
                            "bs_ids":        [],
                            "account_id":    acct_id,
                        }
                        local_breaker_ids.append((bkey, bid))
                    if bs_key not in breakers[bkey]["bs_ids"]:
                        breakers[bkey]["bs_ids"].append(bs_key)
                    if bkey not in breaker_sets[bs_key]["breakers"]:
                        breaker_sets[bs_key]["breakers"].append(bkey)
            except Exception:
                pass

        # 5) Hent consumption for breaker sets
        for bs_key, raw_bs_id in local_bs_ids:
            try:
                r = req.get(f"{base}/api/v1/breaker_sets/{raw_bs_id}/consumption.json",
                            headers=hdrs, timeout=15)
                if r.ok:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        bs_readings[bs_key] = data[0]
            except Exception:
                pass

        # 6) Hent consumption for breakers
        fetched = 0
        for bkey, raw_bid in local_breaker_ids:
            try:
                r = req.get(f"{base}/api/v1/breakers/{raw_bid}/consumption.json",
                            headers=hdrs, timeout=15)
                if r.ok:
                    data = r.json()
                    if isinstance(data, list) and len(data) > 0:
                        latest = data[0]
                        readings[bkey] = latest
                        if bkey not in history:
                            history[bkey] = []
                        history[bkey].append(latest)
                        if len(history[bkey]) > MAX_HISTORY:
                            history[bkey] = history[bkey][-MAX_HISTORY:]
                        temp = latest.get("avg_temperature_c", 0) or 0
                        if temp > 70:
                            alerts.append({
                                "time": now,
                                "device": breakers[bkey]["name"],
                                "account": acct_name,
                                "msg": f"Hoej temp: {temp} C",
                                "level": "warning",
                            })
                        fetched += 1
            except Exception:
                pass

        if len(alerts) > MAX_ALERTS:
            alerts[:] = alerts[-MAX_ALERTS:]

        acct_sites = sum(1 for s in sites.values() if s["account_id"] == acct_id)
        acct_bs = sum(1 for b in breaker_sets.values() if b["account_id"] == acct_id)
        msg = f"[{acct_name}] OK - {acct_sites} sites, {acct_bs} breaker sets, {fetched} breakers"
        sync_log.insert(0, {"time": now, "msg": msg, "ok": True})
        return True, msg

    except Exception as e:
        msg = f"[{acct_name}] Fejl: {e}"
        now = datetime.utcnow().isoformat()
        sync_log.insert(0, {"time": now, "msg": msg, "ok": False})
        return False, msg

# -- Fetch all accounts --
def fetch_all():
    """Hent data fra alle aktiverede konti."""
    enabled = [a for a in accounts if a.get("enabled", True) and a.get("api_token")]
    if not enabled:
        config["sync_status"] = "idle"
        config["sync_message"] = "Ingen konti konfigureret"
        return False, "Ingen konti konfigureret"

    config["sync_status"]  = "syncing"
    config["sync_message"] = f"Henter fra {len(enabled)} konti..."

    results = []
    for acct in enabled:
        ok, msg = fetch_account(acct)
        results.append((ok, msg))

    if len(sync_log) > 50:
        sync_log[:] = sync_log[:50]

    all_ok = all(r[0] for r in results)
    total_sites = len(sites)
    total_breakers = len(breakers)
    summary = f"Synced {len(enabled)} konti: {total_sites} sites, {total_breakers} breakers"

    config["sync_status"]  = "ok" if all_ok else "error"
    config["sync_message"] = summary
    config["last_sync"]    = datetime.utcnow().isoformat()
    return all_ok, summary

# -- Background poller --
def _poller():
    while True:
        if config["auto_poll"] and accounts:
            fetch_all()
        time.sleep(max(config["poll_interval_s"], 10))

threading.Thread(target=_poller, daemon=True).start()

# -- REST API --
@app.route("/")
def index():
    return render_template("dashboard.html")

# Account management
@app.route("/api/accounts", methods=["GET"])
def get_accounts():
    safe = []
    for a in accounts:
        entry = {k: v for k, v in a.items() if k != "api_token"}
        entry["has_token"] = bool(a.get("api_token"))
        safe.append(entry)
    return jsonify(safe)

@app.route("/api/accounts", methods=["POST"])
deistory")
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
        "accounts_count":     len(accounts),
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
