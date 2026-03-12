# Hybird CTS — Virtual BMS Demo

Et live Building Management System dashboard til demo af Hybird API-integration.
Viser realtidsdata fra Hybird-installationer i Hybirdss brand.

---

## 🚀 Deploy på Render.com (anbefalet)

### Mulighed A — Via GitHub (nemmeste)

1. Opret en gratis konto på [render.com](https://render.com)
2. Push dette projekt til et GitHub repository
3. Klik **"New → Web Service"** i Render
4. Vælg dit repository → Render finder `render.yaml` automatisk
5. Klik **Deploy** — du har en URL inden for ~2 min

### Mulighed B — Manuel upload

1. Gå til [render.com](https://render.com) → New → Web Service
2. Vælg **"Deploy from existing repo"** eller brug **Render CLI**
3. Build command:  `pip install -r requirements.txt`
4. Start command:  `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4`
5. Environment: Python 3

---

## 🖥 Lokal test

```bash
pip install -r requirements.txt
python app.py
# Åbn http://localhost:5000
```

---

## ⚙️ Konfiguration i dashboardet

Øverst i dashboardet er der et **API Config panel** med tre felter:

| Felt | Eksempel | Beskrivelse |
|------|----------|-------------|
| Hybird Base URL | `https://copi.hybird.energy` | Ændr til kundens Hybird-instans |
| API Token | `dGhvbWFzQGh5YmlyZ...` | Basic auth token (base64) |
| Site ID | `760` | Breaker Set ID for den ønskede installation |

Klik **"Hent nu"** for at hente live data.
Aktiver **Auto-poll** for løbende opdatering (30 sek, 1 min, 5 min).

---

## 📡 API endpoints

| Endpoint | Metode | Beskrivelse |
|----------|--------|-------------|
| `/` | GET | Dashboard UI |
| `/api/devices` | GET | Alle målere + seneste reading |
| `/api/devices/<id>/history` | GET | Historik for én måler |
| `/api/summary` | GET | KPI-overblik |
| `/api/alerts` | GET | Aktive alarmer |
| `/api/synclog` | GET | Log over API-kald |
| `/api/config` | GET/POST | Hent/sæt konfiguration |
| `/api/sync` | POST | Trigger manuel sync med Hybird |
| `/api/push` | POST | Push data fra eksternt script |

---

## 🔌 Push data fra script (hybird_bridge.py)

```python
import requests, base64

BMS_URL   = "https://din-app.onrender.com"
HYBIRD_URL = "https://copi.hybird.energy"
TOKEN     = "DIT_TOKEN"
SITE_ID   = "760"

headers = {"Authorization": f"Basic {TOKEN}", "Accept": "application/json"}
r = requests.get(f"{HYBIRD_URL}/api/v1/breaker_sets/{SITE_ID}.json", headers=headers)

breakers = r.json().get("breakers", [])
readings = [{"breaker_id": str(b["id"]), "name": b["name"],
             "power_w": b.get("power_w", 0), "voltage_v": b.get("voltage_v", 230)}
            for b in breakers]

requests.post(f"{BMS_URL}/api/push", json={"readings": readings})
```

---

## 🎨 Farver

Hybird brand palette bruges konsekvent:

- Sand `#F3EEE5`
- Dark Green `#595B3D`
- Light Green `#9B9B6B`
- Orange `#BB6125`
- Green-Grey `#A1B1A4`
- Blue-Grey `#99B0BD`
