import json
import os, uuid, random, time, signal, base64, io, re, asyncio
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from io import BytesIO
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, File, UploadFile, Form, Query, HTTPException, Body, Request
from pydantic import BaseModel, Field
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from PIL import Image, ImageOps
# from rembg import remove

import firebase_admin
from firebase_admin import credentials, firestore, storage
from firebase_admin import auth as firebase_auth
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

try:
    from google.oauth2 import service_account as gplay_service_account
    from googleapiclient.discovery import build as gplay_build
    from googleapiclient.errors import HttpError as GPlayHttpError
except Exception:
    gplay_service_account = None
    gplay_build = None
    GPlayHttpError = None

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


def _firestore_json_scalar(v):
    """Converte valori Firestore (datetime/Timestamp-like) in forma JSON-safe."""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    sec = getattr(v, "seconds", None)
    if isinstance(sec, int):
        ns = getattr(v, "nanoseconds", 0)
        try:
            ns_i = int(ns) if ns is not None else 0
            return datetime.fromtimestamp(sec + ns_i / 1e9, tz=timezone.utc).isoformat()
        except (TypeError, ValueError, OSError):
            pass
    tn = getattr(v, "timestamp", None)
    if callable(tn):
        try:
            ts = tn()
            if isinstance(ts, (int, float)):
                return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (TypeError, OSError, ValueError):
            pass
    return v


def _slim(it: dict | None):
    """Rende l'oggetto capo JSON-serializzabile (niente Timestamp ecc.)."""
    if not it:
        return None
    return {
        "id": _firestore_json_scalar(it.get("docId") or it.get("id")),
        "categoria": _firestore_json_scalar(it.get("categoria")),
        "nome": _firestore_json_scalar(it.get("nome")),
        "stile": _firestore_json_scalar(it.get("stile")),
        "colore": _firestore_json_scalar(it.get("colore")),
        "imageUrl": _firestore_json_scalar(it.get("imageUrl")),
    }

# ------------------------------
# 4) ENV & FLAGS
# ------------------------------
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
# Master switch: False disattiva le chiamate GPT negli endpoint (outfit AI-first, judge, helper testuali).
USE_GPT = False

GOOGLE_CREDENTIALS = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
FIREBASE_BUCKET = os.getenv("FIREBASE_BUCKET", "")

# Modalità community: TRUE => solo armadi pubblici; gli altri endpoint community rispondono 503
COMMUNITY_PUBLIC_ONLY = os.getenv("COMMUNITY_PUBLIC_ONLY", "true").lower() == "true"

# IAP Google Play: JSON service account su Render; package pubblicato su Play.
GOOGLE_PLAY_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "").strip()
ANDROID_PACKAGE_NAME = os.getenv("ANDROID_PACKAGE_NAME", "com.myvogueai.app").strip()
IAP_PRODUCT_IDS = frozenset({"premium_monthly", "premium_yearly"})

# Outfit Scan AI Vision (Step 4)
VISION_PROVIDER = os.getenv("VISION_PROVIDER", "openai").strip().lower()
VISION_API_KEY = os.getenv("VISION_API_KEY", "").strip()
OUTFIT_SCAN_USE_VISION = os.getenv("OUTFIT_SCAN_USE_VISION", "false").lower() == "true"
OUTFIT_SCAN_VISION_TIMEOUT_SEC = int(os.getenv("OUTFIT_SCAN_VISION_TIMEOUT_SEC", "25"))

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

# Legacy/env-only: usato solo se qualcosa chiamasse check_and_increment_quota_or_raise(feature="outfit").
# Il flusso GET /outfit FREE reale usa reserve_free_daily_or_raise (1 look/giorno, chiave freeDaily), non questo numero.
FREE_OUTFIT_LIMIT = int(os.getenv("FREE_OUTFIT_LIMIT", "8"))
QUICKPAIR_FREE_DAILY_LIMIT = int(os.getenv("QUICKPAIR_FREE_DAILY_LIMIT", "2"))  # free: max 2 QuickPair/giorno (feature "quickpair")
OUTFIT_SCAN_PREMIUM_DAILY_LIMIT = int(os.getenv("OUTFIT_SCAN_DAILY_LIMIT", "5"))


def check_and_increment_quota_or_raise(userId: str, feature: str, limit_free: int | None = None):
    """
    Controlla e incrementa la quota giornaliera per una feature.
    Se superata, solleva HTTPException 429 con payload JSON strutturato.
    """
    # Nota: nessun caller attuale usa feature="outfit"; FREE outfit usa freeDaily (vedi reserve_free_daily_or_raise).
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


def get_effective_premium_from_firestore(userId: str) -> bool:
    """Fonte unica per /outfit: non usare il flag premium inviato dal client."""
    try:
        udoc = db.collection("users").document(userId).get()
        if not udoc.exists:
            return False
        return bool((udoc.to_dict() or {}).get("isPremium", False))
    except Exception:
        return False


# ====================
# IAP — Google Play (subscriptions v2)
# ====================

_GPLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"


def _gplay_publisher():
    """Restituisce il client androidpublisher v3 o None se librerie/JSON assenti."""
    if not GOOGLE_PLAY_SERVICE_ACCOUNT_JSON or not gplay_build or not gplay_service_account:
        return None
    try:
        sa_info = json.loads(GOOGLE_PLAY_SERVICE_ACCOUNT_JSON)
    except json.JSONDecodeError:
        return None
    try:
        creds = gplay_service_account.Credentials.from_service_account_info(
            sa_info, scopes=[_GPLAY_SCOPE]
        )
        return gplay_build("androidpublisher", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        return None


def _parse_rfc3339_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        t = s.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def verify_google_play_subscription(product_id: str, purchase_token: str) -> dict:
    """
    Verifica abbonamento con purchases.subscriptionsv2.get.
    Ritorna dict: success, code, message, subscription_status, premium_until.
    Non logga purchase_token.
    """
    out = {
        "success": False,
        "code": "PLAY_VERIFY_FAILED",
        "message": "Google Play verification failed.",
        "subscription_status": None,
        "premium_until": None,
    }
    if not ANDROID_PACKAGE_NAME:
        out["code"] = "PLAY_MISCONFIGURED"
        out["message"] = "ANDROID_PACKAGE_NAME is not set."
        return out

    publisher = _gplay_publisher()
    if publisher is None:
        out["code"] = "PLAY_MISCONFIGURED"
        out["message"] = "Google Play credentials are not configured."
        return out

    if GPlayHttpError is None:
        out["message"] = "Google API client not available."
        return out

    try:
        req = (
            publisher.purchases()
            .subscriptionsv2()
            .get(packageName=ANDROID_PACKAGE_NAME, token=purchase_token)
        )
        sub = req.execute()
    except GPlayHttpError as e:
        try:
            status = int(getattr(getattr(e, "resp", None), "status", 0) or 0)
        except (TypeError, ValueError):
            status = 0
        if status == 404:
            out["code"] = "PLAY_PURCHASE_NOT_FOUND"
            out["message"] = "Purchase not found for this app."
        elif status in (401, 403):
            out["code"] = "PLAY_PERMISSION_DENIED"
            out["message"] = "Google Play API permission denied."
        else:
            out["code"] = "PLAY_VERIFY_FAILED"
            out["message"] = "Google Play API error."
        return out
    except Exception:
        out["message"] = "Google Play verification error."
        return out

    sub_state = (sub.get("subscriptionState") or "").strip() or None
    out["subscription_status"] = sub_state

    line_items = sub.get("lineItems") or []
    if not isinstance(line_items, list):
        line_items = []

    matching_expiries: list[datetime] = []
    for li in line_items:
        if not isinstance(li, dict):
            continue
        lid = (li.get("productId") or "").strip()
        if lid != product_id:
            continue
        exp = _parse_rfc3339_utc(li.get("expiryTime"))
        if exp:
            matching_expiries.append(exp)

    if not matching_expiries:
        out["code"] = "PLAY_PRODUCT_MISMATCH"
        out["message"] = "Subscription does not include this product."
        return out

    premium_until = max(matching_expiries)
    out["premium_until"] = premium_until
    now = datetime.now(timezone.utc)

    if sub_state == "SUBSCRIPTION_STATE_EXPIRED":
        out["code"] = "PLAY_SUBSCRIPTION_EXPIRED"
        out["message"] = "Subscription expired."
        return out
    if sub_state == "SUBSCRIPTION_STATE_PAUSED":
        out["code"] = "PLAY_SUBSCRIPTION_PAUSED"
        out["message"] = "Subscription is paused."
        return out

    if premium_until <= now:
        out["code"] = "PLAY_SUBSCRIPTION_EXPIRED"
        out["message"] = "Subscription period ended."
        return out

    if sub_state not in (
        "SUBSCRIPTION_STATE_ACTIVE",
        "SUBSCRIPTION_STATE_IN_GRACE_PERIOD",
        "SUBSCRIPTION_STATE_CANCELED",
    ):
        out["code"] = "PLAY_SUBSCRIPTION_NOT_ACTIVE"
        out["message"] = "Subscription state is not eligible."
        return out

    out["success"] = True
    out["code"] = "OK"
    out["message"] = "Verified with Google Play."
    return out


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

    @firestore.transactional
    def _reserve_txn(transaction, doc_ref, day_key: str):
        snap = doc_ref.get(transaction=transaction)
        data = dict(snap.to_dict()) if snap.exists else {}
        used = data.get(day_key, {}).get("freeDaily", 0)
        if used >= 1:
            detail = {
                "code": "QUOTA",
                "message": "Hai già ottenuto il suggerimento gratuito di oggi. Torna domani o passa a Premium.",
                "feature": "freeDaily",
                "limit": 1,
                "used": used,
                "remaining": 0,
                "resetAtLocal": f"{day_key}T23:59:59+02:00",  # mezzanotte Europe/Rome
            }
            raise HTTPException(status_code=429, detail=detail)
        day_bucket = dict(data.get(day_key) or {})
        day_bucket["freeDaily"] = used + 1
        data[day_key] = day_bucket
        transaction.set(doc_ref, data, merge=True)

    txn = db.transaction()
    _reserve_txn(txn, ref, day)

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


def reserve_outfit_scan_daily_or_raise(userId: str) -> dict:
    """
    Prenota uno slot Outfit Scan Premium per il giorno (Europe/Rome).
    Max OUTFIT_SCAN_PREMIUM_DAILY_LIMIT/giorno su quota/{userId}.outfitScan.
    """
    day = today_iso_rome()
    ref = db.collection("quota").document(userId)
    limit = OUTFIT_SCAN_PREMIUM_DAILY_LIMIT

    @firestore.transactional
    def _reserve_txn(transaction, doc_ref, day_key: str):
        snap = doc_ref.get(transaction=transaction)
        data = dict(snap.to_dict()) if snap.exists else {}
        used = int((data.get(day_key) or {}).get("outfitScan", 0) or 0)
        if used >= limit:
            detail = {
                "code": "QUOTA",
                "message": "Hai raggiunto il limite giornaliero di Outfit Scan. Riprova domani.",
                "feature": "outfitScan",
                "limit": limit,
                "used": used,
                "remaining": 0,
                "resetAtLocal": f"{day_key}T23:59:59+02:00",
            }
            raise HTTPException(status_code=429, detail=detail)
        new_used = used + 1
        day_bucket = dict(data.get(day_key) or {})
        day_bucket["outfitScan"] = new_used
        data[day_key] = day_bucket
        transaction.set(doc_ref, data, merge=True)
        return {
            "feature": "outfitScan",
            "limit": limit,
            "used": new_used,
            "remaining": max(0, limit - new_used),
            "resetAtLocal": f"{day_key}T23:59:59+02:00",
        }

    txn = db.transaction()
    return _reserve_txn(txn, ref, day)


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
    "marrone":     ["bianco", "beige", "verde", "blu", "grigio", "oliva", "senape", "arancione", "rosso"],
    "rosa":        ["bianco", "grigio", "blu", "nero", "beige"],
    "lilla":       ["bianco", "grigio", "blu", "nero"],
    "azzurro":     ["bianco", "beige", "grigio", "blu"],
    "oliva":       ["beige", "bianco", "nero", "marrone", "grigio", "senape", "arancione"],
    "bordeaux":    ["bianco", "nero", "beige", "grigio", "rosa"],
    "arancione":   ["blu", "bianco", "grigio", "nero", "beige"],
    "senape":      ["blu", "bianco", "grigio", "verde", "beige"],
    "avorio":      ["nero", "blu", "rosso", "verde", "beige", "grigio"],
    # navy: separato da blu generico (sinonimi in COLOR_SYNONYMS); abbinamenti classici premium
    "navy":        ["bianco", "beige", "grigio", "avorio", "marrone", "nero"],
    # trattiamo multicolore come neutro “aperto” per massimizzare gli abbinamenti
    "multicolore": ["nero", "bianco", "blu", "rosso", "verde", "beige", "grigio", "marrone", "azzurro"],
}

# Mappa sinonimi/varianti -> colore canonico della palette sopra
COLOR_SYNONYMS = {
    # blu e navy (navy è canonico separato; vedi BASIC_COLORS["navy"])
    "blu navy":          "navy",
    "navy":              "navy",
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

# Keyword “tecnico” per micro-regola formalità (evitare running in elegante).
_RUNNING_HEAVY_KW = (
    "da running",
    "running",
    "marathon",
    "trail run",
    "gara",
    "spikes",
    "spike",
)

# Outer leggero “chic” (non parka pesante: escluso da TOPLAYER_TOO_HEAVY via check).
_CHIC_LIGHT_OUTER_KW = (
    "trench",
    "caban",
    "peacoat",
    "duster",
    "cappotto",
)

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

    if style in ("elegante", "casual") and _has_any(text, _CHIC_LIGHT_OUTER_KW) and not _has_any(
        text, TOPLAYER_TOO_HEAVY
    ):
        score += 0.2
    if style in ("casual", "streetwear") and any(
        k in text for k in ("denim", "di jeans", "jeans")
    ) and any(k in text for k in ("giacca", "jacket", "giubb", "giubbotto")):
        score += 0.12

    return score


def _light_formality_tune(shoes, layer, target_style) -> float:
    """Micro-aggiustamento formalità; contributo piccolo e clampato."""
    s = normalize_stile(target_style)
    if not s:
        return 0.0
    stxt = _item_text(shoes) if shoes else ""
    ltxt = _item_text(layer) if layer else ""
    adj = 0.0
    if s == "elegante" and shoes and _has_any(stxt, _RUNNING_HEAVY_KW):
        adj -= 0.24
    if s == "elegante" and shoes and layer and _has_any(
        ltxt, ("blazer", "sartoriale", "doppiopetto", "monopetto")
    ):
        if _has_any(stxt, SPORT_SHOES) and not _has_any(stxt, STREET_SHOES_GOOD):
            adj -= 0.2
    if s in ("casual", "streetwear") and shoes and layer:
        if any(k in ltxt for k in ("denim", "giacca denim", "di jeans", "jeans jacket")):
            if _has_any(stxt, ("sneaker", "sneakers")) and not _has_any(stxt, _RUNNING_HEAVY_KW):
                adj += 0.12
    return max(-0.28, min(0.28, adj))
    
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

    colors: list[str] = []
    for it in items:
        raw = _color_raw_for_score_v1(it)
        if raw:
            nc = normalize_color(raw)
            if nc != "sconosciuto":
                colors.append(nc)
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
            score -= 1.2    # tre neutri diversi senza accento: look troppo piatto (non tocca monocromie già premiate sotto)

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


def _pattern_mix_penalty(top=None, bottom=None, piece=None, shoes=None, layer=None) -> float:
    """Opzionale: se il campo pattern è presente, penalizza 2+ motivi non tinta unita. Senza campo = neutral (retro-compat)."""
    solids = frozenset({"unito", "solid", "tinta unita", "tinta unità", "plain", "liscio"})
    non_solid = 0
    for it in (top, bottom, piece, shoes, layer):
        if not it:
            continue
        raw = it.get("pattern")
        if raw is None or str(raw).strip() == "":
            continue
        p = str(raw).strip().lower()
        if p in solids:
            continue
        non_solid += 1
    if non_solid >= 2:
        return -0.45
    return 0.0


_VALID_QUICKPAIR_OCCASIONS = frozenset({
    "everyday", "work", "evening", "elegant", "casual", "rainy", "cold", "warm",
})


def _normalize_quickpair_occasion(raw: str | None) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip().lower()
    return s if s in _VALID_QUICKPAIR_OCCASIONS else None


def _occasion_items_blob(
    *,
    top=None,
    bottom=None,
    piece=None,
    layer=None,
    shoes=None,
) -> str:
    chunks: list[str] = []
    for it in (piece, top, bottom, layer, shoes):
        if not it or not isinstance(it, dict):
            continue
        nome = str(it.get("nome") or "").lower()
        cat = str(it.get("categoria") or "").lower()
        chunks.append(f"{cat} {nome}")
    return " ".join(chunks)


def _score_occasion_fit(
    *,
    top=None,
    bottom=None,
    piece=None,
    layer=None,
    shoes=None,
    occasion: str | None,
) -> float:
    """Piccolo aggiustamento (-1.5 … +1.5) per contesto QuickPair; occasion=None → 0."""
    if not occasion:
        return 0.0
    b = _occasion_items_blob(top=top, bottom=bottom, piece=piece, layer=layer, shoes=shoes).lower()

    def has(*needles: str) -> bool:
        return any(n in b for n in needles)

    sc = 0.0

    if occasion in {"work", "elegant", "evening"}:
        if has("running", "runner", "track", "training", "ginnastic", "tennis", "trail"):
            sc -= 0.85
        if has("felpa", "hoodie", "cappuccio", "tuta", "leggings"):
            sc -= 0.55
        if has("blazer", "camicia"):
            sc += 0.45
        if has("mocassin", "mocassino", "loafer", "oxford", "derby", "décolleté", "decollete", "tacco"):
            sc += 0.5

    if occasion == "evening":
        if has("blazer", "vestito", "abito"):
            sc += 0.35

    if occasion in {"casual", "everyday"}:
        if has("jeans", "denim", "t-shirt", "tshirt", "maglietta", "sneaker", "sneakers"):
            sc += 0.35
        if has("pigiama", "slides", "ciabatte", "ciabatta"):
            sc -= 0.55

    if occasion == "rainy":
        if has("stival", "anfibio", "rain", "trench", "impermeabile", "goretex", "gomma"):
            sc += 0.75
        if has("sandalo", "sandali", "zeppa", "slides", "ciabatta"):
            sc -= 0.85

    if occasion == "cold":
        if layer is not None:
            sc += 0.45
        if has("cappotto", "piumino", "parka", "maglion", "pile", "cardigan", "lana"):
            sc += 0.5

    if occasion == "warm":
        if layer is not None and has("cappotto", "piumino", "parka", "shearling", "pelliccia", "imbottito"):
            sc -= 0.75

    return max(-1.5, min(1.5, sc))


def _collect_outfit_colors_and_items(outfit_parts: dict) -> tuple[list[str], list[dict]]:
    colors: list[str] = []
    items: list[dict] = []
    for key in ("pezzoUnico", "top", "bottom", "layer", "shoes"):
        it = outfit_parts.get(key)
        if not it or not isinstance(it, dict):
            continue
        items.append(it)
        raw = _color_raw_for_score_v1(it)
        if raw:
            nc = normalize_color(raw)
            if nc != "sconosciuto":
                colors.append(nc)
    return colors, items


def _styling_reason_fallback_lang(lang: str) -> str:
    lc = (lang or "it").lower()
    if lc.startswith("en"):
        return "A balanced mix of your pieces, tuned so colors and proportions work together."
    if lc.startswith("es"):
        return "Una mezcla equilibrada de tus prendas, afinada para que color y proporciones encajen."
    return "Un equilibrio tra i tuoi capi, calibrato perché colori e proporzioni funzionino insieme."


def _build_styling_reason(
    outfit_parts: dict,
    *,
    target_style: str | None,
    lang: str,
    base_item: dict | None = None,
) -> str:
    """Breve spiegazione locale (no GPT), backward-compatible."""
    lc = (lang or "it").lower()
    if lc.startswith("en"):
        lang_code = "en"
    elif lc.startswith("es"):
        lang_code = "es"
    else:
        lang_code = "it"

    def T(it_txt: str, en_txt: str, es_txt: str) -> str:
        if lang_code == "en":
            return en_txt
        if lang_code == "es":
            return es_txt
        return it_txt

    try:
        colors, items = _collect_outfit_colors_and_items(outfit_parts)
        if not items:
            return _styling_reason_fallback_lang(lang)

        neutrals = {"nero", "bianco", "grigio", "beige", "avorio", "marrone", "crema", "cammello"}
        accents = {"rosso", "rosa", "lilla", "verde", "oliva", "giallo", "senape", "arancione", "bordeaux", "azzurro", "blu", "fucsia"}

        navy_like = any(c == "navy" for c in colors)
        neutral_count = sum(1 for c in colors if c in neutrals)
        accent_count = sum(1 for c in colors if c in accents)

        shoes = outfit_parts.get("shoes")
        layer = outfit_parts.get("layer")
        piece = outfit_parts.get("pezzoUnico")

        shoe_light = False
        if shoes:
            scol = normalize_color(_color_raw_for_score_v1(shoes) or "")
            shoe_light = scol in {"bianco", "avorio", "beige", "crema"}

        layer_nm = str((layer or {}).get("nome") or "").lower()
        formal_layer = layer and any(
            k in layer_nm for k in ("blazer", "giacca", "soprabito", "cappotto", "trench")
        )

        if base_item and isinstance(base_item, dict):
            bn = str(base_item.get("nome") or "").strip()
            bc_raw = _color_raw_for_score_v1(base_item) or base_item.get("colore") or ""
            bc = normalize_color(bc_raw) if bc_raw else ""
            color_hint = f" ({bc})" if bc and bc != "sconosciuto" else ""
            if bn:
                if navy_like and shoe_light:
                    return T(
                        f"Intorno a {bn}{color_hint}, il navy tiene il look ordinato mentre le scarpe chiare lo rendono più fresco.",
                        f"Around {bn}{color_hint}, navy keeps the palette tidy while lighter shoes freshen the result.",
                        f"Con {bn}{color_hint}, el navy mantiene el conjunto ordenado y el calzado claro lo aligera.",
                    )
                if layer:
                    layer_title = str(layer.get("nome") or "").strip()
                    if layer_title:
                        return T(
                            f"Intorno a {bn}, {layer_title} aggiunge struttura al look senza appesantire i colori.",
                            f"Around {bn}, {layer_title} adds structure without weighing down the outfit.",
                            f"Con {bn}, {layer_title} da estructura al look sin cargar los colores.",
                        )
                    return T(
                        f"Intorno a {bn}, il capo sopra aggiunge equilibrio al look senza renderlo pesante.",
                        f"Around {bn}, the piece on top balances the outfit without feeling heavy.",
                        f"Con {bn}, la prenda de arriba equilibra el look sin resultar pesado.",
                    )
                return T(
                    f"Intorno a {bn}, tonalità e proporzioni restano bilanciate per un abbinamento credibile.",
                    f"Around {bn}, tones and proportions stay balanced for a believable pairing.",
                    f"Con {bn}, tonos y proporciones se mantienen equilibrados para un conjunto creíble.",
                )

        if formal_layer:
            return T(
                "La giacca dà struttura al look; pantaloni e scarpe tengono ordinati i colori.",
                "The jacket structures the look, while pants and shoes keep the palette tidy.",
                "La chaqueta estructura el look; pantalón y zapatos mantienen los colores ordenados.",
            )

        if navy_like:
            return T(
                "Il navy tiene uniti i colori del look; gli altri capi completano senza creare confusione.",
                "Navy ties the outfit colors together; the other pieces complete it without visual clutter.",
                "El navy une los colores del look; el resto completa sin crear confusión.",
            )

        if neutral_count >= 2 and accent_count == 1:
            return T(
                "La base a toni neutri tiene pulito il look e il tocco di colore lo scala senza stravolgerlo.",
                "A neutral base keeps the outfit clean, and the color accent lifts it without overpowering.",
                "La base neutra mantiene limpio el conjunto y el toque de color lo eleva sin dominar.",
            )

        if shoe_light and len(colors) >= 2:
            return T(
                "Le scarpe chiare alleggeriscono il look e bilanciano i toni più marcati degli altri capi.",
                "Light shoes brighten the look and balance stronger tones from the other pieces.",
                "El calzado claro aligera el look y equilibra tonos más marcados del resto.",
            )

        style_eff = normalize_stile(target_style) or ""

        if layer and piece:
            return T(
                "Il capo principale resta al centro e il capo sopra completa il look senza appesantirlo.",
                "The main piece stays central and the layer on top completes without overpowering.",
                "La prenda principal sigue al centro y la de arriba completa sin cargar el look.",
            )

        if layer and style_eff in {"casual", "streetwear", "sportivo"}:
            return T(
                "Il capo sopra permette di variare volume e tessuti mantenendo scarpe e pantalone coerenti.",
                "A piece on top varies volume and texture while keeping shoes and trousers coherent.",
                "La prenda superior permite variar volumen y textura manteniendo zapato y pantalón coherentes.",
            )

        shoe_nm = str((shoes or {}).get("nome") or "").lower()
        if style_eff == "elegante" and shoes and any(
            k in shoe_nm for k in ("mocassin", "mocassino", "loafer", "tacco", "decollete", "décolleté")
        ):
            return T(
                "Scarpe più pulite e linee classiche mantengono il look elegante ma ancora portabile ogni giorno.",
                "Cleaner shoes and classic lines keep the outfit refined without feeling overly stiff.",
                "Zapatos sobrios y líneas clásicas mantienen el look elegante sin resultar rígido.",
            )

        return _styling_reason_fallback_lang(lang)
    except Exception:
        return _styling_reason_fallback_lang(lang)


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
    br = _color_raw_for_score_v1(base_item) or str(base_item.get("colore") or "").strip() or ""
    score = 0.0

    def colr(it):
        if not it:
            return ""
        return _color_raw_for_score_v1(it) or str(it.get("colore") or "").strip() or ""

    if base_cat == "bottom":
        if top:
            score += color_relation_score(br, colr(top)) * 2.0
        if shoes:
            score += color_relation_score(br, colr(shoes)) * 1.8
        if layer and top:
            score += max(
                color_relation_score(colr(layer), colr(top)),
                color_relation_score(colr(layer), br)
            ) * 0.8

    elif base_cat == "topBase":
        if bottom:
            score += color_relation_score(br, colr(bottom)) * 2.0
        if layer:
            score += color_relation_score(br, colr(layer)) * 1.0
        if shoes and bottom:
            score += color_relation_score(colr(bottom), colr(shoes)) * 1.2

    elif base_cat == "scarpe":
        if bottom:
            score += color_relation_score(br, colr(bottom)) * 2.4
        if top and bottom:
            score += color_relation_score(colr(top), colr(bottom)) * 1.0
        if layer and top:
            score += color_relation_score(colr(layer), colr(top)) * 0.5
        if piece:
            score += color_relation_score(br, colr(piece)) * 2.0

    elif base_cat == "topLayer":
        if top:
            score += color_relation_score(br, colr(top)) * 1.8
        if bottom and top:
            score += color_relation_score(colr(top), colr(bottom)) * 1.2
        if shoes and bottom:
            score += color_relation_score(colr(bottom), colr(shoes)) * 0.8
        if piece:
            score += color_relation_score(br, colr(piece)) * 1.5

    elif base_cat == "pezzoUnico":
        if shoes:
            score += color_relation_score(br, colr(shoes)) * 2.3
        if layer:
            score += color_relation_score(br, colr(layer)) * 1.0

    return score


# Archetipi outfit (QuickPair / outfit_score v1): bonus piccoli, hard cap globale.
_ARCH_MAX_TOTAL = 0.15

_ARCH_DENIM_LAYER_KW = (
    "denim",
    "jeans",
    "giacca di jeans",
    "giacca denim",
    "denim jacket",
    "giubbotto di jeans",
)

_ARCH_FORMAL_SHOE_KW = (
    "mocassino",
    "mocassini",
    "loafer",
    "loafers",
    "derby",
    "oxford",
    "stringata",
    "stringate",
    "francesina",
)


def archetype_combo_bonus(items: dict, target_style: str | None, base_item: dict | None) -> float:
    """
    Bonus sagomati su combo classiche reali. Max totale _ARCH_MAX_TOTAL.
    Richiede top + bottom + scarpe; niente bonus su categorie incoerenti.
    """
    _ = base_item
    top = items.get("top")
    bottom = items.get("bottom")
    shoes = items.get("shoes")
    layer = items.get("layer")
    piece = items.get("piece")

    if piece or not top or not bottom or not shoes:
        return 0.0
    if bottom.get("categoria") != "bottom" or top.get("categoria") != "topBase":
        return 0.0
    if shoes.get("categoria") != "scarpe":
        return 0.0
    if layer is not None and layer.get("categoria") != "topLayer":
        return 0.0

    tgt = normalize_stile(target_style)

    def _nec(d: dict | None) -> str | None:
        if not d:
            return None
        raw = effective_color(d) or d.get("colore")
        if raw is None or (isinstance(raw, str) and not str(raw).strip()):
            return None
        n = normalize_color(raw)
        return n if n != "sconosciuto" else None

    def _sig_bottom_earth(b: dict) -> bool:
        raw = f"{b.get('colore') or ''} {b.get('nome') or ''}".lower()
        t = _nec(b)
        if t in ("marrone", "beige"):
            return True
        if any(k in raw for k in ("cognac", "cuoio", "camel", "cammello")):
            return True
        return False

    def _sig_top_white_light(t: dict) -> bool:
        raw = (t.get("nome") or "").lower()
        tc = _nec(t)
        if tc in ("bianco", "avorio"):
            return True
        if "avorio" in raw or "bianco" in raw:
            return True
        return False

    def _sig_layer_denim(l: dict | None) -> bool:
        if not l:
            return False
        if l.get("categoria") != "topLayer":
            return False
        txt = _item_text(l)
        if not any(k in txt for k in _ARCH_DENIM_LAYER_KW):
            return False
        lc = _nec(l) or normalize_color(l.get("colore"))
        return lc in ("blu", "azzurro", "nero") or "blu" in txt or "navy" in txt

    def _sig_shoes_white_or_sneaker(s: dict) -> bool:
        txt = _item_text(s)
        sc = _nec(s)
        if sc == "bianco":
            return True
        if _has_any(
            txt,
            ("sneaker", "sneakers", "trainer", "running", "skate", "tennis", "basket"),
        ):
            return True
        return False

    def _sig_shoes_formal(s: dict) -> bool:
        txt = _item_text(s)
        return any(k in txt for k in _ARCH_FORMAL_SHOE_KW)

    def _sig_shoes_earth_leather(s: dict) -> bool:
        raw = f"{s.get('colore') or ''} {s.get('nome') or ''}".lower()
        sc = _nec(s)
        if sc == "marrone":
            return True
        if any(k in raw for k in ("cognac", "cuoio", "camel", "mocassin", "mocassino")):
            return True
        return _sig_shoes_formal(s)

    def _sig_top_shirtish(t: dict) -> bool:
        raw = (t.get("nome") or "").lower()
        if "camicia" in raw or "shirt" in raw:
            return True
        return _sig_top_white_light(t)

    def _sig_layer_blazer_navy_black(l: dict | None) -> bool:
        if not l or l.get("categoria") != "topLayer":
            return False
        txt = _item_text(l)
        if not any(k in txt for k in ("blazer", "giacca sartoriale", "doppiopetto", "monopetto")):
            return False
        lc = _nec(l) or normalize_color(l.get("colore"))
        raw = f"{l.get('colore') or ''} {l.get('nome') or ''}".lower()
        if lc in ("nero", "blu") or "navy" in raw:
            return True
        return False

    bonuses: list[float] = []

    # A) terra + top chiaro + denim + sneakers/bianco · casual / streetwear
    if tgt in ("casual", "streetwear"):
        sig_a = (
            int(_sig_bottom_earth(bottom)),
            int(_sig_top_white_light(top)),
            int(_sig_layer_denim(layer)),
            int(_sig_shoes_white_or_sneaker(shoes)),
            int(tgt in ("casual", "streetwear")),
        )
        if (
            sum(sig_a) >= 3
            and (_sig_shoes_white_or_sneaker(shoes) or _sig_layer_denim(layer))
        ):
            bonuses.append(0.15)

    # B) nero + camicia/bianco + blazer + formale · elegante
    if tgt == "elegante":
        sig_b = (
            int(_nec(bottom) == "nero"),
            int(_sig_top_shirtish(top)),
            int(_sig_layer_blazer_navy_black(layer)),
            int(_sig_shoes_formal(shoes)),
            int(tgt == "elegante"),
        )
        if sum(sig_b) >= 3 and (
            _sig_shoes_formal(shoes) or _sig_layer_blazer_navy_black(layer)
        ):
            bonuses.append(0.15)

    # C) beige + camicia/bianco/azzurro + scarpe cuoio · casual / elegante
    if tgt in ("casual", "elegante"):
        tc = _nec(top)
        top_azz_bianco = tc in ("bianco", "avorio", "azzurro") or _sig_top_shirtish(top)
        sig_c = (
            int(_nec(bottom) == "beige"),
            int(top_azz_bianco),
            int(_sig_shoes_earth_leather(shoes)),
            int(tgt in ("casual", "elegante")),
        )
        if sum(sig_c) >= 3 and _sig_shoes_earth_leather(shoes):
            bonuses.append(0.12)

    btxt = (bottom.get("nome") or "").lower()

    def _sig_shoes_boot_clean(s: dict) -> bool:
        txt = _item_text(s)
        return any(k in txt for k in ("chelsea", "stivaletto", "stival", "ankle boot", "beatle"))

    # D) Jeans / denim + top chiaro + sneaker · casual / streetwear
    if tgt in ("casual", "streetwear"):
        jeansish = "jeans" in btxt or "denim" in btxt or _nec(bottom) in ("blu", "azzurro")
        sig_d = (
            int(jeansish),
            int(_sig_top_white_light(top)),
            int(_sig_shoes_white_or_sneaker(shoes)),
            int(tgt in ("casual", "streetwear")),
        )
        if sum(sig_d) >= 3 and _sig_shoes_white_or_sneaker(shoes):
            bonuses.append(0.10)

    # E) Blu / navy smart + camicia + scarpe formali
    if tgt in ("elegante", "casual"):
        navyish = "navy" in btxt or _nec(bottom) == "blu"
        sig_e = (
            int(navyish),
            int(_sig_top_shirtish(top)),
            int(_sig_shoes_formal(shoes)),
            int(tgt in ("elegante", "casual")),
        )
        if sum(sig_e) >= 3 and _sig_shoes_formal(shoes):
            bonuses.append(0.10)

    # F) Grigio + top neutro + scarpe nere
    if tgt in ("casual", "elegante", "streetwear"):
        tn = _nec(top)
        sig_f = (
            int(_nec(bottom) == "grigio"),
            int(tn in ("bianco", "avorio", "nero", "grigio") if tn else False),
            int(_nec(shoes) == "nero"),
            int(tgt in ("casual", "elegante", "streetwear")),
        )
        if sum(sig_f) >= 3 and _nec(shoes) == "nero":
            bonuses.append(0.08)

    # G) Oliva / military + top chiaro + sneaker
    if tgt in ("casual", "streetwear"):
        olivaish = _nec(bottom) == "oliva" or "oliva" in btxt or "military" in btxt
        sig_g = (
            int(olivaish),
            int(_sig_top_white_light(top)),
            int(_sig_shoes_white_or_sneaker(shoes)),
        )
        if sum(sig_g) >= 3 and _sig_shoes_white_or_sneaker(shoes):
            bonuses.append(0.10)

    # H) Nero bottom + top chiaro + stivaletti puliti
    if tgt in ("casual", "elegante"):
        sig_h = (
            int(_nec(bottom) == "nero"),
            int(_sig_top_white_light(top)),
            int(_sig_shoes_boot_clean(shoes)),
        )
        if sum(sig_h) >= 3 and _sig_shoes_boot_clean(shoes):
            bonuses.append(0.10)

    if not bonuses:
        return 0.0
    return min(_ARCH_MAX_TOTAL, max(bonuses))


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

    _PREMIUM = {
        frozenset({"nero", "bianco"}): 0.90,
        frozenset({"navy", "bianco"}): 0.88,
        frozenset({"beige", "avorio"}): 0.87,
        frozenset({"beige", "bianco"}): 0.87,
        frozenset({"blu", "bianco"}): 0.86,
        frozenset({"navy", "avorio"}): 0.86,
        frozenset({"beige", "navy"}): 0.86,
        frozenset({"marrone", "beige"}): 0.86,
        frozenset({"beige", "nero"}): 0.85,
        frozenset({"navy", "beige"}): 0.84,
        frozenset({"nero", "bordeaux"}): 0.84,
        frozenset({"grigio", "bordeaux"}): 0.82,
        frozenset({"grigio", "blu"}): 0.82,
        frozenset({"oliva", "beige"}): 0.80,
    }
    pair = frozenset({t1, t2})
    if pair in _PREMIUM:
        return _PREMIUM[pair]

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


def _color_raw_for_score_v1(item) -> str | None:
    """Colore testuale per outfit_score v1 / QuickPair: `effective_color` batte il campo DB."""
    if not item or not isinstance(item, dict):
        return None
    ec = effective_color(item)
    if ec and str(ec).strip():
        return ec
    c = item.get("colore")
    if c is None or (isinstance(c, str) and not str(c).strip()):
        return None
    return str(c).strip()


# =============================================================================
# DEPRECATED / DO NOT USE IN PRODUCTION
#
# outfit_score_v2 is an experimental scoring engine with a different scale [-1, +1].
# The production engine is outfit_score(...), which is cumulative and not clamped.
# Do not replace outfit_score calls with outfit_score_v2 without a full migration plan
# and golden regression tests.
#
# Keep this function only as historical/reference code until the scoring engine is
# either fully migrated or the function is safely removed after regression coverage.
# =============================================================================

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
    base_item: dict | None = None,
    apply_archetype: bool = True,
):
    """
    Score totale outfit:
    1) compatibilità colore
    2) piccolo bias su palette preferita
    3) peso stilistico reale di scarpe e topLayer

    Golden set futuro (regressione qualità): mantenere 30–50 outfit noti valutati 10/10 e
    rilanciare lo scoring dopo ogni tweak; non richiede implementazione ora (evitare script/framework pesanti).

    TODO(future diversity): penalità ripetizione itemIds vs storico, forzare 2–3 alternative,
    diversità silhouette — solo se senza query Firestore extra o refactor ampio (oggi: avoid_recent / excludeIds / refreshSeed).
    """
    score = 0.0

    # =========================
    # 1) Compatibilità colore (graduata)
    # =========================
    if piece and shoes:
        score += color_relation_score(_color_raw_for_score_v1(piece), _color_raw_for_score_v1(shoes)) * 1.4
    if top and bottom:
        score += color_relation_score(_color_raw_for_score_v1(top), _color_raw_for_score_v1(bottom)) * 1.4
    if bottom and shoes:
        score += color_relation_score(_color_raw_for_score_v1(bottom), _color_raw_for_score_v1(shoes)) * 1.4
    if top and layer:
        score += color_relation_score(_color_raw_for_score_v1(top), _color_raw_for_score_v1(layer)) * 0.7
    if bottom and layer:
        score += color_relation_score(_color_raw_for_score_v1(bottom), _color_raw_for_score_v1(layer)) * 0.7
    if piece and layer:
        score += color_relation_score(_color_raw_for_score_v1(piece), _color_raw_for_score_v1(layer)) * 0.7

    # =========================
    # 2) Bias cromatico
    # =========================
    if prefer_palette:
        for it in (piece, top, bottom, shoes, layer):
            if not it:
                continue
            raw = _color_raw_for_score_v1(it)
            if raw and normalize_color(raw) in prefer_palette:
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
        score += _light_formality_tune(shoes, layer, style_eff)

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

    if base_item and target_style:
        base_style = normalize_stile(base_item.get("stile"))
        target_norm = normalize_stile(target_style)
        if base_style and target_norm and base_style != target_norm:
            score -= 1.5

    if apply_archetype:
        score += archetype_combo_bonus(
            {"top": top, "bottom": bottom, "shoes": shoes, "layer": layer, "piece": piece},
            target_style,
            base_item,
        )

    score += _pattern_mix_penalty(top, bottom, piece, shoes, layer)

    return score


_LAYER_MIN_GAIN = 0.08


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
        apply_archetype=True,
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
            apply_archetype=True,
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
    if not USE_GPT:
        return None
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
    if not USE_GPT:
        return None
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


class IapVerifyBody(BaseModel):
    """Payload client per POST /iap/verify (nessun flag premium dal client)."""

    platform: str | None = None
    productId: str = Field(..., min_length=1)
    purchaseToken: str | None = None
    verificationData: str | None = None
    purchaseId: str | None = None
    source: str | None = None


@app.post("/iap/verify")
async def iap_verify(request: Request, body: IapVerifyBody):
    """
    Verifica Firebase ID token + Google Play subscriptions v2; grant solo se valido.
    """
    auth_header = request.headers.get("Authorization") or ""
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        return JSONResponse(
            status_code=401,
            content={
                "granted": False,
                "code": "MISSING_OR_INVALID_AUTHORIZATION",
                "message": "Missing bearer token.",
            },
        )
    id_token = auth_header[len(prefix) :].strip()
    if not id_token:
        return JSONResponse(
            status_code=401,
            content={
                "granted": False,
                "code": "MISSING_OR_INVALID_AUTHORIZATION",
                "message": "Empty bearer token.",
            },
        )

    try:
        decoded = firebase_auth.verify_id_token(id_token)
        uid = decoded.get("uid")
        if not uid:
            return JSONResponse(
                status_code=401,
                content={
                    "granted": False,
                    "code": "INVALID_FIREBASE_TOKEN",
                    "message": "Invalid token payload.",
                },
            )
    except Exception:
        return JSONResponse(
            status_code=401,
            content={
                "granted": False,
                "code": "INVALID_FIREBASE_TOKEN",
                "message": "Invalid Firebase ID token.",
            },
        )

    if body.productId not in IAP_PRODUCT_IDS:
        return JSONResponse(
            status_code=200,
            content={
                "granted": False,
                "code": "INVALID_PRODUCT_ID",
                "message": "Product is not allowed.",
            },
        )

    store_token = (body.purchaseToken or body.verificationData or "").strip()
    if not store_token:
        return JSONResponse(
            status_code=200,
            content={
                "granted": False,
                "code": "MISSING_PURCHASE_TOKEN",
                "message": "Missing purchase token.",
            },
        )

    src = (body.source or body.platform or "").strip().lower()
    if src and src != "google_play":
        return JSONResponse(
            status_code=200,
            content={
                "granted": False,
                "code": "PLATFORM_NOT_SUPPORTED",
                "message": "Only google_play is supported for verification.",
            },
        )

    if not GOOGLE_PLAY_SERVICE_ACCOUNT_JSON:
        return JSONResponse(
            status_code=200,
            content={
                "granted": False,
                "code": "IAP_VERIFICATION_NOT_CONFIGURED",
                "message": "Google Play service account is not configured on the server.",
            },
        )

    print(f"=== /iap/verify uid={uid} productId={body.productId} package={ANDROID_PACKAGE_NAME!r} ===")

    v = verify_google_play_subscription(body.productId, store_token)
    if not v["success"]:
        return JSONResponse(
            status_code=200,
            content={
                "granted": False,
                "code": v["code"],
                "message": v["message"],
            },
        )

    token_hash = sha256(store_token.encode("utf-8")).hexdigest()
    premium_until = v["premium_until"]
    try:
        ref = db.collection("users").document(uid)
        update = {
            "isPremium": True,
            "subscriptionStatus": v.get("subscription_status"),
            "lastProductId": body.productId,
            "purchaseTokenHash": token_hash,
            "premiumUpdatedAt": firestore.SERVER_TIMESTAMP,
        }
        if premium_until is not None:
            update["premiumUntil"] = premium_until
        ref.set(update, merge=True)
    except Exception:
        return JSONResponse(
            status_code=200,
            content={
                "granted": False,
                "code": "FIRESTORE_WRITE_FAILED",
                "message": "Could not save subscription state.",
            },
        )

    return JSONResponse(
        status_code=200,
        content={
            "granted": True,
            "code": "OK",
            "message": v.get("message") or "Premium activated.",
        },
    )


# ------------------------------
# 12) /upload — Upload immagine + background removal + Storage
# (legacy endpoint; Flutter currently uploads directly to Firebase Storage)
# ------------------------------

@app.post("/upload")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    userId_query: str | None = Query(None, alias="userId"),
    userId_form: str | None = Form(None, alias="userId"),
):
    try:
        uid = require_firebase_uid(request)
        print("[/upload] authenticated uid=", uid)
        print("=== ENTER /upload ===")

        q = (userId_query or "").strip() or None
        f = (userId_form or "").strip() or None
        if q and f and q != f:
            raise HTTPException(status_code=400, detail="Conflicting userId in query and form.")
        claimed_user_id = q or f
        if claimed_user_id and claimed_user_id != uid:
            raise HTTPException(
                status_code=403,
                detail="Forbidden: userId does not match authenticated user.",
            )

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

        file_uid = str(uuid.uuid4())
        path = f"items/{uid}/final_{file_uid}.png"

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


def require_uid_match(request: Request, user_id: str) -> str:
    """Verifica Authorization: Bearer <firebase_id_token> e che uid == user_id."""
    auth_header = request.headers.get("Authorization") or ""
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
    id_token = auth_header[len(prefix) :].strip()
    if not id_token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase ID token.")
    uid = decoded.get("uid")
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase ID token.")
    if uid != user_id:
        raise HTTPException(status_code=403, detail="Forbidden: userId does not match authenticated user.")
    return uid


def require_firebase_uid(request: Request) -> str:
    """Bearer Firebase ID token → uid (senza confronto con query userId)."""
    auth_header = request.headers.get("Authorization") or ""
    prefix = "Bearer "
    if not auth_header.startswith(prefix):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
    id_token = auth_header[len(prefix) :].strip()
    if not id_token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header.")
    try:
        decoded = firebase_auth.verify_id_token(id_token)
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase ID token.")
    uid = decoded.get("uid")
    if not uid:
        raise HTTPException(status_code=401, detail="Invalid or expired Firebase ID token.")
    return uid


def _log_outfit_suggestion_safe(
    *,
    user_id: str,
    source: str,
    outfit_parts: dict,
    style: str | None = None,
    season: str | None = None,
    is_premium: bool | None = None,
    base_item_id: str | None = None,
    base_category: str | None = None,
    score: float | None = None,
) -> str | None:
    """
    Best-effort: scrive outfitSuggestions/{autoId}. Non propagare eccezioni.
    outfit_parts usa chiavi come _is_valid_outfit: pezzoUnico, top, bottom, layer, shoes.
    """
    try:
        if source not in ("outfit", "quickpair"):
            return None
        order = ("pezzoUnico", "top", "bottom", "layer", "shoes")
        item_ids: list[str] = []
        categories: list[str] = []
        colors: list[str] = []
        for key in order:
            it = outfit_parts.get(key)
            if not it:
                continue
            raw_id = it.get("docId") or it.get("id")
            if raw_id:
                item_ids.append(str(raw_id))
            cat = it.get("categoria")
            if cat:
                categories.append(str(cat))
            rc = _color_raw_for_score_v1(it)
            if rc:
                nc = normalize_color(rc)
                if nc and nc != "sconosciuto":
                    colors.append(nc)
        sty = style.strip() if style else None
        sea = season.strip() if season else None
        doc_ref = db.collection("outfitSuggestions").document()
        doc_ref.set({
            "userId": user_id,
            "source": source,
            "createdAt": firestore.SERVER_TIMESTAMP,
            "updatedAt": firestore.SERVER_TIMESTAMP,
            "style": sty,
            "stile": sty,
            "season": sea,
            "stagione": sea,
            "isPremium": is_premium,
            "baseItemId": base_item_id,
            "baseCategory": base_category,
            "itemIds": item_ids,
            "categories": categories,
            "colors": colors,
            "score": score,
            "status": "shown",
            "feedback": None,
            "feedbackReason": None,
            "acceptedAt": None,
            "ignoredAt": None,
            "regeneratedAt": None,
            "version": "styling_v1",
        })
        return doc_ref.id
    except Exception as ex:
        print(f"[outfitSuggestions] log skipped: {ex}")
        return None


OUTFIT_FEEDBACK_ACTIONS = frozenset({"accepted", "ignored", "regenerated"})
OUTFIT_FEEDBACK_REASONS = frozenset({
    "too_casual",
    "too_formal",
    "colors_bad",
    "colors_good_fit_bad",
    "shoes_wrong",
    "already_seen",
    "other",
})


class OutfitFeedbackIn(BaseModel):
    suggestionId: str = Field(..., min_length=1)
    action: str
    reason: str | None = None


@app.post("/outfit_feedback")
async def outfit_feedback(request: Request, body: OutfitFeedbackIn):
    uid = require_firebase_uid(request)
    action = (body.action or "").strip()
    if action not in OUTFIT_FEEDBACK_ACTIONS:
        raise HTTPException(status_code=400, detail="Invalid action.")
    reason = body.reason
    if reason is not None:
        reason = str(reason).strip()
        if reason == "":
            reason = None
        elif reason not in OUTFIT_FEEDBACK_REASONS:
            raise HTTPException(status_code=400, detail="Invalid reason.")

    sid = body.suggestionId.strip()
    ref = db.collection("outfitSuggestions").document(sid)
    snap = ref.get()
    if not snap.exists:
        raise HTTPException(status_code=404, detail="Suggestion not found.")
    data = snap.to_dict() or {}
    if data.get("userId") != uid:
        raise HTTPException(status_code=403, detail="Forbidden.")

    upd: dict = {
        "feedback": action,
        "feedbackReason": reason,
        "updatedAt": firestore.SERVER_TIMESTAMP,
    }
    if action == "accepted":
        upd["acceptedAt"] = firestore.SERVER_TIMESTAMP
    elif action == "ignored":
        upd["ignoredAt"] = firestore.SERVER_TIMESTAMP
    else:
        upd["regeneratedAt"] = firestore.SERVER_TIMESTAMP
    ref.update(upd)
    return JSONResponse(content={"ok": True})


# ------------------------------
# 13) /outfit — scelta colori + varietà (con lingua + quota + premium)
# ------------------------------
@app.get("/outfit")
async def genera_outfit(
    request: Request,
    stile: str = Query(None, description="Ignorato per utenti free; usato solo per premium."),
    stagione: str = Query(...),
    userId: str = Query(...),
    lang: str = Query(None),
    premium: bool = Query(False),
    charge: bool = Query(False),  # legacy client flag; il ramo free usa solo freeDaily (non incrementa quote "outfit")

    # === Parametri opzionali retro-compatibili (Punto 2) ===
    maxOutfits: int = Query(3, ge=1, le=3),   # premium: quanti outfit provare a restituire
    compact: bool = Query(False),             # se True, payload alleggerito
    noAI: bool = Query(True),                 # forza fallback manuale (utile per test/latenza)
    preferColors: str = Query("", description="comma-separated, e.g. 'blu,beige'"),
    excludeIds: str = Query("", description="comma-separated item ids to avoid"),
    refreshSeed: str = Query("", description="seed opzionale per refresh deterministico"),
):
    require_uid_match(request, userId)
    # parsing veloce (Punti 2 e 4)
    prefer_palette = [normalize_color(c) for c in (preferColors or "").split(",") if c.strip()]
    exclude_set = set([x.strip() for x in (excludeIds or "").split(",") if x.strip()])

    try:
        # Normalizzazioni base e lingua
        stile_l_req = normalize_stile(stile) if stile else None
        stagione_l = normalize_stagione(stagione)
        lang = (lang or get_user_lang(userId, "it")).lower()
        lang_name = LANG_NAME.get(lang, "italiano")

        client_premium_flag = bool(premium)
        db_premium = get_effective_premium_from_firestore(userId)
        effective_premium = db_premium
        print(
            "OUTFIT PREMIUM CHECK "
            f"userId={userId} clientPremium={client_premium_flag} "
            f"dbPremium={db_premium} effectivePremium={effective_premium}"
        )

        # =========================
        # RAMO PREMIUM
        # =========================
        if effective_premium:
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
            ai_result = _ai_choose_outfits(
                articoli, stile_l_req, stagione_l, lang, lang_name, n=maxOutfits
            )

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
            if USE_GPT and not noAI:
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

                scelta["descrizione"] = description
                scelta["consiglioExtra"] = consiglio_extra
                scelta["lang"] = lang
                sid = _log_outfit_suggestion_safe(
                    user_id=userId,
                    source="outfit",
                    outfit_parts=o,
                    style=stile_l_req,
                    season=stagione_l,
                    is_premium=True,
                    score=c.get("score"),
                )
                if sid:
                    scelta["suggestionId"] = sid
                scelta["stylingReason"] = _build_styling_reason(
                    {
                        "pezzoUnico": o["pezzoUnico"],
                        "top": o["top"],
                        "bottom": o["bottom"],
                        "layer": o["layer"],
                        "shoes": o["shoes"],
                    },
                    target_style=stile_l_req,
                    lang=lang,
                )
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

            sid_free = _log_outfit_suggestion_safe(
                user_id=userId,
                source="outfit",
                outfit_parts=best,
                style=stile_l,
                season=stagione_l,
                is_premium=False,
                score=best_score,
            )
            if sid_free:
                payload["suggestionId"] = sid_free

            payload["stylingReason"] = _build_styling_reason(
                {
                    "pezzoUnico": best["pezzoUnico"],
                    "top": best["top"],
                    "bottom": best["bottom"],
                    "layer": best["layer"],
                    "shoes": best["shoes"],
                },
                target_style=stile_l,
                lang=lang,
            )

            # Compact mode per il ramo free (Punto 7 — facoltativo)
            if compact:
                for k in list(payload.keys()):
                    if k not in ("pezzoUnicoImage","topImage","bottomImage","topLayerImage","scarpeImage","descrizione","lang","freeDaily","premium","suggestionId","stylingReason"):
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
    if not USE_GPT:
        return None
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
    request: Request,
    userId: str = Query(...),
    baseId: str = Query(..., description="document id di clothingItems"),
    stagione: str = Query(...),
    lang: str = Query(None),
    occasion: str = Query(
        "",
        description="Optional context (TODO: Flutter UI): everyday|work|evening|elegant|casual|rainy|cold|warm.",
    ),
):
    require_uid_match(request, userId)
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
        _QUICKPAIR_DOMINANCE_GAP = 0.38
        top_candidates = []   # list of (score, candidate), sorted desc, max _TOP_N elements
        rnd = random.Random(time.time())

        occasion_n = _normalize_quickpair_occasion((occasion or "").strip() or None)

        def _occasion_bonus_qp(cand: dict) -> float:
            if not occasion_n:
                return 0.0
            return _score_occasion_fit(
                top=cand.get("top"),
                bottom=cand.get("bottom"),
                piece=cand.get("piece"),
                layer=cand.get("layer"),
                shoes=cand.get("shoes"),
                occasion=occasion_n,
            )

        def _upd(candidate, score):
            adj = score + _occasion_bonus_qp(candidate)
            top_candidates.append((adj, candidate))
            top_candidates.sort(key=lambda x: x[0], reverse=True)
            del top_candidates[_TOP_N:]

        _QUICKPAIR_SHOE_K = 3

        def _rank_shoe_candidates(compat_s, score_one_shoe):
            if len(compat_s) <= _QUICKPAIR_SHOE_K:
                return list(compat_s)
            ranked = [(score_one_shoe(sh), sh) for sh in compat_s]
            ranked.sort(key=lambda x: x[0], reverse=True)
            return [sh for _, sh in ranked[:_QUICKPAIR_SHOE_K]]

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

                def _pre_shoe_topbase(shoe):
                    return outfit_score(
                        top=base,
                        bottom=b,
                        shoes=shoe,
                        layer=None,
                        prefer_palette=None,
                        target_style=stile_l,
                        base_item=base,
                        apply_archetype=False,
                    )

                for sh in _rank_shoe_candidates(compat_s, _pre_shoe_topbase):
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

                def _pre_shoe_bottom(shoe):
                    return outfit_score(
                        top=t,
                        bottom=base,
                        shoes=shoe,
                        layer=None,
                        prefer_palette=None,
                        target_style=stile_l,
                        base_item=base,
                        apply_archetype=False,
                    )

                for sh in _rank_shoe_candidates(compat_s, _pre_shoe_bottom):
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
            scarpe_pool = []
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

                scarpe_pool.append((
                    sc,
                    {
                        "base": base,
                        "piece": p,
                        "layer": layer,
                        "shoes": base,
                        "top": None,
                        "bottom": None,
                    },
                ))

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

                    scarpe_pool.append((
                        sc,
                        {
                            "base": base,
                            "top": t,
                            "bottom": b,
                            "layer": layer,
                            "shoes": base,
                            "piece": None,
                        },
                    ))

            scarpe_pool.sort(key=lambda x: x[0], reverse=True)
            for sc, cand in scarpe_pool[:_TOP_N]:
                _upd(cand, sc)

        elif base.get("categoria") == "topLayer":
            for t in (topBase if len(topBase) <= 20 else rnd.sample(topBase, 20)):
                for b in (bottom if len(bottom) <= 20 else rnd.sample(bottom, 20)):
                    compat_s = [s for s in scarpe if are_compatible(b.get("colore"), s.get("colore"))] or scarpe

                    def _pre_shoe_tl_tb(shoe):
                        return outfit_score(
                            top=t,
                            bottom=b,
                            shoes=shoe,
                            layer=base,
                            prefer_palette=None,
                            target_style=stile_l,
                            base_item=base,
                            apply_archetype=False,
                        )

                    for sh in _rank_shoe_candidates(compat_s, _pre_shoe_tl_tb):
                        sc = outfit_score(
                            top=t,
                            bottom=b,
                            shoes=sh,
                            layer=base,
                            target_style=stile_l,
                            base_item=base,
                            apply_archetype=True,
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

                def _pre_shoe_tl_piece(shoe):
                    return outfit_score(
                        piece=p,
                        shoes=shoe,
                        layer=base,
                        prefer_palette=None,
                        target_style=stile_l,
                        base_item=base,
                        apply_archetype=False,
                    )

                for sh in _rank_shoe_candidates(compat_s, _pre_shoe_tl_piece):
                    sc = outfit_score(
                        piece=p,
                        shoes=sh,
                        layer=base,
                        target_style=stile_l,
                        base_item=base,
                        apply_archetype=True,
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
            _uniq = sorted({s for s, _ in filtered}, reverse=True)
            if (
                len(_uniq) >= 2
                and (_uniq[0] - _uniq[1]) >= _QUICKPAIR_DOMINANCE_GAP
            ):
                selected_score, best = next((s, c) for s, c in filtered if s == _uniq[0])
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

        outfit_for_log = {
            "pezzoUnico": best.get("piece"),
            "top": best.get("top"),
            "bottom": best.get("bottom"),
            "layer": best.get("layer"),
            "shoes": best.get("shoes"),
        }
        sid_qp = _log_outfit_suggestion_safe(
            user_id=userId,
            source="quickpair",
            outfit_parts=outfit_for_log,
            style=stile_l,
            season=stagione_l,
            is_premium=is_premium,
            base_item_id=baseId,
            base_category=base_cat,
            score=selected_score,
        )
        if sid_qp:
            payload["suggestionId"] = sid_qp

        payload["stylingReason"] = _build_styling_reason(
            outfit_for_log,
            target_style=stile_l,
            lang=lang,
            base_item=base,
        )

        return JSONResponse(content=payload)

    except HTTPException:
        raise
    except Exception as e:
        return JSONResponse(content={"error": str(e)}, status_code=500)

# ------------------------------
# 13-ter) /outfit-scan — Outfit Scan (mock o AI Vision)
# ------------------------------
_OUTFIT_SCAN_VALID_CATEGORIE = frozenset({"topBase", "topLayer", "bottom", "scarpe", "pezzoUnico"})
_OUTFIT_SCAN_VALID_STILI = frozenset({"casual", "elegante", "streetwear", "sportivo"})
_OUTFIT_SCAN_VALID_STAGIONI = frozenset({"primavera", "estate", "autunno", "inverno"})
_OUTFIT_SCAN_VISION_MAX_EDGE = 1280
_OUTFIT_SCAN_VISION_MIN_CONFIDENCE = 0.4
_OUTFIT_SCAN_CROP_POLICY = {
    "topBase": {"enabled": True, "min_conf": 0.65, "padding": 0.10, "edge_margin": 0.02},
    "bottom": {"enabled": True, "min_conf": 0.65, "padding": 0.10, "edge_margin": 0.02},
    "topLayer": {"enabled": True, "min_conf": 0.70, "padding": 0.12, "edge_margin": 0.02},
    "pezzoUnico": {"enabled": True, "min_conf": 0.75, "padding": 0.15, "edge_margin": 0.02},
    "scarpe": {"enabled": False},
}
_OUTFIT_SCAN_CROP_GEOMETRY = {
    "topBase": {"min_area": 0.008, "max_area": 0.55, "min_aspect": 0.35, "max_aspect": 2.8, "max_cy": 0.72},
    "bottom": {"min_area": 0.008, "max_area": 0.55, "min_aspect": 0.25, "max_aspect": 1.8, "min_y": 0.18, "max_y_end": 0.98, "max_cy": 0.88},
    "topLayer": {"min_area": 0.010, "max_area": 0.65, "min_aspect": 0.35, "max_aspect": 2.8, "max_cy": 0.75},
    "pezzoUnico": {"min_area": 0.015, "max_area": 0.80, "min_aspect": 0.20, "max_aspect": 1.2},
}
_OUTFIT_SCAN_CROP_MIN_BOX_DIM = 0.04
_OUTFIT_SCAN_CROP_MIN_PIXELS = 50
_OUTFIT_SCAN_CROP_MAX_OUTPUT_ASPECT = 3.5
_OUTFIT_SCAN_CROP_TIME_RESERVE_SEC = 8.0
_OUTFIT_SCAN_MAX_CROP_ITEMS = 6


def _outfit_scan_mock_items() -> list[dict]:
    return [
        {
            "tempId": "mock-1",
            "nome": "Maglietta bianca",
            "categoria": "topBase",
            "colore": "bianco",
            "stile": "casual",
            "stagione": "estate",
            "reviewStatus": "pending",
            "scanConfidence": 0.91,
            "scanRawLabel": "white t-shirt",
        },
        {
            "tempId": "mock-2",
            "nome": "Jeans blu",
            "categoria": "bottom",
            "colore": "blu",
            "stile": "casual",
            "stagione": "estate",
            "reviewStatus": "pending",
            "scanConfidence": 0.88,
            "scanRawLabel": "blue jeans",
        },
        {
            "tempId": "mock-3",
            "nome": "Sneakers nere",
            "categoria": "scarpe",
            "colore": "nero",
            "stile": "casual",
            "stagione": "estate",
            "reviewStatus": "pending",
            "scanConfidence": 0.85,
            "scanRawLabel": "black sneakers",
        },
    ]


def _outfit_scan_vision_prompt(lang: str) -> str:
    lang_name = "italiano" if lang.startswith("it") else "English"
    return (
        "Analizza la foto outfit. Devi riconoscere solo capi realmente visibili.\n"
        "Rispondi SOLO con JSON valido, senza markdown, senza testo aggiuntivo.\n"
        "Preferisci il formato: {\"items\": [...]} (max 6 elementi).\n"
        "Un array JSON semplice è accettabile.\n\n"
        f"Scrivi i nomi dei capi in {lang_name}.\n\n"
        "NON includere: borse, cinture, occhiali, cappelli, gioielli, orologi, calze, collant, "
        "accessori piccoli, volto, corpo, ambiente, sfondo, specchio.\n"
        "NON inventare capi non chiaramente visibili.\n"
        "NON includere capi coperti al 70% o più.\n\n"
        "Ogni elemento in items:\n"
        "{\n"
        '  "nome": "...",\n'
        '  "categoria": "topBase | topLayer | bottom | scarpe | pezzoUnico",\n'
        '  "colore": "...",\n'
        '  "stile": "casual | elegante | streetwear | sportivo",\n'
        '  "stagione": "primavera | estate | autunno | inverno",\n'
        '  "scanConfidence": 0.0,\n'
        '  "scanRawLabel": "...",\n'
        '  "cropBox": {"x": 0.0, "y": 0.0, "width": 0.0, "height": 0.0},\n'
        '  "cropConfidence": 0.0\n'
        "}\n\n"
        "scanConfidence:\n"
        "- 0.9 se capo chiaramente visibile e identificabile\n"
        "- 0.7 se visibile ma parzialmente coperto\n"
        "- 0.5 se incerto\n"
        "- non includere capi sotto 0.4\n\n"
        "cropBox e cropConfidence:\n"
        "- Le coordinate cropBox sono normalizzate 0.0-1.0 sull'immagine analizzata "
        "(dopo orientamento EXIF e ridimensionamento lato server, non l'originale full-res).\n"
        "- x,y = angolo superiore sinistro; width,height = dimensioni del rettangolo.\n"
        "- Includi cropBox solo se il capo è sufficientemente visibile e il box è affidabile.\n"
        "- NON inventare box. Se il box è incerto, ometti cropBox e cropConfidence.\n"
        "- cropConfidence 0.0-1.0: confidenza sul bounding box.\n"
        "- Non includere cropBox con cropConfidence sotto 0.6.\n"
        "- Il box deve contenere principalmente il capo, non volto/sfondo/specchio.\n"
        "- Per tutti i capi: se il capo tocca il bordo dell'immagine, ometti cropBox.\n"
        "- Meglio omettere cropBox che restituire un box parziale o incerto.\n"
        "- Per scarpe/sneakers: cropBox solo se entrambe le scarpe sono interamente visibili.\n"
        "- Per scarpe/sneakers: se anche una scarpa è tagliata dal bordo foto, ometti cropBox.\n\n"
        "Regole categoriche:\n"
        "- blazer, giacca, cappotto, cardigan, giubbotto = topLayer\n"
        "- camicia, t-shirt, maglia, polo, top = topBase\n"
        "- pantaloni, jeans, shorts, gonna = bottom\n"
        "- scarpe, sneakers, stivali, mocassini = scarpe\n"
        "- vestito/tuta intera = pezzoUnico"
    )


def _load_outfit_scan_image(image_path: str) -> tuple[Image.Image, str, str]:
    """Scarica da Storage, EXIF fix, resize max edge; ritorna PIL RGB, base64 JPEG, mime."""
    blob = bucket.blob(image_path)
    if not blob.exists():
        raise FileNotFoundError(f"image not found: {image_path}")
    data = blob.download_as_bytes()
    img = Image.open(BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif img.mode == "L":
        img = img.convert("RGB")
    w, h = img.size
    max_edge = max(w, h)
    if max_edge > _OUTFIT_SCAN_VISION_MAX_EDGE:
        scale = _OUTFIT_SCAN_VISION_MAX_EDGE / max_edge
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    out = BytesIO()
    img.save(out, format="JPEG", quality=85, optimize=True)
    out.seek(0)
    return img, base64.b64encode(out.read()).decode("ascii"), "image/jpeg"


def _download_storage_image_b64(image_path: str) -> tuple[str, str]:
    _, b64, mime = _load_outfit_scan_image(image_path)
    return b64, mime


def _vision_call_with_timeout(fn, *args, timeout_sec: float, **kwargs):
    """Hard timeout lato codice: backup a request_timeout/urllib che possono non essere affidabili."""
    ex = ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn, *args, timeout_sec=timeout_sec, **kwargs)
    try:
        result = fut.result(timeout=timeout_sec)
    except FuturesTimeoutError as e:
        ex.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError(f"vision exceeded {timeout_sec}s") from e
    except Exception:
        ex.shutdown(wait=True)
        raise
    else:
        ex.shutdown(wait=True)
        return result


def _extract_json_from_vision_text(text: str):
    raw = (text or "").strip()
    if not raw:
        raise ValueError("empty vision response")
    fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.DOTALL | re.IGNORECASE)
    if fenced:
        raw = fenced.group(1).strip()
    return json.loads(raw)


def _normalize_outfit_scan_categoria(categoria: str, nome: str, scan_raw) -> str:
    cat = (categoria or "").strip()
    if cat in _OUTFIT_SCAN_VALID_CATEGORIE:
        return cat
    text = f"{nome} {scan_raw or ''} {cat}".lower()
    rules = (
        ("pezzoUnico", ("vestito", "tuta intera", "dress", "jumpsuit", "romper")),
        ("scarpe", ("scarpe", "sneakers", "stivali", "mocassini", "shoes", "boots", "loafers")),
        ("topLayer", ("blazer", "giacca", "cappotto", "cardigan", "giubbotto", "jacket", "coat")),
        ("bottom", ("pantaloni", "jeans", "shorts", "gonna", "pants", "trousers", "skirt")),
        ("topBase", ("camicia", "t-shirt", "tshirt", "maglia", "polo", " shirt", "tee", "blouse", "top")),
    )
    for cat_name, keywords in rules:
        if any(k in text for k in keywords):
            return cat_name
    return "topBase"


def _parse_outfit_scan_crop_confidence(raw: dict) -> float | None:
    if "cropConfidence" not in raw:
        return None
    try:
        conf = float(raw.get("cropConfidence"))
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, conf))


def _outfit_scan_crop_policy(categoria: str) -> dict:
    return _OUTFIT_SCAN_CROP_POLICY.get(categoria, _OUTFIT_SCAN_CROP_POLICY["topBase"])


def _outfit_scan_log_crop_skip(categoria: str, reason: str, temp_id: str | None = None) -> None:
    suffix = f" tempId={temp_id}" if temp_id else ""
    print(f"[outfit-scan] crop skipped category={categoria} reason={reason}{suffix}")


def _outfit_scan_box_near_edge(box: dict, edge_margin: float) -> bool:
    x, y, w, h = box["x"], box["y"], box["width"], box["height"]
    if x < edge_margin or y < edge_margin:
        return True
    if (x + w) > (1.0 - edge_margin):
        return True
    if (y + h) > (1.0 - edge_margin):
        return True
    return False


def _outfit_scan_box_aspect(box: dict) -> float:
    w, h = box["width"], box["height"]
    if h <= 0:
        return 0.0
    return w / h


def _outfit_scan_validate_crop_box(
    box: dict,
    categoria: str,
    crop_confidence: float | None,
) -> tuple[dict | None, str | None]:
    policy = _outfit_scan_crop_policy(categoria)
    if not policy.get("enabled", False):
        return None, "disabled"
    if crop_confidence is None:
        return None, "missing_crop_confidence"
    min_conf = policy.get("min_conf", 0.65)
    if crop_confidence < min_conf:
        return None, f"low_crop_confidence<{min_conf}"
    edge_margin = policy.get("edge_margin", 0.02)
    if _outfit_scan_box_near_edge(box, edge_margin):
        return None, "edge_margin"
    geom = _OUTFIT_SCAN_CROP_GEOMETRY.get(categoria, {})
    area = box["width"] * box["height"]
    min_area = geom.get("min_area", 0.008)
    max_area = geom.get("max_area", 0.55)
    if area < min_area:
        return None, "area_too_small"
    if area > max_area:
        return None, "area_too_large"
    aspect = _outfit_scan_box_aspect(box)
    min_aspect = geom.get("min_aspect", 0.2)
    max_aspect = geom.get("max_aspect", 3.0)
    if aspect < min_aspect or aspect > max_aspect:
        return None, "aspect_ratio"
    cy = box["y"] + box["height"] / 2.0
    if "max_cy" in geom and cy > geom["max_cy"]:
        return None, "vertical_position"
    if "min_y" in geom and box["y"] < geom["min_y"]:
        return None, "vertical_position"
    if "max_y_end" in geom and (box["y"] + box["height"]) > geom["max_y_end"]:
        return None, "vertical_position"
    if box["width"] < _OUTFIT_SCAN_CROP_MIN_BOX_DIM or box["height"] < _OUTFIT_SCAN_CROP_MIN_BOX_DIM:
        return None, "box_too_small"
    return box, None


def _parse_outfit_scan_crop_box(
    raw: dict,
    crop_confidence: float | None,
    categoria: str,
    temp_id: str | None = None,
) -> dict | None:
    box_raw = raw.get("cropBox")
    if not isinstance(box_raw, dict):
        return None
    try:
        x = float(box_raw.get("x"))
        y = float(box_raw.get("y"))
        width = float(box_raw.get("width"))
        height = float(box_raw.get("height"))
    except (TypeError, ValueError):
        _outfit_scan_log_crop_skip(categoria, "invalid_box_values", temp_id)
        return None
    if any(v < 0.0 or v > 1.0 for v in (x, y, width, height)):
        _outfit_scan_log_crop_skip(categoria, "box_out_of_range", temp_id)
        return None
    if x + width > 1.0001 or y + height > 1.0001:
        _outfit_scan_log_crop_skip(categoria, "box_out_of_range", temp_id)
        return None
    box = {
        "x": round(x, 6),
        "y": round(y, 6),
        "width": round(width, 6),
        "height": round(height, 6),
    }
    validated, reason = _outfit_scan_validate_crop_box(box, categoria, crop_confidence)
    if validated is None and reason:
        _outfit_scan_log_crop_skip(categoria, reason, temp_id)
    return validated


def _apply_outfit_scan_crop_padding(box: dict, categoria: str) -> dict:
    policy = _outfit_scan_crop_policy(categoria)
    padding = policy.get("padding", 0.10)
    x, y, w, h = box["x"], box["y"], box["width"], box["height"]
    pad_w = w * padding
    pad_h = h * padding
    x1 = max(0.0, x - pad_w)
    y1 = max(0.0, y - pad_h)
    x2 = min(1.0, x + w + pad_w)
    y2 = min(1.0, y + h + pad_h)
    return {"x": x1, "y": y1, "width": x2 - x1, "height": y2 - y1}


def _outfit_scan_validate_padded_box(padded: dict, categoria: str) -> str | None:
    policy = _outfit_scan_crop_policy(categoria)
    edge_margin = policy.get("edge_margin", 0.02)
    if _outfit_scan_box_near_edge(padded, edge_margin):
        return "padded_edge_touch"
    aspect = _outfit_scan_box_aspect(padded)
    if aspect > 0:
        long_side = max(aspect, 1.0 / aspect)
        if long_side > _OUTFIT_SCAN_CROP_MAX_OUTPUT_ASPECT:
            return "padded_strip"
    return None


def _crop_outfit_scan_image(
    img: Image.Image,
    box: dict,
    categoria: str,
) -> tuple[Image.Image | None, str | None]:
    padded = _apply_outfit_scan_crop_padding(box, categoria)
    padded_reason = _outfit_scan_validate_padded_box(padded, categoria)
    if padded_reason:
        return None, padded_reason
    iw, ih = img.size
    left = max(0, int(padded["x"] * iw))
    top = max(0, int(padded["y"] * ih))
    right = min(iw, max(left + 1, int(round((padded["x"] + padded["width"]) * iw))))
    bottom = min(ih, max(top + 1, int(round((padded["y"] + padded["height"]) * ih))))
    if right - left < _OUTFIT_SCAN_CROP_MIN_PIXELS or bottom - top < _OUTFIT_SCAN_CROP_MIN_PIXELS:
        return None, "output_too_small"
    cropped = img.crop((left, top, right, bottom))
    cw, ch = cropped.size
    if max(cw, ch) / max(1, min(cw, ch)) > _OUTFIT_SCAN_CROP_MAX_OUTPUT_ASPECT:
        return None, "output_strip"
    return cropped, None


def _jpeg_bytes_from_outfit_scan_crop(img: Image.Image) -> bytes:
    out = BytesIO()
    rgb = img.convert("RGB") if img.mode != "RGB" else img
    rgb.save(out, format="JPEG", quality=88, optimize=True)
    return out.getvalue()


def _upload_outfit_scan_crop(uid: str, scan_session_id: str, temp_id: str, jpeg_bytes: bytes) -> str:
    safe_scan = re.sub(r"[^a-zA-Z0-9_-]", "_", scan_session_id)[:64]
    safe_temp = re.sub(r"[^a-zA-Z0-9_-]", "_", temp_id)[:64]
    path = f"users/{uid}/clothing_items/outfit_scan_{safe_scan}_{safe_temp}.jpg"
    blob = bucket.blob(path)
    token = str(uuid.uuid4())
    blob.metadata = {"firebaseStorageDownloadTokens": token}
    blob.upload_from_string(jpeg_bytes, content_type="image/jpeg")
    return (
        f"https://firebasestorage.googleapis.com/v0/b/{FIREBASE_BUCKET}"
        f"/o/{path.replace('/', '%2F')}?alt=media&token={token}"
    )


def _outfit_scan_attach_crop_fields(item: dict, crop_box: dict | None, crop_confidence: float | None) -> None:
    categoria = item.get("categoria", "topBase")
    policy = _outfit_scan_crop_policy(categoria)
    item["cropConfidence"] = crop_confidence
    item["imageUrl"] = ""
    item["needsPhoto"] = True
    if not policy.get("enabled", False):
        item["cropBox"] = None
        item["cropStatus"] = "skipped"
        _outfit_scan_log_crop_skip(categoria, "disabled", item.get("tempId"))
        return
    if crop_box:
        item["cropBox"] = crop_box
        item["cropStatus"] = "ready_candidate"
    else:
        item["cropBox"] = None
        item["cropStatus"] = "skipped"


def _outfit_scan_process_crops(
    items: list[dict],
    img: Image.Image,
    uid: str,
    scan_session_id: str,
    deadline: float,
) -> None:
    crop_count = 0
    time_exhausted = False
    for item in items:
        if item.get("cropStatus") != "ready_candidate":
            continue
        if time_exhausted or crop_count >= _OUTFIT_SCAN_MAX_CROP_ITEMS:
            item["cropStatus"] = "skipped"
            item["imageUrl"] = ""
            item["needsPhoto"] = True
            _outfit_scan_log_crop_skip(
                item.get("categoria", "topBase"),
                "time_or_count_limit",
                item.get("tempId"),
            )
            continue
        remaining = deadline - time.monotonic()
        if remaining <= _OUTFIT_SCAN_CROP_TIME_RESERVE_SEC:
            time_exhausted = True
            item["cropStatus"] = "skipped"
            item["imageUrl"] = ""
            item["needsPhoto"] = True
            _outfit_scan_log_crop_skip(
                item.get("categoria", "topBase"),
                "time_budget",
                item.get("tempId"),
            )
            continue
        crop_box = item.get("cropBox")
        if not crop_box:
            item["cropStatus"] = "skipped"
            item["imageUrl"] = ""
            item["needsPhoto"] = True
            continue
        categoria = item.get("categoria", "topBase")
        try:
            cropped, skip_reason = _crop_outfit_scan_image(img, crop_box, categoria)
            if cropped is None:
                item["cropStatus"] = "skipped"
                item["imageUrl"] = ""
                item["needsPhoto"] = True
                _outfit_scan_log_crop_skip(
                    categoria,
                    skip_reason or "crop_failed",
                    item.get("tempId"),
                )
                continue
            jpeg_bytes = _jpeg_bytes_from_outfit_scan_crop(cropped)
            image_url = _upload_outfit_scan_crop(uid, scan_session_id, item["tempId"], jpeg_bytes)
            item["imageUrl"] = image_url
            item["needsPhoto"] = False
            item["cropStatus"] = "ready"
            crop_count += 1
        except Exception as e:
            print(
                f"[outfit-scan] crop/upload failed tempId={item.get('tempId')}: "
                f"{type(e).__name__}: {e}"
            )
            item["cropStatus"] = "failed"
            item["imageUrl"] = ""
            item["needsPhoto"] = True


def _outfit_scan_response_status(items: list[dict], vision_ok: bool) -> str:
    if not vision_ok or not items:
        return "vision_error"
    ready = sum(1 for i in items if i.get("cropStatus") == "ready")
    not_ready = sum(1 for i in items if i.get("cropStatus") in ("skipped", "failed"))
    if ready > 0 and not_ready > 0:
        return "partial"
    return "ok"


def _normalize_outfit_scan_item(raw: dict, idx: int) -> dict | None:
    nome = str(raw.get("nome") or "").strip()
    if not nome:
        return None
    scan_raw = str(raw.get("scanRawLabel") or nome).strip()
    categoria = _normalize_outfit_scan_categoria(
        str(raw.get("categoria") or "").strip(),
        nome,
        scan_raw,
    )
    colore = normalize_color(raw.get("colore"))
    if colore == "sconosciuto":
        colore = str(raw.get("colore") or "").strip().lower() or "sconosciuto"
    stile = normalize_stile(raw.get("stile"))
    if stile not in _OUTFIT_SCAN_VALID_STILI:
        stile = "casual"
    stagione = normalize_stagione(raw.get("stagione"))
    if stagione not in _OUTFIT_SCAN_VALID_STAGIONI:
        stagione = "primavera"
    try:
        conf = float(raw.get("scanConfidence", 0.5))
    except (TypeError, ValueError):
        conf = 0.5
    conf = max(0.0, min(1.0, conf))
    if conf < _OUTFIT_SCAN_VISION_MIN_CONFIDENCE:
        return None
    temp_id = str(raw.get("tempId") or f"scan-{uuid.uuid4().hex[:8]}-{idx}")
    crop_confidence = _parse_outfit_scan_crop_confidence(raw)
    policy = _outfit_scan_crop_policy(categoria)
    if policy.get("enabled"):
        crop_box = _parse_outfit_scan_crop_box(raw, crop_confidence, categoria, temp_id)
    else:
        crop_box = None
    item = {
        "tempId": temp_id,
        "nome": nome,
        "categoria": categoria,
        "colore": colore,
        "stile": stile,
        "stagione": stagione,
        "reviewStatus": "pending",
        "scanConfidence": conf,
        "scanRawLabel": scan_raw,
    }
    _outfit_scan_attach_crop_fields(item, crop_box, crop_confidence)
    return item


def _parse_vision_items_response(raw_text: str) -> list[dict]:
    data = _extract_json_from_vision_text(raw_text)
    if isinstance(data, list):
        raw_items = data
    elif isinstance(data, dict):
        raw_items = data.get("items") or data.get("capi") or []
        if not isinstance(raw_items, list):
            raw_items = [data] if data.get("nome") else []
    else:
        return []
    items = []
    for i, raw in enumerate(raw_items[:6]):
        if not isinstance(raw, dict):
            continue
        item = _normalize_outfit_scan_item(raw, i)
        if item:
            items.append(item)
    return items


def _call_openai_outfit_vision(b64: str, mime: str, lang: str, timeout_sec: float | None = None) -> str:
    if not openai:
        raise RuntimeError("openai package not available")
    if not VISION_API_KEY:
        raise RuntimeError("VISION_API_KEY missing")
    req_timeout = timeout_sec if timeout_sec is not None else OUTFIT_SCAN_VISION_TIMEOUT_SEC
    resp = openai.ChatCompletion.create(
        model="gpt-4o-mini",
        api_key=VISION_API_KEY,
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": _outfit_scan_vision_prompt(lang)},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }],
        max_tokens=1800,
        temperature=0.2,
        request_timeout=req_timeout,
    )
    return (resp.choices[0].message.content or "").strip()


def _call_gemini_outfit_vision(b64: str, mime: str, lang: str, timeout_sec: float | None = None) -> str:
    if not VISION_API_KEY:
        raise RuntimeError("VISION_API_KEY missing")
    req_timeout = timeout_sec if timeout_sec is not None else OUTFIT_SCAN_VISION_TIMEOUT_SEC
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-1.5-flash:generateContent?key={VISION_API_KEY}"
    )
    payload = {
        "contents": [{
            "parts": [
                {"text": _outfit_scan_vision_prompt(lang)},
                {"inline_data": {"mime_type": mime, "data": b64}},
            ],
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=req_timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"gemini HTTP {e.code}: {err_body[:300]}") from e
    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError("gemini empty candidates")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    if not parts:
        raise RuntimeError("gemini empty parts")
    return str(parts[0].get("text") or "").strip()


def _run_outfit_scan_vision(
    image_path: str,
    lang: str,
    uid: str,
    scan_session_id: str,
) -> tuple[list[dict], str, str]:
    """
    Scarica l'immagine da Storage, chiama il provider Vision, normalizza items, auto-crop.
    Ritorna (items, status, provider) con status 'ok', 'partial' o 'vision_error'.
    """
    provider = VISION_PROVIDER
    if provider not in ("openai", "gemini"):
        print(f"[outfit-scan] unsupported VISION_PROVIDER={provider}")
        return [], "vision_error", provider
    if not VISION_API_KEY:
        print("[outfit-scan] VISION_API_KEY missing")
        return [], "vision_error", provider

    try:
        img, b64, mime = _load_outfit_scan_image(image_path)
    except Exception as e:
        print(f"[outfit-scan] storage download failed: {type(e).__name__}: {e}")
        return [], "vision_error", provider

    caller = _call_openai_outfit_vision if provider == "openai" else _call_gemini_outfit_vision
    deadline = time.monotonic() + OUTFIT_SCAN_VISION_TIMEOUT_SEC
    for attempt in range(2):
        remaining = deadline - time.monotonic()
        if remaining <= 0.5:
            print(f"[outfit-scan] vision budget exhausted before attempt {attempt + 1}")
            break
        per_attempt_timeout = max(1.0, remaining - 0.2)
        try:
            raw_text = _vision_call_with_timeout(
                caller, b64, mime, lang, timeout_sec=per_attempt_timeout
            )
            items = _parse_vision_items_response(raw_text)
            if items:
                _outfit_scan_process_crops(items, img, uid, scan_session_id, deadline)
                status = _outfit_scan_response_status(items, True)
                return items, status, provider
            print(f"[outfit-scan] vision attempt {attempt + 1}: parsed 0 items")
        except TimeoutError as e:
            print(f"[outfit-scan] vision attempt {attempt + 1} timeout: {e}")
            break
        except Exception as e:
            print(f"[outfit-scan] vision attempt {attempt + 1} failed: {type(e).__name__}: {e}")
        if attempt == 0:
            sleep_budget = deadline - time.monotonic() - 0.5
            if sleep_budget > 0:
                time.sleep(min(0.4, sleep_budget))

    return [], "vision_error", provider


class OutfitScanBody(BaseModel):
    userId: str = Field(..., min_length=1)
    imagePath: str = Field(..., min_length=1)
    scanSessionId: str | None = None
    lang: str | None = None
    provider: str | None = None


@app.post("/outfit-scan")
async def outfit_scan(request: Request, body: OutfitScanBody):
    uid = require_uid_match(request, body.userId)

    if not get_effective_premium_from_firestore(uid):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "PREMIUM_REQUIRED",
                "message": "Outfit Scan è disponibile solo per utenti Premium.",
            },
        )

    image_path = (body.imagePath or "").strip().lstrip("/")
    expected_prefix = f"scans/{uid}/"
    if not image_path.startswith(expected_prefix):
        raise HTTPException(
            status_code=400,
            detail={
                "code": "INVALID_IMAGE_PATH",
                "message": f"imagePath deve iniziare con {expected_prefix}",
            },
        )

    quota = reserve_outfit_scan_daily_or_raise(uid)

    scan_session_id = (body.scanSessionId or "").strip() or str(uuid.uuid4())
    lang = (body.lang or get_user_lang(uid) or "it").lower()

    if OUTFIT_SCAN_USE_VISION:
        items, status, provider = await asyncio.to_thread(
            _run_outfit_scan_vision, image_path, lang, uid, scan_session_id
        )
    else:
        items = _outfit_scan_mock_items()
        status = "mock"
        provider = "mock"

    return JSONResponse(
        content={
            "success": True,
            "scanSessionId": scan_session_id,
            "status": status,
            "provider": provider,
            "quota": quota,
            "outfitImagePath": image_path,
            "items": items,
        }
    )

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
   
