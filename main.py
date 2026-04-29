import json
import os, uuid, random, time, signal, base64, io
from io import BytesIO
from datetime import datetime, timedelta

from fastapi import FastAPI, File, UploadFile, Form, Query, HTTPException, Body, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from PIL import Image, ImageOps
# from rembg import remove

import firebase_admin
from firebase_admin import credentials, firestore, storage
from zoneinfo import ZoneInfo
from hashlib import sha256
from pillow_heif import register_heif_opener
register_heif_opener()

try:
    import openai
except Exception:
    openai = None

try:
    from google.cloud import vision
except Exception:
    vision = None

# ====================
# Funzioni helper
# ====================
def normalize_stile(s: str | None) -> str:
    if not s:
        return ""
    return str(s).strip().lower()


def normalize_stagione(s: str | None) -> str:
    if not s:
        return ""
    return str(s).strip().lower()

def _slim(it: dict | None):
    """Rende l'oggetto capo JSON-serializzabile (niente Timestamp ecc.)."""
    if not it:
        return None
    return {
        "id": it.get("docId") or it.get("id"),
        "categoria": it.get("categoria"),
        "nome": it.get("nome"),
        "stile": it.get("stile"),
        "colore": it.get("colore"),
        "imageUrl": it.get("imageUrl"),
    }

# ------------------------------
# 4) ENV & FLAGS
# ------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

GOOGLE_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
FIREBASE_BUCKET = os.getenv("FIREBASE_BUCKET", "")

# Modalità community: TRUE => solo armadi pubblici; gli altri endpoint community rispondono 503
COMMUNITY_PUBLIC_ONLY = os.getenv("COMMUNITY_PUBLIC_ONLY", "true").lower() == "true"

# ------------------------------
# 5) INIT OpenAI
# ------------------------------
if openai and OPENAI_API_KEY:
    openai.api_key = OPENAI_API_KEY

# ------------------------------
# 6) INIT Firebase
# ------------------------------
if not firebase_admin._apps:
    cred = credentials.Certificate(GOOGLE_CREDENTIALS)
    firebase_admin.initialize_app(cred, {"storageBucket": FIREBASE_BUCKET})

db = firestore.client()
bucket = storage.bucket(FIREBASE_BUCKET)

# ------------------------------
# 7) INIT Google Vision (SafeSearch)
# ------------------------------
vision_client = None
if vision is not None:
    try:
        vision_client = vision.ImageAnnotatorClient()
    except Exception:
        vision_client = None

# ------------------------------
# 8) UTILS — Funzioni AI + SafeSearch
# ------------------------------

# Limite free configurabile via env (default = 8 outfit al giorno)
FREE_OUTFIT_LIMIT = int(os.getenv("FREE_OUTFIT_LIMIT", "8"))
QUICKPAIR_FREE_DAILY_LIMIT = int(os.getenv("QUICKPAIR_FREE_DAILY_LIMIT", "2"))  # max 2 suggerimenti/giorno per FREE


def check_and_increment_quota_or_raise(userId: str, feature: str, limit_free: int | None = None):
    """
    Controlla e incrementa la quota giornaliera per una feature.
    Se superata, solleva HTTPException 429 con payload JSON strutturato.
    """
    limit = limit_free if limit_free is not None else (
        FREE_OUTFIT_LIMIT if feature == "outfit" else 999999
    )

    ref = db.collection("quota").document(userId)
    doc = ref.get()
    today = datetime.utcnow().date().isoformat()
    data = doc.to_dict() if doc.exists else {}
    used = data.get(today, {}).get(feature, 0)

    reset_at_utc = f"{today}T23:59:59Z"

    if used >= limit:
        detail = {
            "code": "QUOTA",
            "message": "Hai finito i consigli di stile gratuiti! Passa a Premium o torna domani.",
            "feature": feature,
            "limit": limit,
            "used": used,
            "remaining": 0,
            "resetAtUtc": reset_at_utc,
        }
        raise HTTPException(status_code=429, detail=detail)

    # Incrementa quota
    new_used = used + 1
    data.setdefault(today, {})[feature] = new_used
    ref.set(data, merge=True)

    return {
        "feature": feature,
        "limit": limit,
        "used": new_used,
        "remaining": max(0, limit - new_used),
        "resetAtUtc": reset_at_utc,
    }


def get_user_lang(userId: str, default="it"):
    doc = db.collection("users").document(userId).get()
    if doc.exists:
        u = doc.to_dict() or {}
        return (u.get("language") or u.get("lang") or default).lower()
    return default



def avoid_recent(items, recent_ids):
    """Evita di ripetere troppo spesso gli stessi capi."""
    return [it for it in items if (it.get("id") or it.get("docId")) not in recent_ids] or items


# ====================
# UTILS — Giorno IT, stile del giorno e fallback
# ====================

ALL_STYLES = ["streetwear", "casual", "elegante", "sportivo"]

def today_iso_rome() -> str:
    """Data odierna in Europe/Rome (ISO YYYY-MM-DD)."""
    return datetime.now(ZoneInfo("Europe/Rome")).date().isoformat()

def _daily_seed(userId: str, date_iso: str) -> str:
    """Seed deterministico per la giornata (no reroll)."""
    return sha256(f"{userId}:{date_iso}".encode()).hexdigest()

def daily_style_order(userId: str, date_iso: str) -> list[str]:
    """
    Ordina i 4 stili in modo deterministico per (userId, giorno).
    Il primo è lo 'stile del giorno'; gli altri sono fallback in ordine stabile.
    """
    seed = _daily_seed(userId, date_iso)
    # Key per ordinamento stabile (pseudo-random ma ripetibile nel giorno)
    def k(s: str) -> int:
        return int(sha256(f"{seed}:{s}".encode()).hexdigest(), 16)
    return sorted(ALL_STYLES, key=k)

def has_enough_inventory(userId: str, stagione_l: str, stile_l: str) -> dict:
    """
    Verifica capi minimi per generare un outfit in quello stile/stagione.
    Sufficienza: scarpe >=1 e (pezzoUnico >=1  oppure  topBase >=1 AND bottom >=1).
    Ritorna: {"ok": bool, "missing": [..], "counts": {...}}
    """
    q = db.collection("clothingItems") \
          .where("userId", "==", userId) \
          .where("isDirty", "==", False) \
          .where("stagione", "==", stagione_l) \
          .stream()

    counts = {"topBase":0, "topLayer":0, "bottom":0, "scarpe":0, "pezzoUnico":0}
    for d in q:
        it = d.to_dict()
        if normalize_stile(it.get("stile")) != stile_l:
            continue
        cat = it.get("categoria")
        if cat in counts:
            counts[cat] += 1

    ok = (counts["scarpe"] >= 1) and (
        counts["pezzoUnico"] >= 1 or (counts["topBase"] >= 1 and counts["bottom"] >= 1)
    )

    missing = []
    if counts["scarpe"] < 1:
        missing.append("scarpe")
    if counts["pezzoUnico"] < 1 and counts["topBase"] < 1:
        missing.append("topBase")
    if counts["pezzoUnico"] < 1 and counts["bottom"] < 1:
        missing.append("bottom")

    return {"ok": ok, "missing": missing, "counts": counts}

def choose_free_style_with_fallback(userId: str, stagione_l: str) -> dict:
    """
    Determina lo stile del giorno e applica fallback automatico.
    Ritorna:
      {
        "date": YYYY-MM-DD (Europe/Rome),
        "assigned": stile del giorno (primo in ordine),
        "used": stile effettivo usato (può coincidere o essere fallback),
        "fallback": bool,
        "missingByStyle": {stile: [categorie mancanti]}  # presente solo se nessuno stile è sufficiente
      }
    """
    date_iso = today_iso_rome()
    order = daily_style_order(userId, date_iso)

    first = order[0]
    # Prova assegnato + fallback in ordine deterministico
    missing_map = {}
    for s in order:
        chk = has_enough_inventory(userId, stagione_l, s)
        if chk["ok"]:
            return {
                "date": date_iso,
                "assigned": first,
                "used": s,
                "fallback": (s != first),
            }
        else:
            missing_map[s] = chk["missing"]

    # Nessuno stile sufficiente → restituisci mancanti
    return {
        "date": date_iso,
        "assigned": first,
        "used": None,
        "fallback": False,
        "missingByStyle": missing_map
    }

# ====================
# UTILS — Free daily: cache + quota
# ====================

def daily_doc_id(userId: str) -> str:
    """ID documento per l'outfit gratuito del giorno (Europe/Rome)."""
    return f"{userId}_{today_iso_rome()}"

def read_cached_daily_outfit(userId: str):
    """Se esiste l'outfit del giorno per l'utente, lo ritorna; altrimenti None."""
    doc = db.collection("dailyOutfit").document(daily_doc_id(userId)).get()
    if doc.exists:
        data = doc.to_dict() or {}
        return data.get("payload"), data.get("meta", {})
    return None, None

def write_cached_daily_outfit(userId: str, payload: dict, meta: dict):
    """Salva l'outfit del giorno (payload + meta)."""
    db.collection("dailyOutfit").document(daily_doc_id(userId)).set({
        "payload": payload,
        "meta": meta,
        "savedAt": firestore.SERVER_TIMESTAMP,
    }, merge=False)

def reserve_free_daily_or_raise(userId: str):
    """
    Prenota lo slot gratuito del giorno (Europe/Rome).
    Se già usato → 429 QUOTA.
    """
    day = today_iso_rome()
    ref = db.collection("quota").document(userId)
    snap = ref.get()
    data = snap.to_dict() if snap.exists else {}
    used = data.get(day, {}).get("freeDaily", 0)
    if used >= 1:
        detail = {
            "code": "QUOTA",
            "message": "Hai già ottenuto il suggerimento gratuito di oggi. Torna domani o passa a Premium.",
            "feature": "freeDaily",
            "limit": 1,
            "used": used,
            "remaining": 0,
            "resetAtLocal": f"{day}T23:59:59+02:00",  # mezzanotte Europe/Rome
        }
        raise HTTPException(status_code=429, detail=detail)
    data.setdefault(day, {})["freeDaily"] = used + 1
    ref.set(data, merge=True)

def rollback_free_daily_if_any(userId: str):
    """Opzionale: in caso di errore dopo la prenotazione, rimette la quota."""
    day = today_iso_rome()
    ref = db.collection("quota").document(userId)
    snap = ref.get()
    if not snap.exists:
        return
    data = snap.to_dict() or {}
    if data.get(day, {}).get("freeDaily", 0) > 0:
        data[day]["freeDaily"] = max(0, data[day]["freeDaily"] - 1)
        ref.set(data, merge=True)


# ------------------------------
# 9) Colori & compatibilità (+ normalizzatori stile/stagione)
# ------------------------------
# --- Sostituisci la tua sezione Colori & compatibilità con questa ---

# Tavolozza estesa (italiano, minuscolo)
BASIC_COLORS = {
    # neutri base
    "nero":        ["bianco", "grigio", "rosso", "blu", "verde", "beige"],
    "bianco":      ["nero", "blu", "rosso", "verde", "beige", "grigio"],
    "grigio":      ["nero", "bianco", "blu", "rosso", "verde", "beige"],
    "beige":       ["blu", "bianco", "verde", "nero", "grigio", "rosso"],

    # primari/secondari
    "blu":         ["bianco", "beige", "grigio", "arancione", "giallo", "senape", "azzurro"],
    "rosso":       ["nero", "bianco", "beige", "grigio", "bordeaux", "rosa", "lilla"],
    "verde":       ["bianco", "nero", "beige", "grigio", "giallo", "senape", "oliva"],

    # estesi dalla UI
    "giallo":      ["blu", "bianco", "grigio", "verde", "nero", "beige"],
    "marrone":     ["bianco", "beige", "verde", "blu", "grigio", "oliva"],
    "rosa":        ["bianco", "grigio", "blu", "nero", "beige"],
    "lilla":       ["bianco", "grigio", "blu", "nero"],
    "azzurro":     ["bianco", "beige", "grigio", "blu"],
    "oliva":       ["beige", "bianco", "nero", "marrone", "grigio"],
    "bordeaux":    ["bianco", "nero", "beige", "grigio", "rosa"],
    "arancione":   ["blu", "bianco", "grigio", "nero", "beige"],
    "senape":      ["blu", "bianco", "grigio", "verde", "beige"],
    "avorio":      ["nero", "blu", "rosso", "verde", "beige", "grigio"],
    # trattiamo multicolore come neutro “aperto” per massimizzare gli abbinamenti
    "multicolore": ["nero", "bianco", "blu", "rosso", "verde", "beige", "grigio", "marrone", "azzurro"],
}

# Mappa sinonimi/varianti -> colore canonico della palette sopra
COLOR_SYNONYMS = {
    # blu e navy
    "blu navy":          "blu",
    "navy":              "blu",
    "blu scuro":         "blu",
    "blu chiaro":        "azzurro",
    "celeste":           "azzurro",
    "denim":             "blu",
    "jeans":             "blu",
    # verdi e oliva
    "verde oliva":       "oliva",
    "oliva":             "oliva",
    "olive":             "oliva",
    "militare":          "oliva",
    "verde militare":    "oliva",
    "kaki":              "oliva",
    "khaki":             "oliva",
    # marroni / earth
    "cognac":            "marrone",
    "cuoio":             "marrone",
    "cammello":          "beige",
    "camel":             "beige",
    "cammello chiaro":   "beige",
    "beige caldo":       "beige",
    "tortora":           "beige",
    # bianchi / avori
    "crema":             "avorio",
    "ivory":             "avorio",
    "panna":             "avorio",
    "ecru":              "avorio",
    "écru":              "avorio",
    "champagne":         "avorio",
    # grigi / scuri
    "antracite":         "grigio",
    "grafite":           "grigio",
    "grigio scuro":      "grigio",
    "grigio chiaro":     "grigio",
    "grigio melange":    "grigio",
    # rossi / bordeaux
    "bordeaux":          "bordeaux",
    "burgundy":          "bordeaux",
    "vinaccia":          "bordeaux",
    "rosso scuro":       "bordeaux",
    "ruggine":           "arancione",
    "terracotta":        "arancione",
    # rosa / viola
    "fucsia":            "rosa",
    "magenta":           "rosa",
    "viola":             "lilla",
    "lavanda":           "lilla",
    "lilla chiaro":      "lilla",
    # metallici
    "oro":               "giallo",
    "dorato":            "giallo",
    "argento":           "grigio",
    "argentato":         "grigio",
    # altri
    "senape":            "senape",
    "multi":             "multicolore",
    "multicolor":        "multicolore",
    "stampa":            "multicolore",
}

def normalize_color(c: str | None) -> str:
    """Normalizza in minuscolo, mappa i sinonimi; restituisce 'sconosciuto' se il colore non è riconosciuto."""
    if not c:
        return "sconosciuto"
    x = str(c).strip().lower()
    x = COLOR_SYNONYMS.get(x, x)
    if x not in BASIC_COLORS:
        # prova riduzioni semplici (trattini, underscore)
        x = x.replace("-", " ").replace("_", " ")
        x = COLOR_SYNONYMS.get(x, x)
    if x not in BASIC_COLORS:
        return "sconosciuto"
    return x

def are_compatible(c1, c2):
    c1, c2 = normalize_color(c1), normalize_color(c2)
    return c2 in BASIC_COLORS.get(c1, []) or c1 in BASIC_COLORS.get(c2, [])


# color_relation_score è definita più avanti (dopo palette helpers); questa era la versione precedente ora rimossa.

def _norm_text(value):
    return str(value or "").strip().lower()


def _item_text(item):
    if not item:
        return ""
    nome = _norm_text(item.get("nome"))
    categoria = _norm_text(item.get("categoria"))
    stile = _norm_text(item.get("stile"))
    colore = _norm_text(item.get("colore"))
    return f"{nome} {categoria} {stile} {colore}"


def _has_any(text, keywords):
    return any(k in text for k in keywords)


def _infer_style_from_items(top=None, bottom=None, piece=None, shoes=None, layer=None):
    counts: dict[str, int] = {}
    for it in (piece, top, bottom, shoes, layer):
        s = it and normalize_stile(it.get("stile"))
        if s:
            counts[s] = counts.get(s, 0) + 1
    if not counts:
        return ""
    max_count = max(counts.values())
    candidates = [s for s, n in counts.items() if n == max_count]
    for priority in ("elegante", "streetwear", "casual", "sportivo"):
        if priority in candidates:
            return priority
    return candidates[0]


# =========================
# Keyword stilistiche reali
# =========================

STREET_SHOES_GOOD = [
    "sneaker", "sneakers", "trainer", "running", "skate",
    "air force", "dunk", "gazelle", "campus", "new balance",
    "converse", "vans", "urban", "chunky",
    "air max", "low top", "high top",
]

FORMAL_SHOES = [
    "mocassino", "mocassini", "loafer", "loafers",
    "derby", "stringata", "stringate", "oxford",
    "francesina", "elegante", "classica", "classiche",
    "chelsea", "chelsea boot", "stivaletto", "stivaletti",
    "tacco", "tacchi", "pump", "pumps",
    "slingback", "décolleté", "decollete",
]

SPORT_SHOES = [
    "running", "training", "gym", "sport", "sportive", "tennis"
]

STREET_TOPLAYER_GOOD = [
    "hoodie", "felpa", "oversize", "bomber", "denim jacket",
    "giubbotto", "giacca di jeans", "varsity", "college",
    "cargo jacket", "puffer", "street",
    "giacca denim", "denim chiara", "cardigan oversize", "cardigan lungo",
]

ELEGANT_TOPLAYER_GOOD = [
    "blazer", "giacca sartoriale", "giacca elegante",
    "doppiopetto", "monopetto", "tailored", "strutturata",
    "cappotto", "trench",
    "cardigan", "cardigan fine", "cardigan lana", "cardigan cashmere",
]

TOPLAYER_TOO_HEAVY = [
    "parka", "maxi", "imbottito pesante", "pesante", "montone"
]


def _score_real_shoes(shoes_item, target_style):
    if not shoes_item:
        return 0.0

    text = _item_text(shoes_item)
    style = normalize_stile(target_style)
    score = 0.0

    item_style = normalize_stile(shoes_item.get("stile"))
    if item_style and item_style == style:
        score += 0.3

    if style == "streetwear":
        if _has_any(text, STREET_SHOES_GOOD):
            score += 4.0
        if _has_any(text, FORMAL_SHOES):
            score -= 5.5
        if _has_any(text, SPORT_SHOES):
            score += 1.0

    elif style == "elegante":
        if _has_any(text, FORMAL_SHOES):
            score += 3.4
        if _has_any(text, STREET_SHOES_GOOD):
            score -= 2.0
        if _has_any(text, SPORT_SHOES):
            score -= 2.5

    elif style == "casual":
        if _has_any(text, STREET_SHOES_GOOD):
            score += 1.4
        if _has_any(text, FORMAL_SHOES):
            score -= 0.8
        if _has_any(text, SPORT_SHOES):
            score += 0.4

    elif style == "sportivo":
        if _has_any(text, SPORT_SHOES):
            score += 3.0
        if _has_any(text, FORMAL_SHOES):
            score -= 4.0
        if _has_any(text, STREET_SHOES_GOOD):
            score += 0.5

    return score


def _score_real_toplayer(top_layer_item, target_style):
    if not top_layer_item:
        return 0.0

    text = _item_text(top_layer_item)
    style = normalize_stile(target_style)
    score = 0.0

    item_style = normalize_stile(top_layer_item.get("stile"))
    if item_style and item_style == style:
        score += 0.8

    if _has_any(text, TOPLAYER_TOO_HEAVY):
        score -= 1.2

    if style == "streetwear":
        if _has_any(text, STREET_TOPLAYER_GOOD):
            score += 3.0
        if _has_any(text, ELEGANT_TOPLAYER_GOOD):
            score -= 3.5

    elif style == "elegante":
        if _has_any(text, ELEGANT_TOPLAYER_GOOD):
            score += 3.2
        if _has_any(text, STREET_TOPLAYER_GOOD):
            score -= 2.2

    elif style == "casual":
        if _has_any(text, STREET_TOPLAYER_GOOD):
            score += 1.0
        if _has_any(text, ELEGANT_TOPLAYER_GOOD):
            score += 0.6

    elif style == "sportivo":
        if _has_any(text, ELEGANT_TOPLAYER_GOOD):
            score -= 2.5
        if _has_any(text, STREET_TOPLAYER_GOOD):
            score += 0.4

    return score
    
def _score_shoes_layer_combo(shoes_item, top_layer_item, target_style):
    """
    Bonus/malus morbido sulla combinazione scarpe + topLayer.
    NON esclude outfit: migliora solo il ranking finale.
    """
    if not shoes_item or not top_layer_item:
        return 0.0

    shoes_text = _item_text(shoes_item)
    layer_text = _item_text(top_layer_item)
    style = normalize_stile(target_style)

    shoes_is_street = _has_any(shoes_text, STREET_SHOES_GOOD)
    shoes_is_formal = _has_any(shoes_text, FORMAL_SHOES)
    shoes_is_sport = _has_any(shoes_text, SPORT_SHOES)

    layer_is_street = _has_any(layer_text, STREET_TOPLAYER_GOOD)
    layer_is_elegant = _has_any(layer_text, ELEGANT_TOPLAYER_GOOD)
    layer_too_heavy = _has_any(layer_text, TOPLAYER_TOO_HEAVY)

    score = 0.0

    if style == "streetwear":
        if shoes_is_street and layer_is_street:
            score += 2.2
        if shoes_is_formal and layer_is_street:
            score -= 2.8
        if shoes_is_formal and layer_is_elegant:
            score -= 1.2
        if layer_too_heavy:
            score -= 0.8

    elif style == "elegante":
        if shoes_is_formal and layer_is_elegant:
            score += 2.2
        if shoes_is_street and layer_is_elegant:
            score -= 1.8
        if shoes_is_sport and layer_is_elegant:
            score -= 2.0

    elif style == "casual":
        if shoes_is_street and layer_is_street:
            score += 0.8
        if shoes_is_formal and layer_is_elegant:
            score += 0.6
        if shoes_is_formal and layer_is_street:
            score -= 0.6

    elif style == "sportivo":
        if shoes_is_sport and layer_is_street:
            score += 0.8
        if shoes_is_formal and layer_is_elegant:
            score -= 1.5
        if shoes_is_formal and layer_is_street:
            score -= 1.2

    return score

def _score_visual_balance(top=None, bottom=None, piece=None, shoes=None, layer=None, target_style=None):
    """
    Bonus/malus per rendere il look più premium:
    - evita outfit piatti o tutti uguali
    - premia 1 accento colore ben gestito
    - penalizza troppi pezzi "forti" insieme
    - premia layering utile e coerente
    """
    items = [it for it in [top, bottom, piece, shoes, layer] if it]
    if not items:
        return 0.0

    colors = [normalize_color(it.get("colore")) for it in items if it.get("colore")]
    style = normalize_stile(target_style)
    score = 0.0

    neutrals = {"nero", "bianco", "grigio", "beige", "avorio", "marrone"}
    accents = {"rosso", "rosa", "lilla", "verde", "oliva", "giallo", "senape", "arancione", "bordeaux", "azzurro", "blu"}

    neutral_count = sum(1 for c in colors if c in neutrals)
    accent_count = sum(1 for c in colors if c in accents)

    if len(colors) >= 3 and neutral_count == len(colors):
        unique_neutral = len(set(colors))
        if unique_neutral == 1:
            score += 0.3    # monocromatico intenzionale (total black, total grey)
        elif unique_neutral == 2:
            pass            # tonal duo neutro: né bonus né malus
        else:
            score -= 0.3    # tre neutri diversi senza accento: lieve piattezza

    if neutral_count >= 2 and accent_count == 1:
        score += 1.4

    if accent_count >= 3:
        score -= 2.2

    if len(set(colors)) >= 4:
        score -= 1.4

    if layer:
        if piece:
            score += 0.4
        elif top and bottom:
            score += 0.8

    if style in {"streetwear", "casual", "elegante"}:
        if top and bottom and not layer:
            score -= 0.3

    if len(set(colors)) == 1:
        score += 0.5        # look monocromatico intenzionale
    elif len(set(colors)) == 2:
        score += 1.0
    elif len(set(colors)) == 3:
        score += 0.6

    return score


def _score_base_item_fit(
    base_item,
    top=None,
    bottom=None,
    piece=None,
    shoes=None,
    layer=None
):
    """
    Bonus per QuickPair: premia i capi che valorizzano davvero il capo cliccato.
    """
    if not base_item:
        return 0.0

    base_cat = base_item.get("categoria")
    base_color = normalize_color(base_item.get("colore"))
    score = 0.0

    def col(it):
        return normalize_color(it.get("colore")) if it else None

    if base_cat == "bottom":
        if top:
            score += color_relation_score(base_color, col(top)) * 2.0
        if shoes:
            score += color_relation_score(base_color, col(shoes)) * 1.8
        if layer and top:
            score += max(
                color_relation_score(col(layer), col(top)),
                color_relation_score(col(layer), base_color)
            ) * 0.8

    elif base_cat == "topBase":
        if bottom:
            score += color_relation_score(base_color, col(bottom)) * 2.0
        if layer:
            score += color_relation_score(base_color, col(layer)) * 1.0
        if shoes and bottom:
            score += color_relation_score(col(bottom), col(shoes)) * 1.2

    elif base_cat == "scarpe":
        if bottom:
            score += color_relation_score(base_color, col(bottom)) * 2.4
        if top and bottom:
            score += color_relation_score(col(top), col(bottom)) * 1.0
        if layer and top:
            score += color_relation_score(col(layer), col(top)) * 0.5
        if piece:
            score += color_relation_score(base_color, col(piece)) * 2.0

    elif base_cat == "topLayer":
        if top:
            score += color_relation_score(base_color, col(top)) * 1.8
        if bottom and top:
            score += color_relation_score(col(top), col(bottom)) * 1.2
        if shoes and bottom:
            score += color_relation_score(col(bottom), col(shoes)) * 0.8
        if piece:
            score += color_relation_score(base_color, col(piece)) * 1.5

    elif base_cat == "pezzoUnico":
        if shoes:
            score += color_relation_score(base_color, col(shoes)) * 2.3
        if layer:
            score += color_relation_score(base_color, col(layer)) * 1.0

    return score


NEUTRALS = {"bianco", "nero", "grigio", "navy", "beige"}
EARTH = {"marrone", "cognac", "cuoio"}
STRONG = {"rosso", "giallo", "fucsia", "arancione"}


def color_relation_score(c1, c2) -> float:
    r1 = str(c1 or "").strip().lower()
    r2 = str(c2 or "").strip().lower()
    t1 = normalize_color(c1)
    t2 = normalize_color(c2)

    def neutral_side(t: str, raw: str) -> int:
        if "navy" in raw or t == "blu":
            return 1
        if t in NEUTRALS:
            return 1
        return 0

    def earth_side(t: str, raw: str) -> int:
        if t == "marrone":
            return 1
        if "cognac" in raw or "cuoio" in raw:
            return 1
        return 0

    def cool_elegant_side(t: str, raw: str) -> int:
        if t in ("blu", "grigio"):
            return 1
        if "navy" in raw:
            return 1
        return 0

    def strong_side(t: str, raw: str) -> int:
        if t in ("rosso", "giallo", "arancione"):
            return 1
        if "fucsia" in raw or "magenta" in raw:
            return 1
        if t == "fucsia":
            return 1
        return 0

    def bright_green_side(t: str, raw: str) -> int:
        if t != "verde":
            return 0
        if any(k in raw for k in ("acceso", "fluo", "fluoresc", "lime", "neon", "smeraldo")):
            return 1
        return 0

    def bright_purple_side(t: str, raw: str) -> int:
        if "fucsia" in raw or "magenta" in raw:
            return 1
        if "viola" in raw and any(k in raw for k in ("acceso", "fluo", "neon", "elett")):
            return 1
        return 0

    def same_color_family(ta: str, ra: str, tb: str, rb: str) -> bool:
        if ta == tb:
            return False
        if ta in ("blu", "azzurro") and tb in ("blu", "azzurro"):
            return True
        ea = ta == "marrone" or "cognac" in ra or "cuoio" in ra
        eb = tb == "marrone" or "cognac" in rb or "cuoio" in rb
        if ea and eb:
            return True
        if ta in ("verde", "oliva") and tb in ("verde", "oliva"):
            return True
        if ta in ("rosso", "bordeaux") and tb in ("rosso", "bordeaux"):
            return True
        return False

    if t1 == "sconosciuto" or t2 == "sconosciuto":
        return 0.2

    if t1 == t2:
        return 0.65

    # Coppie neutre classiche: abbinamenti iconici moda, non mediocri
    _CLASSIC = {
        frozenset({"bianco", "nero"}):   0.85,
        frozenset({"bianco", "grigio"}): 0.80,
        frozenset({"nero",   "grigio"}): 0.75,
        frozenset({"bianco", "beige"}):  0.72,
        frozenset({"nero",   "beige"}):  0.72,
        frozenset({"grigio", "beige"}):  0.68,
    }
    if frozenset({t1, t2}) in _CLASSIC:
        return _CLASSIC[frozenset({t1, t2})]

    if same_color_family(t1, r1, t2, r2):
        return 0.75

    if (t1 == "nero" and "navy" in r2) or (t2 == "nero" and "navy" in r1):
        return 0.1

    if (t1 == "rosso" and t2 == "verde") or (t2 == "rosso" and t1 == "verde"):
        if (t1 == "verde" and bright_green_side(t1, r1)) or (t2 == "verde" and bright_green_side(t2, r2)):
            return -1.0
        return -0.5

    if (t1 == "giallo" and bright_purple_side(t2, r2)) or (t2 == "giallo" and bright_purple_side(t1, r1)):
        return -0.65

    if strong_side(t1, r1) and strong_side(t2, r2):
        return -0.2

    if (earth_side(t1, r1) and cool_elegant_side(t2, r2)) or (
        earth_side(t2, r2) and cool_elegant_side(t1, r1)
    ):
        return 0.9

    if neutral_side(t1, r1) and neutral_side(t2, r2):
        return 0.5

    if neutral_side(t1, r1) or neutral_side(t2, r2):
        return 0.7

    return 0.2


def palette_score(items) -> float:
    """Equilibrio cromatico globale outfit in [-1, 1]; morbido, indipendente dalle coppie."""
    if not items or not isinstance(items, list):
        return 0.0

    norm_list: list[tuple[str, str]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        raw = it.get("colore")
        if raw is None:
            continue
        if isinstance(raw, str) and not raw.strip():
            continue
        n = normalize_color(raw)
        if not n or n == "sconosciuto":
            continue
        norm_list.append((str(raw).strip().lower(), n))

    if len(norm_list) < 2:
        return 0.0

    def is_neutral(t: str, raw: str) -> bool:
        if "navy" in raw or t == "blu":
            return True
        if t in ("nero", "bianco", "grigio", "beige", "avorio", "azzurro"):
            return True
        return False

    def is_earth(t: str, raw: str) -> bool:
        return t == "marrone" or "cognac" in raw or "cuoio" in raw

    def is_bright_green(t: str, raw: str) -> bool:
        if t != "verde":
            return False
        return any(k in raw for k in ("acceso", "fluo", "fluoresc", "lime", "neon", "smeraldo"))

    def is_strong(t: str, raw: str) -> bool:
        if t in ("rosso", "giallo", "arancione", "fucsia"):
            return True
        if "fucsia" in raw or "magenta" in raw:
            return True
        if is_bright_green(t, raw):
            return True
        return False

    def tono_su_tono_pair(ra: str, ta: str, rb: str, tb: str) -> bool:
        if ta == tb:
            return True
        if ta in ("blu", "azzurro") and tb in ("blu", "azzurro"):
            return True
        ea = ta == "marrone" or "cognac" in ra or "cuoio" in ra
        eb = tb == "marrone" or "cognac" in rb or "cuoio" in rb
        if ea and eb:
            return True
        if ta in ("verde", "oliva") and tb in ("verde", "oliva"):
            return True
        if ta in ("rosso", "bordeaux") and tb in ("rosso", "bordeaux"):
            return True
        return False

    mc = ne = ea = st = ot = 0
    family_tags: list[str] = []
    for raw, t in norm_list:
        if t == "multicolore":
            mc += 1
            family_tags.append("mc")
        elif is_strong(t, raw):
            st += 1
            family_tags.append("strong")
        elif is_earth(t, raw):
            ea += 1
            family_tags.append("earth")
        elif is_neutral(t, raw):
            ne += 1
            family_tags.append("neutral")
        else:
            ot += 1
            family_tags.append("other")

    n_items = len(norm_list)
    unique_norms = {t for _, t in norm_list}
    n_families = len(set(family_tags))

    score = 0.0

    if len(unique_norms) == 1:
        score += 0.28
    elif len(unique_norms) == 2:
        u = list(unique_norms)
        reps: dict[str, str] = {}
        for raw, t in norm_list:
            if t not in reps:
                reps[t] = raw
        ra, rb = reps[u[0]], reps[u[1]]
        if tono_su_tono_pair(ra, u[0], rb, u[1]):
            score += 0.22

    if st == 1 and (ne + ea) >= 1:
        score += 0.32
    if ne >= 1 and ea >= 1 and st == 0:
        score += 0.2
    if st == 0 and mc == 0 and ea == 0 and ot == 0 and ne == n_items:
        score += 0.12
    if mc >= 1 and st == 0:
        score += 0.05

    if st >= 2:
        score -= 0.12
    if n_families > 3:
        score -= 0.14
    if mc >= 1 and st >= 1:
        score -= 0.1

    return max(-1.0, min(1.0, score))


def shoes_score(bottom, shoes, layer=None, target_style=None) -> float:
    """Quanto le scarpe aiutano o ostacolano l'outfit, in [-1, 1] (contributo morbido)."""
    if shoes is None or not isinstance(shoes, dict):
        return 0.0

    def _has_color_val(c) -> bool:
        if c is None:
            return False
        if isinstance(c, str) and not str(c).strip():
            return False
        return True

    def _raw(c) -> str:
        return str(c or "").strip().lower()

    s_col = shoes.get("colore")
    b_col = bottom.get("colore") if bottom is not None and isinstance(bottom, dict) else None
    l_col = layer.get("colore") if layer is not None and isinstance(layer, dict) else None

    rs = _raw(s_col)
    rb = _raw(b_col)
    rl = _raw(l_col)

    tb = (
        normalize_color(b_col)
        if bottom is not None and isinstance(bottom, dict) and _has_color_val(b_col)
        else None
    )
    ts = normalize_color(s_col) if _has_color_val(s_col) else None
    tl = (
        normalize_color(l_col)
        if layer is not None and isinstance(layer, dict) and _has_color_val(l_col)
        else None
    )
    if tb == "sconosciuto":
        tb = None
    if ts == "sconosciuto":
        ts = None
    if tl == "sconosciuto":
        tl = None

    blob = " ".join(
        [
            _norm_text(shoes.get("nome")),
            _norm_text(shoes.get("categoria")),
            _norm_text(shoes.get("stile")),
        ]
    )

    score = 0.0

    if (
        bottom is not None
        and isinstance(bottom, dict)
        and _has_color_val(b_col)
        and _has_color_val(s_col)
    ):
        bc = normalize_color(b_col)
        sc = normalize_color(s_col)
        if bc != "sconosciuto" and sc != "sconosciuto":
            score += color_relation_score(b_col, s_col) * 0.18

    def _earth_shoe(t: str | None, r: str) -> bool:
        return t == "marrone" or "cognac" in r or "cuoio" in r

    def _cool_bottom(t: str | None, r: str) -> bool:
        if "navy" in r or "denim" in r:
            return True
        return t in ("blu", "grigio")

    if ts is not None and tb is not None and _earth_shoe(ts, rs) and _cool_bottom(tb, rb):
        score += 0.08

    if ts == "bianco" and tb is not None:
        if tb in ("blu", "grigio", "nero") or "denim" in rb or "navy" in rb:
            score += 0.07

    if ts == "nero" and tb is not None:
        if tb in ("nero", "grigio", "blu") or "navy" in rb:
            score += 0.07

    def _loud_color(t: str | None, r: str) -> bool:
        if t in ("rosso", "giallo", "arancione", "fucsia"):
            return True
        if "fucsia" in r or "magenta" in r:
            return True
        if t == "verde" and any(
            k in r for k in ("acceso", "fluo", "fluoresc", "lime", "neon", "smeraldo")
        ):
            return True
        return False

    if ts is not None and tb is not None and _loud_color(ts, rs) and _loud_color(tb, rb):
        score -= 0.08

    if ts is not None and tl is not None and ts == tl:
        score += 0.05

    tgt = normalize_stile(target_style)

    def _has_kw(*words: str) -> bool:
        return any(w in blob for w in words)

    if tgt == "elegante":
        if _has_kw("mocassino", "mocassin", "derby", "oxford", "loafer", "loafers"):
            score += 0.08
        if _has_kw(
            "tacco",
            "tacchi",
            "décolleté",
            "decollete",
            "pump",
            "pumps",
            "slingback",
            "sandalo elegante",
            "sandali eleganti",
        ):
            score += 0.08
        if _has_kw("stival", "stivale", "stivaletto") and not _has_kw(
            "combat", "moto", "biker"
        ):
            score += 0.06
        if _has_kw("sneaker", "sneakers", "running", "trainer", "basket", "tennis"):
            score -= 0.06
    elif tgt == "streetwear":
        if _has_kw("sneaker", "sneakers", "dunk", "jordan", "yeezy", "skate"):
            score += 0.09
        if _has_kw("mocassino", "oxford", "derby", "loafer") and not _has_kw(
            "sneaker", "sneakers"
        ):
            score -= 0.06
    elif tgt == "sportivo":
        if _has_kw(
            "sneaker",
            "sneakers",
            "running",
            "trainer",
            "sport",
            "sportiv",
            "ginnastica",
        ):
            score += 0.08
        if _has_kw("oxford", "derby", "mocassino", "elegante", "tacco"):
            score -= 0.07
    elif tgt == "casual":
        if _has_kw("sneaker", "sneakers", "stivalett", "chelsea", "mocassino"):
            score += 0.05

    if abs(score) < 0.02:
        if _has_color_val(s_col) and _has_color_val(b_col) and bottom is not None:
            bc = normalize_color(b_col)
            sc = normalize_color(s_col)
            if bc != "sconosciuto" and sc != "sconosciuto":
                score = color_relation_score(b_col, s_col) * 0.12
        elif _has_color_val(s_col) and ts is not None:
            score = 0.03

    return max(-1.0, min(1.0, score))


def style_score(items, target_style=None) -> float:
    """Coerenza stilistica dell'outfit in [-1, 1]; contributo morbido."""
    if not items or not isinstance(items, list):
        return 0.0

    item_texts: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        n = _norm_text(it.get("nome"))
        c = _norm_text(it.get("categoria"))
        s = normalize_stile(it.get("stile"))
        t = " ".join(x for x in (n, c, s) if x).strip()
        if t:
            item_texts.append(t)

    if not item_texts:
        return 0.0

    big = " ".join(item_texts)

    def _hit(text: str, kws: tuple[str, ...]) -> bool:
        return any(k in text for k in kws)

    def _count_items(kws: tuple[str, ...]) -> int:
        return sum(1 for t in item_texts if _hit(t, kws))

    def _blazer_elegant(t: str) -> bool:
        return "blazer" in t and (
            "elegante" in t or "cerimonia" in t or "sartoria" in t or "formale" in t
        )

    street_bonus = (
        "sneaker",
        "sneakers",
        "hoodie",
        "felpa",
        "oversize",
        "cargo",
        "denim",
        "bomber",
        "puffer",
        "street",
        "skate",
    )
    street_pen = (
        "oxford",
        "derby",
        "mocassino",
        "décolleté",
        "decolleté",
        "decollete",
        "tacco elegante",
        "pumps",
        "pump",
        "slingback",
    )
    eleg_bonus = (
        "blazer",
        "camicia",
        "mocassino",
        "oxford",
        "derby",
        "tacco",
        "décolleté",
        "decolleté",
        "decollete",
        "pump",
        "pumps",
        "slingback",
        "abito",
        "vestito",
        "trench",
        "cappotto",
    )
    eleg_pen = (
        "hoodie",
        "felpa",
        "cargo",
        "running",
        "gym",
        "sportivo",
        "basket",
        "skate",
    )
    casual_bonus = (
        "jeans",
        "denim",
        "t-shirt",
        "t shirt",
        "maglietta",
        "camicia semplice",
        "sneaker",
        "sneakers",
        "stivalett",
        "cardigan",
        "maglione",
        "chino",
        "chinos",
    )
    sport_bonus = (
        "sneaker",
        "sneakers",
        "running",
        "gym",
        "tuta",
        "leggings",
        "felpa",
        "hoodie",
        "sportivo",
        "tecnico",
        "trainer",
        "ginnastica",
    )
    sport_pen = (
        "oxford",
        "derby",
        "mocassino",
        "décolleté",
        "decolleté",
        "decollete",
        "tacco",
        "pump",
        "pumps",
        "slingback",
    )

    formal_items = sum(
        1
        for t in item_texts
        if _hit(t, ("oxford", "derby", "mocassino", "abito", "vestito", "trench"))
        or _blazer_elegant(t)
        or "décolleté" in t
        or "decolleté" in t
        or "decollete" in t
        or ("tacco" in t and "elegante" in t)
    )
    street_sport_items = sum(
        1
        for t in item_texts
        if _hit(
            t,
            ("hoodie", "felpa", "running", "gym", "basket", "skate", "street", "cargo"),
        )
        or _hit(t, ("sneaker", "sneakers"))
    )

    tgt = normalize_stile(target_style)
    score = 0.0

    if formal_items >= 1 and street_sport_items >= 1:
        score -= 0.08
    if formal_items >= 2 and street_sport_items >= 2:
        score -= 0.05

    if tgt == "streetwear":
        if _hit(big, street_bonus):
            score += 0.1
        if _hit(big, street_pen) or any(_blazer_elegant(t) for t in item_texts):
            score -= 0.08
        if _count_items(street_bonus) >= 2:
            score += 0.08
    elif tgt == "elegante":
        if _hit(big, eleg_bonus) or "pantalone elegante" in big:
            score += 0.11
        if _hit(big, eleg_pen):
            score -= 0.09
        coh = 0
        for t in item_texts:
            if _hit(t, eleg_bonus) or "pantalone elegante" in t or _blazer_elegant(t):
                coh += 1
        if coh >= 2:
            score += 0.08
    elif tgt == "casual":
        if _hit(big, casual_bonus):
            score += 0.07
        too_sport = _hit(big, ("running", "gym", "tuta", "leggings", "tecnico"))
        too_form = _hit(big, ("oxford", "abito", "vestito", "décolleté", "decollete")) or any(
            _blazer_elegant(t) for t in item_texts
        )
        if too_sport or too_form:
            score -= 0.06
        if _count_items(casual_bonus) >= 2:
            score += 0.06
    elif tgt == "sportivo":
        if _hit(big, sport_bonus):
            score += 0.1
        pen = _hit(big, sport_pen) or any(_blazer_elegant(t) for t in item_texts)
        if pen:
            score -= 0.09
        if _count_items(sport_bonus) >= 2:
            score += 0.08
    else:
        if formal_items == 0 and street_sport_items == 0:
            score += 0.04

    return max(-1.0, min(1.0, score))


def effective_color(item) -> str:
    """Colore più plausibile per scoring: parole nel nome vincono sul campo colore (es. blazer navy vs grigio)."""
    if not isinstance(item, dict):
        return ""
    nome = item.get("nome") or ""
    colore_raw = item.get("colore")
    colore = str(colore_raw) if colore_raw is not None else ""
    nome_l = str(nome).lower()

    for needle, canon in (
        ("blu navy", "navy"),
        ("navy", "navy"),
        ("verde oliva", "verde oliva"),
        ("oliva", "verde oliva"),
        ("burgundy", "bordeaux"),
        ("bordeaux", "bordeaux"),
        ("cognac", "cognac"),
        ("cuoio", "cuoio"),
        ("camel", "cammello"),
        ("cammello", "cammello"),
        ("beige", "beige"),
        ("nero", "nero"),
        ("bianco", "bianco"),
        ("grigio", "grigio"),
        ("celeste", "azzurro"),
        ("azzurro", "azzurro"),
        ("blu", "blu"),
        ("rosso", "rosso"),
        ("verde", "verde"),
        ("giallo", "giallo"),
        ("arancione", "arancione"),
        ("fucsia", "fucsia"),
        ("rosa", "rosa"),
        ("marrone", "marrone"),
    ):
        if needle in nome_l:
            return canon
    return colore.strip()


def outfit_score_v2(
    top=None,
    bottom=None,
    shoes=None,
    layer=None,
    piece=None,
    target_style=None,
    base_item=None,
    prefer_palette=None,
) -> float:
    """Score outfit con media pesata di colore, palette, scarpe e stile; base_item riservato al futuro."""
    _ = base_item

    if piece is not None:
        items = [x for x in (piece, shoes, layer) if x is not None]
    else:
        items = [x for x in (top, bottom, shoes, layer) if x is not None]

    if not items:
        return 0.0

    def _item_with_effective_color(it):
        if not isinstance(it, dict):
            return it
        copied = dict(it)
        ec = effective_color(it)
        if ec:
            copied["colore"] = ec
        return copied

    top_eff    = _item_with_effective_color(top)
    bottom_eff = _item_with_effective_color(bottom)
    shoes_eff  = _item_with_effective_color(shoes)
    layer_eff  = _item_with_effective_color(layer)
    piece_eff  = _item_with_effective_color(piece)
    items_eff  = [_item_with_effective_color(i) for i in items]

    def _has_col(it) -> bool:
        if not it or not isinstance(it, dict):
            return False
        c = it.get("colore")
        if c is None:
            return False
        if isinstance(c, str) and not str(c).strip():
            return False
        return True

    def _pair_rel(a, b, acc: list[float]) -> None:
        if not a or not b:
            return
        ca = effective_color(a)
        cb = effective_color(b)
        if not ca or not cb:
            return
        acc.append(color_relation_score(ca, cb))

    rel_vals: list[float] = []
    rel_no_layer: list[float] = []

    if piece is None:
        _pair_rel(top, bottom, rel_vals)
        _pair_rel(bottom, shoes, rel_vals)
        _pair_rel(top, shoes, rel_vals)
        _pair_rel(layer, bottom, rel_vals)
        _pair_rel(layer, shoes, rel_vals)
        _pair_rel(top, bottom, rel_no_layer)
        _pair_rel(bottom, shoes, rel_no_layer)
        _pair_rel(top, shoes, rel_no_layer)
    else:
        _pair_rel(piece, shoes, rel_vals)
        _pair_rel(piece, layer, rel_vals)
        _pair_rel(layer, shoes, rel_vals)
        _pair_rel(piece, shoes, rel_no_layer)

    if rel_vals:
        mean_score = sum(rel_vals) / len(rel_vals)
        min_score = min(rel_vals)
        color_score = mean_score * 0.8 + min_score * 0.2
        if min_score <= -0.75:
            color_score = min(color_score, 0.15)
        elif min_score <= -0.5:
            color_score = min(color_score, 0.30)
    else:
        color_score = 0.0
    color_score = max(-1.0, min(1.0, color_score))

    palette = palette_score(items_eff)
    shoe = shoes_score(
        bottom_eff if piece is None else piece_eff,
        shoes_eff,
        layer_eff,
        target_style,
    )
    style = style_score(items, target_style)

    final = (
        color_score * 0.35
        + palette * 0.25
        + shoe * 0.20
        + style * 0.20
    )

    if style < -0.5:
        final *= 0.5
    if palette < -0.5:
        final *= 0.7

    layer_bonus = 0.0
    if layer is not None and layer in items:
        items_nl = [x for x in items if x is not layer]
        pal_full = palette
        pal_nl = palette_score(items_nl) if items_nl else 0.0
        if pal_full > pal_nl + 0.02:
            layer_bonus += min(0.03, (pal_full - pal_nl) * 0.6)
        if rel_no_layer:
            color_nl = sum(rel_no_layer) / len(rel_no_layer)
            if color_score > color_nl + 0.02:
                layer_bonus += min(0.03, (color_score - color_nl) * 0.6)
        layer_bonus = min(0.05, layer_bonus)
        final += layer_bonus

    prefer_bonus = 0.0
    if prefer_palette:
        prefs: set[str] = set()
        for p in prefer_palette:
            if p is None:
                continue
            if isinstance(p, str) and not p.strip():
                continue
            nc = normalize_color(p if isinstance(p, str) else str(p))
            if nc and nc != "sconosciuto":
                prefs.add(nc)
        if prefs:
            match_n = 0
            for it in items:
                if not _has_col(it):
                    continue
                ic = normalize_color(it.get("colore"))
                if ic != "sconosciuto" and ic in prefs:
                    match_n += 1
            if match_n > 0:
                prefer_bonus = min(0.05, 0.02 * match_n)

    final += prefer_bonus

    return max(-1.0, min(1.0, final))


def outfit_score(
    top=None,
    bottom=None,
    piece=None,
    shoes=None,
    layer=None,
    prefer_palette: list[str] | None = None,
    target_style: str | None = None,
    base_item: dict | None = None
):
    """
    Score totale outfit:
    1) compatibilità colore
    2) piccolo bias su palette preferita
    3) peso stilistico reale di scarpe e topLayer
    """
    score = 0.0

    # =========================
    # 1) Compatibilità colore (graduata)
    # =========================
    if piece and shoes:
        score += color_relation_score(piece.get("colore"), shoes.get("colore")) * 1.4
    if top and bottom:
        score += color_relation_score(top.get("colore"), bottom.get("colore")) * 1.4
    if bottom and shoes:
        score += color_relation_score(bottom.get("colore"), shoes.get("colore")) * 1.4
    if top and layer:
        score += color_relation_score(top.get("colore"), layer.get("colore")) * 0.7
    if bottom and layer:
        score += color_relation_score(bottom.get("colore"), layer.get("colore")) * 0.7
    if piece and layer:
        score += color_relation_score(piece.get("colore"), layer.get("colore")) * 0.7

    # =========================
    # 2) Bias cromatico
    # =========================
    if prefer_palette:
        for it in (piece, top, bottom, shoes, layer):
            if it and normalize_color(it.get("colore")) in prefer_palette:
                score += 0.25

    # =========================
    # 3) Peso stilistico reale
    # =========================
    style_eff = normalize_stile(target_style) or _infer_style_from_items(
        top=top,
        bottom=bottom,
        piece=piece,
        shoes=shoes,
        layer=layer
    )

    if style_eff:
        score += _score_real_shoes(shoes, style_eff)
        score += _score_real_toplayer(layer, style_eff)
        score += _score_shoes_layer_combo(shoes, layer, style_eff)

    score += _score_visual_balance(
        top=top,
        bottom=bottom,
        piece=piece,
        shoes=shoes,
        layer=layer,
        target_style=style_eff
    )

    score += _score_base_item_fit(
        base_item=base_item,
        top=top,
        bottom=bottom,
        piece=piece,
        shoes=shoes,
        layer=layer
    )

    return score


_LAYER_MIN_GAIN = 0.25


def _best_layer_for_outfit(
    compat_layers: list | None,
    *,
    top=None,
    bottom=None,
    piece=None,
    shoes=None,
    prefer_palette: list[str] | None = None,
    target_style: str | None = None,
    base_item: dict | None = None,
):
    base_sc = outfit_score(
        top=top,
        bottom=bottom,
        piece=piece,
        shoes=shoes,
        layer=None,
        prefer_palette=prefer_palette,
        target_style=target_style,
        base_item=base_item,
    )
    if not compat_layers:
        return None, base_sc
    best_layer = None
    best_sc = base_sc
    for lyr in compat_layers:
        sc = outfit_score(
            top=top,
            bottom=bottom,
            piece=piece,
            shoes=shoes,
            layer=lyr,
            prefer_palette=prefer_palette,
            target_style=target_style,
            base_item=base_item,
        )
        if sc > best_sc:
            best_sc = sc
            best_layer = lyr
    if best_layer is not None and best_sc >= base_sc + _LAYER_MIN_GAIN:
        return best_layer, best_sc
    return None, base_sc


# ------------------------------
# 10) Lingue supportate
# ------------------------------
LANG_NAME = {"it": "italiano", "en": "inglese", "es": "spagnolo"}

# ------------------------------
# Helper di risposta (mancavano: ok/err)
# ------------------------------
def ok(payload: dict, status_code: int = 200):
    data = {"success": True}
    data.update(payload or {})
    return JSONResponse(content=data, status_code=status_code)

def err(e: Exception | str, status_code: int = 500):
    msg = str(e)
    return JSONResponse(content={"success": False, "error": msg}, status_code=status_code)

def _compact_item(it: dict) -> dict:
    # Riduci l’oggetto per il prompt (meno token)
    return {
        "id": it.get("docId") or it.get("id"),
        "categoria": it.get("categoria"),
        "nome": (it.get("nome") or "")[:60],
        "colore": normalize_color(it.get("colore")),
    }

def _index_by_id(items: list[dict]) -> dict:
    m = {}
    for it in items:
        _id = it.get("docId") or it.get("id")
        if _id:
            m[_id] = it
    return m

def _ai_choose_outfits(articoli: list[dict], stile: str, stagione: str, lang: str, lang_name: str, n: int = 3) -> dict | None:
    """
    Chiede a GPT di scegliere direttamente i capi. Ritorna dict con chiavi:
    { "outfits": [ {idTop,idBottom,idShoes,idLayer,idPiece,descrizione}, ... ],
      "consiglioExtra": "..." }
    Oppure None se fallisce.
    """
    if not openai or not OPENAI_API_KEY:
        return None

    # comprimi lista capi
    items_small = [_compact_item(it) for it in articoli]

    prompt = (
        "Sei un fashion stylist personale.\n"
        f"Hai accesso a questi capi (JSON): {json.dumps(items_small, ensure_ascii=False)}\n\n"
        f"Crea {n} proposte di outfit per lo stile '{stile}' adatte alla stagione '{stagione}'.\n"
        "Regole:\n"
        "- Usa solo capi presenti nella lista.\n"
        "- Evita ripetizioni e privilegia armonia cromatica e coerenza con lo stile.\n"
        "- Se mancano capi importanti, segnala cosa acquistare.\n"
        f"- Scrivi la descrizione in {lang_name}, max 2 frasi per outfit.\n\n"
        "Rispondi SOLO con un JSON valido di questa forma:\n"
        "{\n"
        '  "outfits": [\n'
        '    {"idTop": "...", "idBottom": "...", "idShoes": "...", "idLayer": "...", "idPiece": "...", "descrizione": "..."},\n'
        "    ...\n"
        "  ],\n"
        '  "consiglioExtra": "..." \n'
        "}\n"
    )

    try:
        gpt = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.8
        )
        raw = gpt.choices[0].message.content.strip()
        data = json.loads(raw)
        # validazione minima
        if not isinstance(data, dict) or "outfits" not in data or not isinstance(data["outfits"], list):
            return None
        return data
    except Exception as _:
        return None

def _pick_image(it: dict | None) -> str | None:
    return it.get("imageUrl") if it else None
def _ai_pick_best_candidate(candidates: list[dict], stile: str, stagione: str, lang_name: str) -> dict | None:
    """
    GPT sceglie il migliore tra candidati già filtrati dalla logica locale.
    Non esplora tutto l'armadio: valuta solo poche opzioni già sensate.
    Ritorna direttamente il dict 'outfit' del candidato vincente, oppure None.
    """
    if not openai or not OPENAI_API_KEY:
        return None

    if not candidates:
        return None

    shortlist = []
    for idx, cand in enumerate(candidates[:5], start=1):
        o = cand.get("outfit", {})

        def slim(it):
            if not it:
                return None
            return {
                "id": it.get("docId") or it.get("id"),
                "categoria": it.get("categoria"),
                "nome": it.get("nome"),
                "colore": normalize_color(it.get("colore")),
            }

        shortlist.append({
            "index": idx,
            "top": slim(o.get("top")),
            "bottom": slim(o.get("bottom")),
            "layer": slim(o.get("layer")),
            "shoes": slim(o.get("shoes")),
            "piece": slim(o.get("pezzoUnico")),
        })

    prompt = (
        f"Sei un fashion stylist premium, molto esigente. "
        f"Devi scegliere il miglior outfit per stile '{stile}' e stagione '{stagione}' "
        f"tra candidate già tecnicamente valide.\n\n"

        f"Non scegliere l'outfit solo perché è compatibile: scegli quello più bello, desiderabile, moderno e portabile davvero.\n\n"

        f"Criteri di valutazione, in ordine di importanza:\n"
        f"1. Coerenza reale con lo stile richiesto.\n"
        f"2. Armonia cromatica elegante: preferisci palette pulite, ben bilanciate, con colori che stanno bene insieme davvero.\n"
        f"3. Equilibrio visivo: meglio un look con un protagonista chiaro e gli altri capi a supporto, non tanti pezzi che competono tra loro.\n"
        f"4. Desiderabilità premium: scegli il look che un utente indosserebbe davvero con piacere.\n"
        f"5. Modernità e pulizia: premia outfit ordinati, credibili, non confusi.\n\n"

        f"Penalizza fortemente:\n"
        f"- look piatti o banali\n"
        f"- troppi colori forti insieme\n"
        f"- layer inutili o che appesantiscono\n"
        f"- outfit corretti ma poco belli\n"
        f"- combinazioni che sembrano casuali o poco curate\n\n"

        f"Premia:\n"
        f"- buon contrasto tra capi\n"
        f"- presenza intelligente di neutri\n"
        f"- un solo accento forte ben gestito\n"
        f"- outfit che sembrano pensati da uno stylist\n"
        f"- combinazioni facili da indossare ma con personalità\n\n"

        f"Se due outfit sono simili, scegli quello più raffinato, armonioso e premium.\n"
        f"Rispondi SOLO con un JSON valido nel formato "
        f'{{"bestIndex": 1}} '
        f"senza testo extra.\n\n"
        f"Candidate: {json.dumps(shortlist, ensure_ascii=False)}"
    )

    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=30,
            temperature=0.2,
            request_timeout=4,
        )
        raw = (resp.choices[0].message.content or "").strip()
        data = json.loads(raw)
        best_index = int(data.get("bestIndex", 0))

        if 1 <= best_index <= min(len(candidates), 5):
            return candidates[best_index - 1].get("outfit")
        return None

    except Exception as e:
        print(f"[_ai_pick_best_candidate ERROR] {e}")
        return None
def _safe_id(x):  # accetta None o string
    return x if x else None


# ------------------------------
# 11) FASTAPI setup
# ------------------------------
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def log_all_requests(request: Request, call_next):
    print(f"=== REQUEST {request.method} {request.url.path} ===")
    try:
        response = await call_next(request)
        print(f"=== RESPONSE {request.method} {request.url.path} -> {response.status_code} ===")
        return response
    except Exception as e:
        print(f"=== CRASH {request.method} {request.url.path}: {type(e).__name__}: {e} ===")
        raise

@app.get("/")
async def root():
    return {"status": "ok", "community_public_only": COMMUNITY_PUBLIC_ONLY}

# ------------------------------
# 12) /upload — Upload immagine + background removal + Storage
# ------------------------------

@app.post("/upload")
async def upload_image(file: UploadFile = File(...)):
    try:
        print("=== ENTER /upload ===")
        print("filename:", file.filename if file else None)

        raw = await file.read()
        print("bytes letti:", len(raw) if raw else 0)

        if not raw:
            return err("File vuoto")

        # Apro immagine originale
        img = Image.open(BytesIO(raw))
        try:
            img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        img = img.convert("RGBA")

        print("=== REMBG DISATTIVATA TEMPORANEAMENTE ===")

        # =========================
        # CANVAS LEGGERO
        # =========================
        try:
            w, h = img.size
            pad_x = max(18, int(w * 0.06))
            pad_y = max(18, int(h * 0.06))

            canvas_w = w + (pad_x * 2)
            canvas_h = h + (pad_y * 2)

            canvas = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 0))
            canvas.paste(img, (pad_x, pad_y), img)
            img = canvas

            max_side = 1600
            if img.width > max_side or img.height > max_side:
                img.thumbnail((max_side, max_side), Image.LANCZOS)

        except Exception as e:
            print(f"⚠️ Errore canvas/resize: {type(e).__name__}: {e}")

        # =========================
        # SALVATAGGIO PNG
        # =========================
        out = BytesIO()
        img.save(out, format="PNG")
        out.seek(0)
        png_bytes = out.read()

        uid = str(uuid.uuid4())
        path = f"items/final_{uid}.png"

        blob = bucket.blob(path)
        token = str(uuid.uuid4())

        blob.metadata = {
            "firebaseStorageDownloadTokens": token
        }

        blob.upload_from_string(
            png_bytes,
            content_type="image/png"
        )

        image_url = (
            f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}"
            f"/o/{path.replace('/', '%2F')}?alt=media&token={token}"
        )

        return JSONResponse(
            content={
                "success": True,
                "imageUrl": image_url,
                "nome": None,
                "colore": None,
                "categoria": None,
                "stile": None,
                "stagione": None,
            }
        )

    except HTTPException:
        raise

    except Exception as e:
        print(f"[UPLOAD ERROR] type={type(e).__name__} msg={e}")
        return err(e)


# ====== TIMEOUT SOFT PER AI (Punto 8) ======
class Timeout(Exception):
    pass

def _timeout_handler(signum, frame):
    raise Timeout()

def ai_choose_wrapper(*, noAI: bool, timeout_sec: int, chooser, **kwargs):
    """
    Wrapper con timeout 'soft' per le scelte AI.
    Su ambienti dove signal non funziona (es. Colab/Windows),
    puoi rimpiazzare con un guard su time.monotonic().
    """
    if noAI:
        return None
    try:
        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout_sec)
        try:
            return chooser(**kwargs)
        finally:
            signal.alarm(0)
    except Exception:
        # In caso di Timeout o qualsiasi errore AI, fai fallback
        return None

# ====== PICCOLI HELPER (Punti 4, 9, 10) ======
def _exclude(items, ids):
    return [i for i in (items or []) if (i.get("docId") or i.get("id")) not in ids]

def _sig(o: dict):
    # firma minimale e ordinata sugli ID capi (None -> '-')
    return tuple(sorted([
        (o.get("pezzoUnico") or {}).get("id") or (o.get("pezzoUnico") or {}).get("docId") or "-",
        (o.get("top")        or {}).get("id") or (o.get("top")        or {}).get("docId") or "-",
        (o.get("bottom")     or {}).get("id") or (o.get("bottom")     or {}).get("docId") or "-",
        (o.get("layer")      or {}).get("id") or (o.get("layer")      or {}).get("docId") or "-",
        (o.get("shoes")      or {}).get("id") or (o.get("shoes")      or {}).get("docId") or "-",
    ]))

def _is_valid_outfit(o: dict) -> bool:
    # Almeno 2 elementi tra pezzoUnico/top/bottom/shoes (Punto 10)
    count = 0
    if o.get("pezzoUnico"): count += 1
    if o.get("top"):        count += 1
    if o.get("bottom"):     count += 1
    if o.get("shoes"):      count += 1
    return count >= 2


# ------------------------------
# 13) /outfit — scelta colori + varietà (con lingua + quota + premium)
# ------------------------------
@app.get("/outfit")
async def genera_outfit(
    stile: str = Query(None, description="Ignorato per utenti free; usato solo per premium."),
    stagione: str = Query(...),
    userId: str = Query(...),
    lang: str = Query(None),
    premium: bool = Query(False),
    charge: bool = Query(False),  # tenuto per retro-compatibilità; ignorato per free-daily

    # === Parametri opzionali retro-compatibili (Punto 2) ===
    maxOutfits: int = Query(3, ge=1, le=3),   # premium: quanti outfit provare a restituire
    compact: bool = Query(False),             # se True, payload alleggerito
    noAI: bool = Query(True),                 # forza fallback manuale (utile per test/latenza)
    preferColors: str = Query("", description="comma-separated, e.g. 'blu,beige'"),
    excludeIds: str = Query("", description="comma-separated item ids to avoid"),
    refreshSeed: str = Query("", description="seed opzionale per refresh deterministico"),
):
    # parsing veloce (Punti 2 e 4)
    prefer_palette = [normalize_color(c) for c in (preferColors or "").split(",") if c.strip()]
    exclude_set = set([x.strip() for x in (excludeIds or "").split(",") if x.strip()])

    try:
        # Normalizzazioni base e lingua
        stile_l_req = normalize_stile(stile) if stile else None
        stagione_l = normalize_stagione(stagione)
        lang = (lang or get_user_lang(userId, "it")).lower()
        lang_name = LANG_NAME.get(lang, "italiano")

        # =========================
        # RAMO PREMIUM
        # =========================
        if premium:
            if not stile_l_req:
                return JSONResponse(
                    content={"error": "Per gli utenti Premium è necessario specificare 'stile'."},
                    status_code=400
                )

            # (1) Filtra stile già in Firestore (meno letture) + opzionale select (Bonus Punto 1)
            base_q = (
                db.collection("clothingItems")
                .where("userId", "==", userId)
                .where("isDirty", "==", False)
                .where("stagione", "==", stagione_l)
                .where("stile", "==", stile_l_req)
                # .select(["categoria", "nome", "colore", "imageUrl"])  # <-- opzionale
                .stream()
            )

            articoli = []
            for doc in base_q:
                d = doc.to_dict()
                d["docId"] = doc.id
                d["stile"] = normalize_stile(d.get("stile"))
                d["stagione"] = normalize_stagione(d.get("stagione"))
                d["colore"] = normalize_color(d.get("colore"))
                if d["stile"] == stile_l_req:
                    articoli.append(d)

            # fallback se pochi articoli: rilassa stile (mantieni stagione/isDirty)
            # Fallback Firestore disattivato: evitata seconda lettura inutile.
            # Se ci sono pochi articoli, usiamo direttamente quelli già trovati.

            # id coerenti
            for it in articoli:
                it.setdefault("id", it.get("docId"))

            # === AI-FIRST (Premium) — con timeout soft e n=maxOutfits ===
            idx = _index_by_id(articoli)
            # Disattivo la scelta outfit via GPT:
            # gli outfit vengono scelti solo dalla logica locale,
            # molto più stabile per il cuore del Lookbook.
            ai_result = None

            if ai_result and ai_result.get("outfits"):
                outfits_payload = []
                for o in ai_result["outfits"]:
                    idTop    = _safe_id(o.get("idTop"))
                    idBottom = _safe_id(o.get("idBottom"))
                    idShoes  = _safe_id(o.get("idShoes"))
                    idLayer  = _safe_id(o.get("idLayer"))
                    idPiece  = _safe_id(o.get("idPiece"))

                    top    = idx.get(idTop) if idTop else None
                    bottom = idx.get(idBottom) if idBottom else None
                    shoes  = idx.get(idShoes) if idShoes else None
                    layer  = idx.get(idLayer) if idLayer else None
                    piece  = idx.get(idPiece) if idPiece else None

                    # Validazione minima (Punto 10)
                    outfit_obj = {
                        "pezzoUnico": piece, "top": top, "bottom": bottom, "layer": layer, "shoes": shoes
                    }
                    if not _is_valid_outfit(outfit_obj):
                        continue

                    # Esclusioni lato client (Punto 4)
                    ids_this = set([
                        (piece or {}).get("docId") or (piece or {}).get("id"),
                        (top   or {}).get("docId") or (top   or {}).get("id"),
                        (bottom or {}).get("docId") or (bottom or {}).get("id"),
                        (layer or {}).get("docId") or (layer or {}).get("id"),
                        (shoes or {}).get("docId") or (shoes or {}).get("id"),
                    ])
                    if exclude_set & ids_this:
                        continue

                    payload_single = {
                        "pezzoUnicoImage": piece.get("imageUrl") if piece else None,
                        "topImage":        top.get("imageUrl")   if top else None,
                        "bottomImage":     bottom.get("imageUrl") if bottom else None,
                        "topLayerImage":   layer.get("imageUrl")  if layer else None,
                        "scarpeImage":     shoes.get("imageUrl")  if shoes else None,
                        "descrizione":     o.get("descrizione") or "",
                        "lang":            lang,
                    }
                    outfits_payload.append(payload_single)

                if outfits_payload:
                    response_premium = {
                        "premium": True,
                        "lang": lang,
                        "outfits": outfits_payload,
                        "consiglioExtra": ai_result.get("consiglioExtra", "")
                    }

                    # Compact mode (Punto 7)
                    if compact and isinstance(response_premium.get("outfits"), list):
                        for oo in response_premium["outfits"]:
                            oo.pop("lang", None)

                    # History ultimi ID usati
                    used_ids = []
                    for o in ai_result["outfits"]:
                        for k in ["idTop","idBottom","idShoes","idLayer","idPiece"]:
                            if o.get(k):
                                used_ids.append(o[k])
                    hist_ref = db.collection("outfitHistory").document(f"{userId}_{stile_l_req}")
                    hist_doc = hist_ref.get()
                    recent_ids = set((hist_doc.to_dict().get("recentIds") or [])[-10:]) if hist_doc.exists else set()
                    hist_ref.set({"recentIds": list((recent_ids | set(used_ids)))[-10:]}, merge=True)

                    db.collection("outfits").document(f"{userId}_{stile_l_req}").set(response_premium)
                    return JSONResponse(content=response_premium)

            # === FALLBACK MANUALE (Premium) ===
            topbase  = [a for a in articoli if a.get("categoria") == "topBase"]
            toplayer = [a for a in articoli if a.get("categoria") == "topLayer"]
            bottom   = [a for a in articoli if a.get("categoria") == "bottom"]
            scarpe   = [a for a in articoli if a.get("categoria") == "scarpe"]
            pezzo    = [a for a in articoli if a.get("categoria") == "pezzoUnico"]

            if not scarpe or (not pezzo and (not topbase or not bottom)):
                missing = []
                if len(scarpe) < 1: missing.append("scarpe")
                if len(pezzo) < 1 and len(topbase) < 1: missing.append("topBase")
                if len(pezzo) < 1 and len(bottom)  < 1: missing.append("bottom")
                return JSONResponse(
                    content={
                        "error": "FEW_ITEMS",
                        "message": "Pochi articoli per questo stile.",
                        "lang": lang,
                        "missing": missing,
                        "counts": {
                            "topBase":    len(topbase),
                            "topLayer":   len(toplayer),
                            "bottom":     len(bottom),
                            "scarpe":     len(scarpe),
                            "pezzoUnico": len(pezzo),
                        }
                    },
                    status_code=200
                )

            # storico outfit → evita ripetizioni
            hist_ref = db.collection("outfitHistory").document(f"{userId}_{stile_l_req}")
            hist_doc = hist_ref.get()
            recent_ids = set((hist_doc.to_dict().get("recentIds") or [])[-10:]) if hist_doc.exists else set()

            topbase  = avoid_recent(topbase,  recent_ids)
            bottom   = avoid_recent(bottom,   recent_ids)
            scarpe   = avoid_recent(scarpe,   recent_ids)
            toplayer = avoid_recent(toplayer, recent_ids)
            pezzo    = avoid_recent(pezzo,    recent_ids)

            # (Punto 4) escludi capi già mostrati in UI
            topbase  = _exclude(topbase,  exclude_set)
            toplayer = _exclude(toplayer, exclude_set)
            bottom   = _exclude(bottom,   exclude_set)
            scarpe   = _exclude(scarpe,   exclude_set)
            pezzo    = _exclude(pezzo,    exclude_set)

            # Seed deterministico (Punto 5)
            seed_material = refreshSeed or str(time.time())
            rnd = random.Random(seed_material)

            candidati = []

            # pezzo unico
            # pezzo unico
            if pezzo and scarpe:
                for p in rnd.sample(pezzo, k=min(len(pezzo), 12)):
                    sample_shoes = rnd.sample(scarpe, k=min(len(scarpe), 12))
                    compat_shoes_p = [sh for sh in sample_shoes if are_compatible(p.get("colore"), sh.get("colore"))] or sample_shoes
                    for sh in compat_shoes_p:
                        compat_layers = [
                            l for l in toplayer
                            if are_compatible(p.get("colore"), l.get("colore"))
                        ]
                        cand_layer, score = _best_layer_for_outfit(
                            compat_layers,
                            piece=p,
                            shoes=sh,
                            prefer_palette=prefer_palette,
                            target_style=stile_l_req,
                        )
                        candidati.append({
                            "score": score,
                            "outfit": {
                                "pezzoUnico": p,
                                "top": None,
                                "bottom": None,
                                "layer": cand_layer,
                                "shoes": sh
                            }
                        })

            # top + bottom
            if topbase and bottom and scarpe:
                sample_tops = rnd.sample(topbase, k=min(len(topbase), 16))
                sample_bottoms = bottom if len(bottom) <= 20 else rnd.sample(bottom, 20)
                sample_shoes = scarpe if len(scarpe) <= 20 else rnd.sample(scarpe, 20)

                for t in sample_tops:
                    compat_b = [
                        b for b in sample_bottoms
                        if are_compatible(t.get("colore"), b.get("colore"))
                    ] or sample_bottoms

                    for b in compat_b:
                        compat_s = [
                            s for s in sample_shoes
                            if are_compatible(b.get("colore"), s.get("colore"))
                        ] or sample_shoes

                        compat_l = [
                            l for l in toplayer
                            if (
                                are_compatible(l.get("colore"), t.get("colore")) or
                                are_compatible(l.get("colore"), b.get("colore"))
                            )
                        ]

                        sh = rnd.choice(compat_s)
                        cand_layer, score = _best_layer_for_outfit(
                            compat_l,
                            top=t,
                            bottom=b,
                            shoes=sh,
                            prefer_palette=prefer_palette,
                            target_style=stile_l_req,
                        )

                        candidati.append({
                            "score": score,
                            "outfit": {
                                "pezzoUnico": None,
                                "top": t,
                                "bottom": b,
                                "layer": cand_layer,
                                "shoes": sh
                            }
                        })

            # valida + ordina + dedup
            candidati = [c for c in candidati if _is_valid_outfit(c["outfit"])]
            candidati.sort(key=lambda x: x["score"], reverse=True)

            seen, dedup = set(), []
            for c in candidati:
                s = _sig(c["outfit"])
                if s in seen:
                    continue
                seen.add(s)
                dedup.append(c)
                if len(dedup) >= maxOutfits:
                    break

            topK = dedup

            if not topK:
                return JSONResponse(
                    content={"error": "Impossibile generare un outfit coerente.", "lang": lang}
                )

            if refreshSeed and len(topK) > 1:
                try:
                    shift = int(sha256(refreshSeed.encode()).hexdigest(), 16) % len(topK)
                    topK = topK[shift:] + topK[:shift]
                except Exception:
                    pass

            # GPT come giudice finale: sceglie solo tra i migliori candidati locali (max 5).
            if not noAI:
                print("GPT judge enabled")
                picked_outfit = _ai_pick_best_candidate(
                    topK[:5], stile_l_req, stagione_l, lang_name
                )
                if picked_outfit and _is_valid_outfit(picked_outfit):
                    sig_pick = _sig(picked_outfit)
                    idx_match = next(
                        (i for i, c in enumerate(topK) if _sig(c["outfit"]) == sig_pick),
                        None,
                    )
                    if idx_match is not None:
                        topK = [topK[idx_match]] + [
                            c for i, c in enumerate(topK) if i != idx_match
                        ]
                        print("GPT judge selected candidate")
                    else:
                        print("GPT judge fallback to local ranking")
                else:
                    print("GPT judge fallback to local ranking")

            outfits_payload = []
            for idx, c in enumerate(topK):
                o = c["outfit"]
                scelta, descr = {}, []

                if o["pezzoUnico"]:
                    p = o["pezzoUnico"]
                    scelta["pezzoUnicoImage"] = p["imageUrl"]
                    scelta["pezzoUnicoId"] = p.get("id") or p.get("docId")
                    scelta["topImage"] = None
                    scelta["topId"] = None
                    scelta["bottomImage"] = None
                    scelta["bottomId"] = None
                    descr.append(f"pezzo unico: {p['nome']} {normalize_color(p['colore'])}")
                else:
                    t = o["top"]
                    b = o["bottom"]
                    scelta["pezzoUnicoImage"] = None
                    scelta["pezzoUnicoId"] = None
                    scelta["topImage"] = t["imageUrl"]
                    scelta["topId"] = t.get("id") or t.get("docId")
                    scelta["bottomImage"] = b["imageUrl"]
                    scelta["bottomId"] = b.get("id") or b.get("docId")
                    descr.append(f"top: {t['nome']} {normalize_color(t['colore'])}")
                    descr.append(f"bottom: {b['nome']} {normalize_color(b['colore'])}")

                if o["layer"]:
                    l = o["layer"]
                    scelta["topLayerImage"] = l["imageUrl"]
                    scelta["topLayerId"] = l.get("id") or l.get("docId")
                    descr.append(f"strato: {l['nome']} {normalize_color(l['colore'])}")
                else:
                    scelta["topLayerImage"] = None
                    scelta["topLayerId"] = None

                s = o["shoes"]
                scelta["scarpeImage"] = s["imageUrl"]
                scelta["scarpeId"] = s.get("id") or s.get("docId")
                descr.append(f"scarpe: {s['nome']} {normalize_color(s['colore'])}")

                description = fallback_description(descr, lang)
                consiglio_extra = ""

                if idx == 0:
                    # GPT disattivato temporaneamente per stabilizzare /outfit su Render.
                    # Usiamo solo descrizione locale e nessun consiglio extra.
                    pass

                scelta["descrizione"] = description
                scelta["consiglioExtra"] = consiglio_extra
                scelta["lang"] = lang
                outfits_payload.append(scelta)


            response_premium = {
                "premium": True,
                "lang": lang,
                "outfits": outfits_payload
            }

            # Compact mode (Punto 7)
            if compact and isinstance(response_premium.get("outfits"), list):
                for oo in response_premium["outfits"]:
                    oo.pop("lang", None)

            first_outfit = topK[0]["outfit"]
            used_ids = [
                first_outfit[k].get("id") or first_outfit[k].get("docId")
                for k in ["pezzoUnico", "top", "bottom", "layer", "shoes"]
                if first_outfit.get(k)
            ]

            hist_ref.set(
                {"recentIds": list((recent_ids | set(used_ids)))[-10:]},
                merge=True
            )

            db.collection("outfits").document(f"{userId}_{stile_l_req}").set(response_premium)
            return JSONResponse(content=response_premium)


        # =========================
        # (continua sotto con RAMO FREE)
        # =========================
        # =========================
        # RAMO FREE: 1 outfit/giorno, 1 stile con fallback, cache giornaliera
        # =========================

        # 1) Scelta stile del giorno (deterministica + fallback)
        free_choice = choose_free_style_with_fallback(userId, stagione_l)
        assigned = free_choice["assigned"]
        used = free_choice.get("used")
        fb = bool(free_choice.get("fallback", False))

        # 2) Se nessuno stile è sufficiente → NON consumare quota
        if not used:
            return JSONResponse(content={
                "error": "Pochi articoli per generare l'outfit di oggi.",
                "freeDaily": {
                    "date": free_choice["date"],
                    "assigned": assigned,
                    "used": None,
                    "fallback": False,
                    "missingByStyle": free_choice.get("missingByStyle", {})
                },
                "premium": False
            })

        # 3) Cache giornaliera già presente?
        cached, meta = read_cached_daily_outfit(userId)
        if cached:
            cached["freeDaily"] = {
                "date": meta.get("date"),
                "assigned": meta.get("assignedStyle"),
                "used": meta.get("usedStyle"),
                "fallback": meta.get("fallback", False)
            }
            cached["premium"] = False
            return JSONResponse(content=cached)

        # 4) Prenota slot free del giorno
        reserve_free_daily_or_raise(userId)

        # 5) Genera outfit vincolato allo stile 'used'
        try:
            stile_l = used  # vincolo per i free

            # Filtra lato Firestore anche lo stile (Punto 1)
            base_q = (
                db.collection("clothingItems")
                .where("userId", "==", userId)
                .where("isDirty", "==", False)
                .where("stagione", "==", stagione_l)
                .where("stile", "==", stile_l)
                # .select(["categoria", "nome", "colore", "imageUrl"])  # opzionale
                .stream()
            )

            articoli = []
            for doc in base_q:
                d = doc.to_dict()
                d["docId"] = doc.id
                d["stile"] = normalize_stile(d.get("stile"))
                d["stagione"] = normalize_stagione(d.get("stagione"))
                d["colore"] = normalize_color(d.get("colore"))
                if d["stile"] == stile_l:
                    articoli.append(d)

            # fallback se pochi articoli: rilassa stile mantenendo stagione/isDirty
            # Fallback Firestore disattivato anche nel ramo free:
            # evitiamo una seconda lettura inutile e usiamo direttamente i capi già trovati.

            for it in articoli:
                it.setdefault("id", it.get("docId"))

            # categorie
            topbase  = [a for a in articoli if a.get("categoria") == "topBase"]
            toplayer = [a for a in articoli if a.get("categoria") == "topLayer"]
            bottom   = [a for a in articoli if a.get("categoria") == "bottom"]
            scarpe   = [a for a in articoli if a.get("categoria") == "scarpe"]
            pezzo    = [a for a in articoli if a.get("categoria") == "pezzoUnico"]

            if not scarpe or (not pezzo and (not topbase or not bottom)):
                rollback_free_daily_if_any(userId)
                return JSONResponse(content={
                    "error": "Pochi articoli per generare l'outfit di oggi.",
                    "freeDaily": {
                        "date": free_choice["date"],
                        "assigned": assigned,
                        "used": None,
                        "fallback": False
                    },
                    "premium": False
                })

            # storico outfit → evita ripetizioni
            hist_ref = db.collection("outfitHistory").document(f"{userId}_{stile_l}")
            hist_doc = hist_ref.get()
            recent_ids = set((hist_doc.to_dict().get("recentIds") or [])[-10:]) if hist_doc.exists else set()

            topbase  = avoid_recent(topbase,  recent_ids)
            bottom   = avoid_recent(bottom,   recent_ids)
            scarpe   = avoid_recent(scarpe,   recent_ids)
            toplayer = avoid_recent(toplayer, recent_ids)
            pezzo    = avoid_recent(pezzo,    recent_ids)

            # (Punto 4) escludi capi già mostrati in UI
            topbase  = _exclude(topbase,  exclude_set)
            toplayer = _exclude(toplayer, exclude_set)
            bottom   = _exclude(bottom,   exclude_set)
            scarpe   = _exclude(scarpe,   exclude_set)
            pezzo    = _exclude(pezzo,    exclude_set)

            # Seed deterministico (Punto 5)
            seed_material = refreshSeed or str(time.time())
            rnd = random.Random(seed_material)

            best, best_score = None, -1.0

            # pezzo unico
            # pezzo unico
            if pezzo and scarpe:
                for p in rnd.sample(pezzo, k=min(len(pezzo), 12)):
                    sample_shoes_p = rnd.sample(scarpe, k=min(len(scarpe), 12))
                    compat_shoes_p = [sh for sh in sample_shoes_p if are_compatible(p.get("colore"), sh.get("colore"))] or sample_shoes_p
                    for sh in compat_shoes_p:
                        compat_layers = [
                            l for l in toplayer
                            if are_compatible(p.get("colore"), l.get("colore"))
                        ]
                        cand_layer, score = _best_layer_for_outfit(
                            compat_layers,
                            piece=p,
                            shoes=sh,
                            prefer_palette=prefer_palette,
                            target_style=stile_l,
                        )

                        if score > best_score:
                            best_score, best = score, {
                                "pezzoUnico": p,
                                "top": None,
                                "bottom": None,
                                "layer": cand_layer,
                                "shoes": sh
                            }

            # top + bottom
            if topbase and bottom and scarpe:
                sample_tops = rnd.sample(topbase, k=min(len(topbase), 16))
                sample_bottoms = bottom if len(bottom) <= 20 else rnd.sample(bottom, 20)
                sample_shoes = scarpe if len(scarpe) <= 20 else rnd.sample(scarpe, 20)

                for t in sample_tops:
                    compat_b = [
                        b for b in sample_bottoms
                        if are_compatible(t.get("colore"), b.get("colore"))
                    ] or sample_bottoms

                    for b in compat_b:
                        compat_s = [
                            s for s in sample_shoes
                            if are_compatible(b.get("colore"), s.get("colore"))
                        ] or sample_shoes

                        compat_l = [
                            l for l in toplayer
                            if (
                                are_compatible(l.get("colore"), t.get("colore")) or
                                are_compatible(l.get("colore"), b.get("colore"))
                            )
                        ]
                        sh = rnd.choice(compat_s)
                        cand_layer, score = _best_layer_for_outfit(
                            compat_l,
                            top=t,
                            bottom=b,
                            shoes=sh,
                            prefer_palette=prefer_palette,
                            target_style=stile_l,
                        )

                        if score > best_score:
                            best_score, best = score, {
                                "pezzoUnico": None,
                                "top": t,
                                "bottom": b,
                                "layer": cand_layer,
                                "shoes": sh
                            }
            if not best:
                rollback_free_daily_if_any(userId)
                return JSONResponse(content={
                    "error": "Impossibile generare un outfit coerente.",
                    "freeDaily": {
                        "date": free_choice["date"],
                        "assigned": assigned,
                        "used": None,
                        "fallback": False
                    },
                    "premium": False
                })

            # Output (descrizione locale per i free)
            scelta, descr = {}, []
            if best["pezzoUnico"]:
                p = best["pezzoUnico"]
                scelta["pezzoUnicoImage"] = p["imageUrl"]
                scelta["topImage"] = None
                scelta["bottomImage"] = None
                descr.append(f"pezzo unico: {p['nome']} {normalize_color(p['colore'])}")
            else:
                t = best["top"]; b = best["bottom"]
                scelta["pezzoUnicoImage"] = None
                scelta["topImage"] = t["imageUrl"]
                scelta["bottomImage"] = b["imageUrl"]
                descr.append(f"top: {t['nome']} {normalize_color(t['colore'])}")
                descr.append(f"bottom: {b['nome']} {normalize_color(b['colore'])}")

            if best["layer"]:
                l = best["layer"]
                scelta["topLayerImage"] = l["imageUrl"]
                descr.append(f"strato: {l['nome']} {normalize_color(l['colore'])}")
            else:
                scelta["topLayerImage"] = None

            s = best["shoes"]
            scelta["scarpeImage"] = s["imageUrl"]
            descr.append(f"scarpe: {s['nome']} {normalize_color(s['colore'])}")

            description = f"Outfit {stile_l} per {stagione_l}: " + ", ".join(descr) + "."
            scelta["descrizione"] = description
            scelta["lang"] = lang

            # salva in 'outfits' e aggiorna history
            db.collection("outfits").document(f"{userId}_{stile_l}").set(scelta)
            used_ids = [
                best[k].get("id") or best[k].get("docId")
                for k in ["pezzoUnico", "top", "bottom", "layer", "shoes"]
                if best.get(k)
            ]
            hist_ref.set({"recentIds": list((recent_ids | set(used_ids)))[-10:]}, merge=True)

            # 6) Salva cache giornaliera e rispondi
            meta = {
                "date": free_choice["date"],
                "assignedStyle": assigned,
                "usedStyle": stile_l,
                "fallback": fb,
                "stagioneAtGeneration": stagione_l,
                "lang": lang
            }
            payload = dict(scelta)
            payload["freeDaily"] = {
                "date": meta["date"],
                "assigned": assigned,
                "used": stile_l,
                "fallback": fb
            }
            payload["premium"] = False

            # Compact mode per il ramo free (Punto 7 — facoltativo)
            if compact:
                for k in list(payload.keys()):
                    if k not in ("pezzoUnicoImage","topImage","bottomImage","topLayerImage","scarpeImage","descrizione","lang","freeDaily","premium"):
                        payload.pop(k, None)

            write_cached_daily_outfit(userId, payload, meta)
            return JSONResponse(content=payload)

        except Exception as gen_err:
            rollback_free_daily_if_any(userId)
            return JSONResponse(content={"error": str(gen_err), "premium": False}, status_code=500)

    # ====== Rate-limit QUOTA con header standard (Punto 6) ======
    except HTTPException as he:
        if he.status_code == 429 and isinstance(he.detail, dict) and he.detail.get("code") == "QUOTA":
            resp = JSONResponse(content={"error": he.detail}, status_code=429)
            limit = he.detail.get("limit", 0)
            used  = he.detail.get("used", 0)
            reset = he.detail.get("resetAtUtc", "")
            resp.headers["X-RateLimit-Limit"] = str(limit)
            resp.headers["X-RateLimit-Remaining"] = str(max(0, limit - used))
            resp.headers["X-RateLimit-Reset"] = reset
            return resp
        raise
    except Exception as e:
        return JSONResponse(content={"error": str(e)})
# ------------------------------
# Funzione di fallback multilingua
# ------------------------------
def fallback_description(parts: list[str], lang: str) -> str:
    cleaned = [str(p).strip() for p in parts if p and str(p).strip()]

    def simplify_it(part: str) -> str:
        p = part.strip()

        replacements = [
            ("pezzo unico:", ""),
            ("top:", ""),
            ("bottom:", ""),
            ("strato:", ""),
            ("scarpe:", ""),
        ]
        for old, new in replacements:
            p = p.replace(old, new).strip()

        words = p.split()
        if len(words) >= 2 and words[-1].lower() == words[-2].lower():
            p = " ".join(words[:-1])

        return p.strip()

    cleaned = [simplify_it(p) for p in cleaned if simplify_it(p)]

    if not cleaned:
        if lang == "en":
            return "Well-balanced outfit."
        if lang == "es":
            return "Outfit equilibrado."
        return "Outfit ben bilanciato."

    if lang == "en":
        return "Outfit with " + ", ".join(cleaned[:-1]) + (" and " + cleaned[-1] if len(cleaned) > 1 else cleaned[0]) + "."
    if lang == "es":
        return "Outfit con " + ", ".join(cleaned[:-1]) + (" y " + cleaned[-1] if len(cleaned) > 1 else cleaned[0]) + "."

    if len(cleaned) == 1:
        return f"Outfit con {cleaned[0]}."
    if len(cleaned) == 2:
        return f"Outfit con {cleaned[0]} e {cleaned[1]}."
    return f"Outfit con {', '.join(cleaned[:-1])} e {cleaned[-1]}."

def safe_gpt_text(prompt: str, max_tokens: int = 120, temperature: float = 0.8, timeout_sec: int = 4) -> str | None:
    """
    Genera un testo con GPT senza bloccare l'endpoint.
    Usa il timeout nativo della libreria OpenAI, più stabile di signal.alarm in Colab/Uvicorn.
    """
    if not openai or not OPENAI_API_KEY:
        return None

    try:
        resp = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            temperature=temperature,
            request_timeout=timeout_sec,
        )
        text = (resp.choices[0].message.content or "").strip()
        return text or None
    except Exception as e:
        print(f"[safe_gpt_text ERROR] {e}")
        return None

    # ------------------------------
# 13-bis) /quickpair — Suggerimento PRO (Premium) dato un capo base
# ------------------------------
@app.get("/quickpair")
async def quickpair(
    userId: str = Query(...),
    baseId: str = Query(..., description="document id di clothingItems"),
    stagione: str = Query(...),
    lang: str = Query(None),
):
    try:
        stagione_l = normalize_stagione(stagione)
        lang = (lang or get_user_lang(userId, "it")).lower()
        lang_name = LANG_NAME.get(lang, "italiano")

        # 1) Premium detection + quota FREE (max 2/giorno)
        udoc = db.collection("users").document(userId).get()
        is_premium = False
        if udoc.exists:
            udata = udoc.to_dict() or {}
            is_premium = bool(udata.get("isPremium", False))

        if not is_premium:
            _quota = check_and_increment_quota_or_raise(
                userId=userId,
                feature="quickpair",
                limit_free=QUICKPAIR_FREE_DAILY_LIMIT
            )

        # 2) Carica capo base
        bdoc = db.collection("clothingItems").document(baseId).get()
        if not bdoc.exists:
            print("QUICKPAIR ERROR: NOT_FOUND")
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "NOT_FOUND",
                    "message": "Capo base non trovato."
                }
            )

        base = bdoc.to_dict()
        if base.get("userId") != userId or base.get("isDirty") is True:
            print("QUICKPAIR ERROR: INVALID_BASE")
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_BASE",
                    "message": "Capo base non valido."
                }
            )

        if stagione_l and normalize_stagione(base.get("stagione")) != stagione_l:
            pass

        stile_l = normalize_stile(base.get("stile"))
        if not stile_l:
            print("QUICKPAIR ERROR: MISSING_STYLE")
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "MISSING_STYLE",
                    "message": "Il capo base non ha uno stile valido."
                }
            )

        q = (
            db.collection("clothingItems")
            .where("userId", "==", userId)
            .where("isDirty", "==", False)
            .where("stagione", "==", stagione_l)
            .stream()
        )

        items = []
        for d in q:
            it = d.to_dict()
            if d.id == baseId:
                continue

            it["docId"] = d.id
            it["stile"] = normalize_stile(it.get("stile"))
            it["stagione"] = normalize_stagione(it.get("stagione"))
            it["colore"] = normalize_color(it.get("colore"))

            if it["stile"] == stile_l:
                items.append(it)

        base["docId"] = baseId
        base["stile"] = stile_l
        base["stagione"] = normalize_stagione(base.get("stagione"))
        base["colore"] = normalize_color(base.get("colore"))

        topBase = [i for i in items if i.get("categoria") == "topBase"]
        topLayer = [i for i in items if i.get("categoria") == "topLayer"]
        bottom = [i for i in items if i.get("categoria") == "bottom"]
        scarpe = [i for i in items if i.get("categoria") == "scarpe"]
        pezzo = [i for i in items if i.get("categoria") == "pezzoUnico"]

        print("=== QUICKPAIR DEBUG START ===")
        print("baseId =", baseId)
        print("base categoria =", base.get("categoria"))
        print("base nome =", base.get("nome"))
        print("base colore =", base.get("colore"))
        print("base stile =", stile_l)
        print("base stagione =", stagione_l)
        print("counts =", {
            "topBase": len(topBase),
            "topLayer": len(topLayer),
            "bottom": len(bottom),
            "scarpe": len(scarpe),
            "pezzoUnico": len(pezzo),
        })

        base_cat = base.get("categoria")

        if base_cat == "scarpe":
            has_min = (len(pezzo) >= 1) or (len(topBase) >= 1 and len(bottom) >= 1)

        elif base_cat == "topBase":
            has_min = (len(bottom) >= 1) and (len(scarpe) >= 1)

        elif base_cat == "bottom":
            has_min = (len(topBase) >= 1) and (len(scarpe) >= 1)

        elif base_cat == "pezzoUnico":
            has_min = (len(scarpe) >= 1)

        elif base_cat == "topLayer":
            has_min = (len(scarpe) >= 1) and (
                (len(pezzo) >= 1) or (len(topBase) >= 1 and len(bottom) >= 1)
            )

        else:
            has_min = False

        if not has_min:
            print("QUICKPAIR ERROR: FEW_ITEMS")
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "FEW_ITEMS",
                    "message": "Non ci sono abbastanza capi compatibili.",
                    "counts": {
                        "topBase": len(topBase),
                        "topLayer": len(topLayer),
                        "bottom": len(bottom),
                        "scarpe": len(scarpe),
                        "pezzoUnico": len(pezzo),
                    },
                    "baseCategoria": base_cat,
                }
            )

        _TOP_N = 3
        _SCORE_MARGIN = 1.5
        top_candidates = []   # list of (score, candidate), sorted desc, max _TOP_N elements
        rnd = random.Random(time.time())

        def _upd(candidate, score):
            top_candidates.append((score, candidate))
            top_candidates.sort(key=lambda x: x[0], reverse=True)
            del top_candidates[_TOP_N:]

        if base.get("categoria") == "pezzoUnico":
            for sh in scarpe:
                if not are_compatible(base.get("colore"), sh.get("colore")):
                    continue
                compat_layers = [l for l in topLayer if are_compatible(base.get("colore"), l.get("colore"))]
                layer, sc = _best_layer_for_outfit(
                    compat_layers,
                    piece=base,
                    shoes=sh,
                    target_style=stile_l,
                    base_item=base,
                )
                _upd({"base": base, "piece": base, "layer": layer, "shoes": sh, "top": None, "bottom": None}, sc)

        elif base.get("categoria") == "topBase":
            for b in (bottom if len(bottom) <= 25 else rnd.sample(bottom, 25)):
                if not are_compatible(base.get("colore"), b.get("colore")) and rnd.random() < 0.65:
                    continue

                compat_l = [
                    l for l in topLayer
                    if are_compatible(l.get("colore"), base.get("colore"))
                    or are_compatible(l.get("colore"), b.get("colore"))
                ]

                compat_s = [s for s in scarpe if are_compatible(b.get("colore"), s.get("colore"))] or scarpe
                sh = rnd.choice(compat_s)
                layer, sc = _best_layer_for_outfit(
                    compat_l,
                    top=base,
                    bottom=b,
                    shoes=sh,
                    target_style=stile_l,
                    base_item=base,
                )

                _upd(
                    {
                        "base": base,
                        "top": base,
                        "bottom": b,
                        "layer": layer,
                        "shoes": sh,
                        "piece": None
                    },
                    sc
                )

        elif base.get("categoria") == "bottom":
            for t in (topBase if len(topBase) <= 25 else rnd.sample(topBase, 25)):
                if not are_compatible(t.get("colore"), base.get("colore")) and rnd.random() < 0.65:
                    continue

                compat_l = [
                    l for l in topLayer
                    if are_compatible(l.get("colore"), t.get("colore"))
                    or are_compatible(l.get("colore"), base.get("colore"))
                ]

                compat_s = [s for s in scarpe if are_compatible(base.get("colore"), s.get("colore"))] or scarpe
                sh = rnd.choice(compat_s)
                layer, sc = _best_layer_for_outfit(
                    compat_l,
                    top=t,
                    bottom=base,
                    shoes=sh,
                    target_style=stile_l,
                    base_item=base,
                )

                _upd(
                    {
                        "base": base,
                        "top": t,
                        "bottom": base,
                        "layer": layer,
                        "shoes": sh,
                        "piece": None
                    },
                    sc
                )

        elif base.get("categoria") == "scarpe":
            for p in (pezzo if len(pezzo) <= 30 else rnd.sample(pezzo, 30)):
                if not are_compatible(p.get("colore"), base.get("colore")):
                    continue

                compat_layers = [
                    l for l in topLayer
                    if are_compatible(p.get("colore"), l.get("colore"))
                ]
                layer, sc = _best_layer_for_outfit(
                    compat_layers,
                    piece=p,
                    shoes=base,
                    target_style=stile_l,
                    base_item=base,
                )

                _upd(
                    {
                        "base": base,
                        "piece": p,
                        "layer": layer,
                        "shoes": base,
                        "top": None,
                        "bottom": None
                    },
                    sc
                )

            for t in (topBase if len(topBase) <= 20 else rnd.sample(topBase, 20)):
                for b in (bottom if len(bottom) <= 20 else rnd.sample(bottom, 20)):
                    if not (
                        are_compatible(t.get("colore"), b.get("colore"))
                        or are_compatible(b.get("colore"), base.get("colore"))
                    ):
                        continue

                    compat_l = [
                        l for l in topLayer
                        if are_compatible(l.get("colore"), t.get("colore"))
                        or are_compatible(l.get("colore"), b.get("colore"))
                    ]
                    layer, sc = _best_layer_for_outfit(
                        compat_l,
                        top=t,
                        bottom=b,
                        shoes=base,
                        target_style=stile_l,
                        base_item=base,
                    )

                    _upd(
                        {
                            "base": base,
                            "top": t,
                            "bottom": b,
                            "layer": layer,
                            "shoes": base,
                            "piece": None
                        },
                        sc
                    )

        elif base.get("categoria") == "topLayer":
            for t in (topBase if len(topBase) <= 20 else rnd.sample(topBase, 20)):
                for b in (bottom if len(bottom) <= 20 else rnd.sample(bottom, 20)):
                    compat_s = [s for s in scarpe if are_compatible(b.get("colore"), s.get("colore"))] or scarpe
                    sh = rnd.choice(compat_s)

                    sc = outfit_score(
                        top=t,
                        bottom=b,
                        shoes=sh,
                        layer=base,
                        target_style=stile_l,
                        base_item=base,
                    )

                    _upd(
                        {
                            "base": base,
                            "top": t,
                            "bottom": b,
                            "layer": base,
                            "shoes": sh,
                            "piece": None
                        },
                        sc
                    )

            for p in (pezzo if len(pezzo) <= 25 else rnd.sample(pezzo, 25)):
                compat_s = [s for s in scarpe if are_compatible(p.get("colore"), s.get("colore"))] or scarpe
                sh = rnd.choice(compat_s)

                sc = outfit_score(
                    piece=p,
                    shoes=sh,
                    layer=base,
                    target_style=stile_l,
                    base_item=base,
                )

                _upd(
                    {
                        "base": base,
                        "piece": p,
                        "layer": base,
                        "shoes": sh,
                        "top": None,
                        "bottom": None
                    },
                    sc
                )

        else:
            raise HTTPException(
                status_code=400,
                detail={"code": "UNSUPPORTED_CATEGORY", "message": "Categoria del capo base non gestita."}
            )

        if not top_candidates:
            print("quickpair nessun best trovato")
            return JSONResponse(
                content={"error": "Impossibile generare un outfit coerente.", "lang": lang}
            )

        best_score = top_candidates[0][0]

        # Filtra per margine qualità: scarta candidati troppo distanti dal top
        filtered = [(s, c) for s, c in top_candidates if s >= best_score - _SCORE_MARGIN]

        if len(filtered) == 1:
            selected_score, best = filtered[0]
            selected_rank = 1
        else:
            _sc  = [s for s, _ in filtered]
            _can = [c for _, c in filtered]
            _min = min(_sc)
            _w   = [s - _min + 0.1 for s in _sc]   # peso proporzionale, minimo 0.1
            _idx = rnd.choices(range(len(filtered)), weights=_w, k=1)[0]
            selected_score, best = filtered[_idx]
            selected_rank = _idx + 1   # rank 1-based

        print("quickpair best trovato = True")
        print("quickpair best_score =", best_score)
        print("quickpair selected_rank =", selected_rank, "of", len(filtered))
        print("quickpair best payload =", {
            "top":    (best.get("top") or {}).get("nome"),
            "bottom": (best.get("bottom") or {}).get("nome"),
            "piece":  (best.get("piece") or {}).get("nome"),
            "layer":  (best.get("layer") or {}).get("nome"),
            "shoes":  (best.get("shoes") or {}).get("nome"),
        })

        fallback_level = 0
        debug_reason = (
            f"same_style_pool;base_cat={base.get('categoria')};"
            f"items={len(items)};best_score={best_score:.4f};"
            f"topCandidates={len(filtered)};selectedRank={selected_rank}"
        )
        scores_payload = {"outfit": selected_score}

        parts = []

        def _fmt(it):
            if not it:
                return ""
            n = (it.get("nome") or "").strip()
            c = normalize_color(it.get("colore"))
            return f"{n} {c}".strip()

        slim_base = _slim(base)
        slim_top = _slim(best.get("top"))
        slim_bottom = _slim(best.get("bottom"))
        slim_piece = _slim(best.get("piece"))
        slim_layer = _slim(best.get("layer"))
        slim_shoes = _slim(best.get("shoes"))

        if slim_top and slim_top["id"] == slim_base["id"]:
            slim_top = None
        if slim_bottom and slim_bottom["id"] == slim_base["id"]:
            slim_bottom = None
        if slim_piece and slim_piece["id"] == slim_base["id"]:
            slim_piece = None
        if slim_layer and slim_layer["id"] == slim_base["id"]:
            slim_layer = None
        if slim_shoes and slim_shoes["id"] == slim_base["id"]:
            slim_shoes = None

        # Il capo cliccato deve sempre comparire in suggestion nel campo corretto.
        if base_cat == "topBase":
            slim_top = slim_base
        elif base_cat == "topLayer":
            slim_layer = slim_base
        elif base_cat == "bottom":
            slim_bottom = slim_base
        elif base_cat == "scarpe":
            slim_shoes = slim_base
        elif base_cat == "pezzoUnico":
            slim_piece = slim_base

        payload = {
            "base": slim_base,
            "suggestion": {
                "top": slim_top,
                "bottom": slim_bottom,
                "piece": slim_piece,
                "layer": slim_layer,
                "shoes": slim_shoes,
            },
            "lang": lang,
            "premium": is_premium,
            "fallbackLevel": fallback_level,
            "score": selected_score,
            "scores": scores_payload,
            "debugReason": debug_reason,
        }

        scelta = {}
        if slim_piece:
            scelta["pezzoUnicoImage"] = slim_piece["imageUrl"]
            scelta["topImage"] = None
            scelta["bottomImage"] = None
            parts.append("pezzo unico: " + _fmt(best.get("piece")))
        else:
            if slim_top:
                scelta["topImage"] = slim_top["imageUrl"]
                parts.append("top: " + _fmt(best.get("top")))
            else:
                scelta["topImage"] = None
            if slim_bottom:
                scelta["bottomImage"] = slim_bottom["imageUrl"]
                parts.append("bottom: " + _fmt(best.get("bottom")))
            else:
                scelta["bottomImage"] = None
            scelta["pezzoUnicoImage"] = None

        if slim_layer:
            scelta["topLayerImage"] = slim_layer["imageUrl"]
            parts.append("strato: " + _fmt(best.get("layer")))
        else:
            scelta["topLayerImage"] = None

        if slim_shoes:
            scelta["scarpeImage"] = slim_shoes["imageUrl"]
            parts.append("scarpe: " + _fmt(best.get("shoes")))
        else:
            scelta["scarpeImage"] = None

        description = fallback_description([p for p in parts if p], lang)

        payload.update(scelta)
        payload["descrizione"] = description

        return JSONResponse(content=payload)

    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

# ------------------------------
# 14) SOLO ARMADI PUBBLICI — /public_users e /public_wardrobe
#     (aggiunta normalizzazione nei confronti)
# ------------------------------
@app.get("/public_users")
async def public_users(
    q: str = Query("", description="ricerca nickname (contains, case-insensitive)"),
    stile: str = Query("", description="filtra utenti con >=1 capo di questo stile"),
    stagione: str = Query("", description="filtra utenti con >=1 capo di questa stagione"),
    limit: int = Query(30, ge=1, le=100)
):
    try:
        users_ref = db.collection("users").where("isPublic", "==", True).stream()
        out = []
        search = (q or "").strip().lower()
        stile_n = normalize_stile(stile) if stile else None
        stagione_n = normalize_stagione(stagione) if stagione else None

        for udoc in users_ref:
            u = udoc.to_dict()
            nickname = (u.get("nickname") or "")
            if search and search not in nickname.lower():
                continue

            uid = udoc.id

            # se richiesto, verifica che l'utente abbia almeno un capo che matcha i filtri
            if stile_n or stagione_n:
                cq = db.collection("clothingItems") \
                       .where("userId", "==", uid) \
                       .where("isDirty", "==", False) \
                       .stream()
                match_found = False
                for d in cq:
                    it = d.to_dict()
                    if it.get("isHiddenPublic") is True:
                        continue
                    it_stile = normalize_stile(it.get("stile"))
                    it_stagione = normalize_stagione(it.get("stagione"))
                    if stile_n and it_stile != stile_n:
                        continue
                    if stagione_n and it_stagione != stagione_n:
                        continue
                    match_found = True
                    break
                if not match_found:
                    continue

            out.append({
                "userId": uid,
                "nickname": nickname,
                "avatarUrl": u.get("avatarUrl", ""),
            })
            if len(out) >= limit:
                break

        return ok({"users": out})
    except Exception as e:
        return err(e)


@app.get("/public_wardrobe")
async def public_wardrobe(userId: str = Query(...)):
    """
    Ritorna i capi visibili dell'armadio di un utente pubblico:
    - users[userId].isPublic == True
    - clothingItems.isDirty == False
    - clothingItems.isHiddenPublic != True
    """
    try:
        user_doc = db.collection("users").document(userId).get()
        if not user_doc.exists or not user_doc.to_dict().get("isPublic", False):
            return ok({"items": []})

        q = db.collection("clothingItems") \
              .where("userId", "==", userId) \
              .where("isDirty", "==", False) \
              .stream()

        items = []
        for d in q:
            it = d.to_dict()
            if it.get("isHiddenPublic") is True:
                continue
            # normalizza per coerenza output
            it["stile"] = normalize_stile(it.get("stile"))
            it["stagione"] = normalize_stagione(it.get("stagione"))
            it["colore"] = normalize_color(it.get("colore"))
            items.append(it)

        # ordinamento semplice per categoria
        order = {"pezzoUnico": 0, "topLayer": 1, "topBase": 2, "bottom": 3, "scarpe": 4}
        items.sort(key=lambda it: order.get(it.get("categoria", ""), 99))

        return ok({"items": items})
    except Exception as e:
        return err(e)

# ------------------------------
# 15) Guardia: community OFF in modalità public-only
# ------------------------------
def _community_off():
    if COMMUNITY_PUBLIC_ONLY:
        raise HTTPException(status_code=503, detail="Community disabilitata: solo armadi pubblici attivi")

# ------------------------------
# 16) Endpoint community (presenti ma disattivati)
# ------------------------------
@app.post("/follow_user")
async def follow_user(userId: str = Form(...), targetUserId: str = Form(...)):
    _community_off()

@app.get("/followers")
async def get_followers(userId: str = Query(...)):
    _community_off()

@app.get("/following")
async def get_following(userId: str = Query(...)):
    _community_off()

@app.post("/like_story")
async def like_story(userId: str = Form(...), storyId: str = Form(...)):
    _community_off()

@app.post("/save_story")
async def save_story(userId: str = Form(...), storyId: str = Form(...)):
    _community_off()

@app.get("/saved_stories")
async def get_saved_stories(userId: str = Query(...)):
    _community_off()

@app.get("/top_stories")
async def get_top_stories(limit: int = 20):
    _community_off()

@app.get("/search_users")
async def search_users(query: str = Query(...)):
    _community_off()

@app.post("/mark_story_seen")
async def mark_story_seen(userId: str = Form(...), storyId: str = Form(...)):
    _community_off()

@app.get("/storie_viste")
async def get_storie_viste(userId: str = Query(...)):
    _community_off()

@app.get("/utenti_con_storie")
async def utenti_con_storie(viewerId: str = Query(...)):
    _community_off()
   
