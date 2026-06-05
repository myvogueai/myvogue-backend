"""Local tests for QuickPair anti-duplicate variant patch."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

import firebase_admin
from firebase_admin import auth, credentials, firestore

API = os.environ.get("QP_TEST_API", "http://127.0.0.1:8765")
API_KEY = "AIzaSyCnvuqnm1Bc45dBEU-JefZ9C8XN0DO10kM"
PREMIUM = "LYVA0KexDIRnIwROWDsuDdTAdEl2"
FREE = "1p6sGINrafKDmuE5RxVH"
TOP_BASE = "pN7PCppzL9rOVd7CxDje"
BOTTOM_BASE = "1FFvlJSZgKkYLufcfnGr"
ST = "primavera"


def init() -> None:
    cred = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not cred:
        sys.exit("Set GOOGLE_APPLICATION_CREDENTIALS")
    if not firebase_admin._apps:
        firebase_admin.initialize_app(credentials.Certificate(cred))


def token(uid: str) -> str:
    custom = auth.create_custom_token(uid)
    if isinstance(custom, bytes):
        custom = custom.decode()
    req = urllib.request.Request(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}",
        data=json.dumps({"token": custom, "returnSecureToken": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["idToken"]


def qp(uid: str, base: str, id_token: str, variants: int | None = None) -> tuple[int, dict]:
    params = {"userId": uid, "baseId": base, "stagione": ST, "lang": "it"}
    if variants is not None:
        params["variants"] = str(variants)
    url = f"{API}/quickpair?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {id_token}"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def variant_rows(body: dict) -> list[dict]:
    rows = []
    for v in body.get("variants") or []:
        s = v.get("suggestion") or {}
        rows.append(
            {
                "variant": v.get("variant"),
                "sig": "|".join(
                    str((s.get(k) or {}).get("id") or "-")
                    for k in ("top", "bottom", "piece", "layer", "shoes")
                ),
                "slots": "|".join(
                    str((s.get(k) or {}).get("id") or "-")
                    for k in ("bottom", "shoes", "layer")
                ),
                "capi": {
                    "top": (s.get("top") or {}).get("nome"),
                    "bottom": (s.get("bottom") or {}).get("nome"),
                    "layer": (s.get("layer") or {}).get("nome"),
                    "shoes": (s.get("shoes") or {}).get("nome"),
                },
                "score": v.get("score"),
                "reason": (v.get("stylingReason") or "")[:100],
            }
        )
    return rows


def main() -> None:
    init()
    pt = token(PREMIUM)
    ft = token(FREE)

    print("=== TEST 1 backward ===")
    sc, b = qp(PREMIUM, TOP_BASE, pt)
    print(json.dumps({"http": sc, "hasVariants": "variants" in b, "pass": sc == 200 and "variants" not in b}, indent=2))

    print("\n=== TEST 2 premium topBase variants=3 ===")
    sc, b = qp(PREMIUM, TOP_BASE, pt, 3)
    vars_ = variant_rows(b)
    sigs = [x["sig"] for x in vars_]
    slots = [x["slots"] for x in vars_]
    safe_creative_dup = False
    if len(vars_) >= 2 and vars_[0]["variant"] == "safe" and vars_[1]["variant"] == "creative":
        safe_creative_dup = vars_[0]["sig"] == vars_[1]["sig"] or vars_[0]["slots"] == vars_[1]["slots"]
    print(
        json.dumps(
            {
                "http": sc,
                "variantsAvailable": b.get("variantsAvailable"),
                "variantsRequested": b.get("variantsRequested"),
                "distinctSigs": len(set(sigs)),
                "distinctSlotCombos": len(set(slots)),
                "safeCreativeDuplicate": safe_creative_dup,
                "debugReason": b.get("debugReason"),
                "variants": vars_,
                "pass": sc == 200 and not safe_creative_dup,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    print("\n=== TEST 3 free gate ===")
    db = firestore.client()
    today = datetime.utcnow().date().isoformat()
    qdoc = db.collection("quota").document(FREE).get()
    qbefore = ((qdoc.to_dict() or {}).get(today) or {}).get("quickpair", 0) if qdoc.exists else 0
    sc, b = qp(FREE, TOP_BASE, ft, 3)
    qafter = ((db.collection("quota").document(FREE).get().to_dict() or {}).get(today) or {}).get("quickpair", 0)
    print(
        json.dumps(
            {
                "http": sc,
                "code": (b.get("detail") or {}).get("code"),
                "quotaBefore": qbefore,
                "quotaAfter": qafter,
                "pass": sc == 403 and (b.get("detail") or {}).get("code") == "PREMIUM_REQUIRED" and qbefore == qafter,
            },
            indent=2,
        )
    )

    print("\n=== TEST 4 bottom base variants=3 ===")
    sc, b = qp(PREMIUM, BOTTOM_BASE, pt, 3)
    print(
        json.dumps(
            {
                "http": sc,
                "variantsAvailable": b.get("variantsAvailable"),
                "debugReason": b.get("debugReason"),
                "variants": variant_rows(b),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
