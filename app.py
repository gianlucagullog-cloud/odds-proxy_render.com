import os, time, json, logging
import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("odds-proxy")

app = Flask(__name__)
CORS(app, origins="*")

EP_BASE   = "https://api2.eplay24.it/api"
TIMEOUT   = 8          # secondi per chiamata eplay24
CACHE_TTL = 240        # 4 minuti cache

_cache = {}

def cache_get(key):
    e = _cache.get(key)
    if e and time.time() - e["ts"] < CACHE_TTL:
        return e["data"]
    return None

def cache_set(key, data):
    _cache[key] = {"ts": time.time(), "data": data}
    return data

def ep_get(path):
    hit = cache_get(path)
    if hit is not None:
        return hit
    r = requests.get(f"{EP_BASE}{path}", timeout=TIMEOUT,
                     headers={"Accept":"application/json"})
    r.raise_for_status()
    return cache_set(path, r.json())

def ts(s):
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z","+00:00")).timestamp()
    except:
        return 0

SPORT_MAP = {"calcio":"Calcio","tennis":"Tennis","basket":"Basket"}

@app.get("/health")
def health():
    return jsonify({"status":"ok"})

@app.post("/api/scan")
def scan():
    body   = request.get_json(force=True) or {}
    sports = body.get("sports", ["calcio","tennis","basket"])
    limit  = min(int(body.get("limit", 5)), 20)

    # Fetch palinsesto
    try:
        all_ev = ep_get("/Palinsesto/GetAllEventsPrematch")
    except Exception as e:
        log.error("GetAllEventsPrematch: %s", e)
        return jsonify({"error": str(e), "events": []}), 502

    now = time.time()
    results = []

    for sport_key in sports:
        sport_name = SPORT_MAP.get(sport_key.lower(), "Calcio")
        events = [
            e for e in all_ev
            if e.get("Sport_Desc") == sport_name
            and ts(e.get("Match_Time","")) > now + 900
        ]
        events.sort(key=lambda e: ts(e.get("Match_Time","")))
        events = events[:limit]

        for ev in events:
            # Mercato base sempre disponibile (1X2 con quote da palinsesto)
            markets = [{
                "name":  "Esito Finale 1X2",
                "linee": ["1","X","2"],
                "tab":   "Principali",
                "lineaNum": None
            }]

            # Prova a prendere più mercati — ma con timeout corto
            try:
                r = requests.post(
                    f"{EP_BASE}/Palinsesto/GetEventePalinsesto",
                    json={"match_id": ev["Match_Id"],
                          "Group_id": ev.get("Group_id", 0)},
                    timeout=TIMEOUT,
                    headers={"Accept":"application/json",
                             "Content-Type":"application/json"}
                )
                if r.ok:
                    data = r.json()
                    rows = data if isinstance(data, list) else \
                           data.get("ResponseData", []) if isinstance(data, dict) else []
                    if rows:
                        markets = parse_markets(rows)
            except Exception as e:
                log.warning("GetEventePalinsesto %s: %s", ev["Match_Id"], e)

            results.append({
                "eventId":   str(ev["Match_Id"]),
                "label":     ev.get("label",""),
                "sport":     sport_key,
                "group":     ev.get("Group_Name",""),
                "time":      ev.get("Match_Time",""),
                "scannedAt": int(time.time()*1000),
                "sites": {
                    "Eplay24": {
                        "markets": markets,
                        "tabs": list({m["tab"] for m in markets})
                    }
                }
            })

    return jsonify({"meta":{"sports":sports,"limit":limit}, "events":results, "errors":[]})

def parse_markets(rows):
    seen = set()
    out  = []
    for row in rows:
        name = (row.get("DescTipoScommessa") or
                row.get("Nome") or
                row.get("name") or "").strip()
        if not name:
            continue
        linea = str(row.get("Linea") or row.get("linea") or "-")
        tab   = row.get("Categoria") or "Principali"
        key   = f"{name}|{linea}"
        if key in seen:
            continue
        seen.add(key)

        outcomes = []
        for k in ["Quota1","QuotaX","Quota2"]:
            try:
                if float(row.get(k,0)) > 1.0:
                    outcomes.append(k.replace("Quota",""))
            except:
                pass

        out.append({
            "name":     name,
            "linee":    outcomes if outcomes else [linea],
            "lineaNum": float(linea) if _is_num(linea) else None,
            "tab":      tab
        })
    return out or [{"name":"Esito Finale 1X2","linee":["1","X","2"],"tab":"Principali","lineaNum":None}]

def _is_num(v):
    try: float(str(v)); return True
    except: return False

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
