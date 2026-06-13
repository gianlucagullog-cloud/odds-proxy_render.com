"""
odds_proxy.py — Proxy server per Odds Gap Scanner
Gira su Render.com (piano gratuito) e fa da ponte tra
il sito GitHub Pages e le API di eplay24.it

Endpoints esposti:
  GET  /health                          → status check
  GET  /api/events?sport=calcio&limit=5 → lista eventi prematch
  GET  /api/markets?match_id=123        → mercati di una partita
  POST /api/scan                        → scansione multipla
"""

import os
import time
import logging
from functools import lru_cache

import requests
from flask import Flask, jsonify, request
from flask_cors import CORS

# ----------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("odds-proxy")

app = Flask(__name__)

# Permette richieste da qualsiasi origine (necessario per GitHub Pages)
CORS(app, origins="*")

# ----------------------------------------------------------------
# Config
# ----------------------------------------------------------------
EP_BASE    = "https://api2.eplay24.it/api"
TIMEOUT    = 12          # secondi per ogni richiesta verso eplay24
CACHE_TTL  = 300         # 5 minuti di cache in memoria

SPORT_NAMES = {
    "calcio":  "Calcio",
    "tennis":  "Tennis",
    "basket":  "Basket",
    "football":"Calcio",
}

# ----------------------------------------------------------------
# Cache semplice in memoria (evita di hammering eplay24)
# ----------------------------------------------------------------
_cache: dict = {}

def cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < CACHE_TTL:
        return entry["data"]
    return None

def cache_set(key: str, data):
    _cache[key] = {"ts": time.time(), "data": data}
    return data

def ep_get(path: str, **params):
    """GET verso api2.eplay24.it con cache."""
    key = path + str(sorted(params.items()))
    hit = cache_get(key)
    if hit is not None:
        return hit
    url = f"{EP_BASE}{path}"
    r = requests.get(url, params=params, timeout=TIMEOUT,
                     headers={"Accept": "application/json"})
    r.raise_for_status()
    return cache_set(key, r.json())

def ep_post(path: str, body: dict):
    """POST verso api2.eplay24.it con cache."""
    import json
    key = path + json.dumps(body, sort_keys=True)
    hit = cache_get(key)
    if hit is not None:
        return hit
    url = f"{EP_BASE}{path}"
    r = requests.post(url, json=body, timeout=TIMEOUT,
                      headers={"Accept": "application/json",
                               "Content-Type": "application/json"})
    r.raise_for_status()
    return cache_set(key, r.json())

# ----------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------
def filter_future_events(events: list, sport_name: str, limit: int) -> list:
    now = time.time()
    future = [
        e for e in events
        if e.get("Sport_Desc") == sport_name
        and _ts(e.get("Match_Time", "")) > now + 900  # almeno 15 min nel futuro
    ]
    future.sort(key=lambda e: _ts(e.get("Match_Time", "")))
    return future[:limit]

def _ts(dt_str: str) -> float:
    """ISO datetime → unix timestamp."""
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0

def extract_markets(rows, match_id=None) -> list:
    """
    Converte le righe API eplay24 in lista di mercati normalizzati.
    Supporta diversi formati risposta dell'API.
    """
    if not rows:
        return []

    # Formato: array diretto
    if isinstance(rows, list):
        data = rows
    # Formato: { ResponseData: [...] }
    elif isinstance(rows, dict) and isinstance(rows.get("ResponseData"), list):
        data = rows["ResponseData"]
    else:
        data = []

    if match_id:
        data = [r for r in data if not r.get("Match_Id") or r.get("Match_Id") == match_id]

    markets = []
    seen = set()

    for row in data:
        name = (
            row.get("DescTipoScommessa")
            or row.get("desc_tipo_scommessa")
            or row.get("Nome")
            or row.get("name")
            or ""
        ).strip()

        linea = row.get("Linea") or row.get("linea") or row.get("Line") or "-"
        tab   = row.get("Categoria") or row.get("tab") or "Principali"
        quota = row.get("Quota1") or row.get("quota") or row.get("Odd")

        # Outcomes standard
        outcomes = []
        for k in ["Quota1", "QuotaX", "Quota2", "Quota3"]:
            if row.get(k) and float(row[k]) > 1.0:
                outcomes.append(k.replace("Quota", ""))

        if not name:
            continue

        key = f"{name}|{linea}"
        if key in seen:
            continue
        seen.add(key)

        markets.append({
            "name":   name,
            "linee":  outcomes if outcomes else [str(linea)],
            "lineaNum": float(linea) if _is_num(linea) else None,
            "tab":    tab,
            "quota":  float(quota) if quota and _is_num(quota) else None,
        })

    return markets

def _is_num(v) -> bool:
    try:
        float(str(v).replace(",", "."))
        return True
    except Exception:
        return False

# ----------------------------------------------------------------
# Routes
# ----------------------------------------------------------------

@app.get("/health")
def health():
    return jsonify({"status": "ok", "cache_entries": len(_cache)})


@app.get("/api/events")
def get_events():
    """
    GET /api/events?sport=calcio&limit=5
    Ritorna i prossimi N eventi prematch del sport richiesto.
    """
    sport_key  = request.args.get("sport", "calcio").lower()
    limit      = min(int(request.args.get("limit", 5)), 50)
    sport_name = SPORT_NAMES.get(sport_key, "Calcio")

    try:
        all_events = ep_get("/Palinsesto/GetAllEventsPrematch")
    except Exception as e:
        log.error("GetAllEventsPrematch failed: %s", e)
        return jsonify({"error": str(e)}), 502

    events = filter_future_events(all_events, sport_name, limit)

    return jsonify({
        "sport":  sport_key,
        "total":  len(events),
        "events": [
            {
                "Match_Id":    e["Match_Id"],
                "label":       e.get("label", ""),
                "Sport_Desc":  e.get("Sport_Desc", ""),
                "Group_Name":  e.get("Group_Name", ""),
                "Category_Desc": e.get("Category_Desc", ""),
                "Group_id":    e.get("Group_id"),
                "Match_Time":  e.get("Match_Time", ""),
                "Sport_Id":    e.get("Sport_Id", 1),
            }
            for e in events
        ],
    })


@app.get("/api/markets")
def get_markets():
    """
    GET /api/markets?match_id=12345&group_id=-965
    Ritorna i mercati di una singola partita.
    """
    match_id = request.args.get("match_id")
    group_id = request.args.get("group_id")

    if not match_id:
        return jsonify({"error": "match_id richiesto"}), 400

    match_id = int(match_id)
    markets  = []
    tried    = []

    # Tentativo 1: endpoint specifico per evento
    for path, body in [
        ("/Palinsesto/GetEventePalinsesto",
         {"match_id": match_id, "Group_id": int(group_id) if group_id else 0}),
        ("/Palinsesto/GetPalinsestoCore",
         {"match_id": match_id}),
    ]:
        tried.append(path)
        try:
            data = ep_post(path, body)
            mkts = extract_markets(data, match_id)
            if mkts:
                markets = mkts
                log.info("markets via %s: %d rows", path, len(markets))
                break
        except Exception as e:
            log.warning("%s failed: %s", path, e)

    # Tentativo 2: palinsesto del gruppo
    if not markets and group_id:
        tried.append("/Palinsesto/GetPalinsestoGruppo")
        try:
            data = ep_post("/Palinsesto/GetPalinsestoGruppo",
                           {"group_id": int(group_id), "sport_id": 1})
            markets = extract_markets(data, match_id)
            log.info("markets via gruppo: %d rows", len(markets))
        except Exception as e:
            log.warning("GetPalinsestoGruppo failed: %s", e)

    # Tentativo 3: GetAllEventsPrematch ha già i dati base (sempre disponibile)
    if not markets:
        tried.append("GetAllEventsPrematch-fallback")
        try:
            all_ev = ep_get("/Palinsesto/GetAllEventsPrematch")
            ev = next((e for e in all_ev if e.get("Match_Id") == match_id), None)
            if ev:
                # Almeno il mercato 1X2 con le quote base
                markets = [
                    {"name": "Esito Finale 1X2", "linee": ["1","X","2"],
                     "tab": "Principali", "lineaNum": None, "quota": None}
                ]
        except Exception as e:
            log.warning("fallback failed: %s", e)

    return jsonify({
        "match_id": match_id,
        "markets_count": len(markets),
        "tried_endpoints": tried,
        "markets": markets,
    })


@app.post("/api/scan")
def scan():
    """
    POST /api/scan
    Body: { "sports": ["calcio","tennis"], "limit": 5 }
    Ritorna eventi + mercati in un unico payload pronto per app.js
    """
    body   = request.get_json(force=True) or {}
    sports = body.get("sports", ["calcio", "tennis", "basket"])
    limit  = min(int(body.get("limit", 5)), 20)

    try:
        all_events = ep_get("/Palinsesto/GetAllEventsPrematch")
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    results = []
    errors  = []

    for sport_key in sports:
        sport_name = SPORT_NAMES.get(sport_key.lower(), "Calcio")
        events     = filter_future_events(all_events, sport_name, limit)

        for ev in events:
            match_id = ev["Match_Id"]
            markets  = []

            for path, body_req in [
                ("/Palinsesto/GetEventePalinsesto",
                 {"match_id": match_id, "Group_id": ev.get("Group_id", 0)}),
                ("/Palinsesto/GetPalinsestoCore", {"match_id": match_id}),
            ]:
                try:
                    data = ep_post(path, body_req)
                    mkts = extract_markets(data, match_id)
                    if mkts:
                        markets = mkts
                        break
                except Exception:
                    pass

            # Fallback: dati base sempre disponibili
            if not markets:
                markets = [
                    {"name": "Esito Finale 1X2", "linee": ["1","X","2"],
                     "tab": "Principali", "lineaNum": None, "quota": None}
                ]

            results.append({
                "eventId":   str(match_id),
                "label":     ev.get("label", ""),
                "sport":     sport_key,
                "group":     ev.get("Group_Name", ""),
                "time":      ev.get("Match_Time", ""),
                "scannedAt": int(time.time() * 1000),
                "sites": {
                    "Eplay24": {
                        "markets": markets,
                        "tabs":    list({m["tab"] for m in markets}),
                    }
                },
            })

    return jsonify({
        "meta":   {"scannedAt": int(time.time() * 1000), "sports": sports, "limit": limit},
        "events": results,
        "errors": errors,
    })


# ----------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
