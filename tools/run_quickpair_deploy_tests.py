"""One-off post-deploy tests for QuickPair variants (Render)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import firebase_admin
from firebase_admin import auth, credentials, firestore

API = "https://myvogue-backend.onrender.com"
API_KEY = "AIzaSyCnvuqnm1Bc45dBEU-JefZ9C8XN0DO10kM"
PREMIUM_UID = "LYVA0KexDIRnIwROWDsuDdTAdEl2"
FREE_UID = "1p6sGINrafKDmuE5RxVH"
BASE_ID = "1FFvlJSZgKkYLufcfnGr"
STAGIONE = "primavera"


def init_firebase() -> firestore.Client:
    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not cred_path or not os.path.isfile(cred_path):
        print("Set GOOGLE_APPLICATION_CREDENTIALS to service account JSON.", file=sys.stderr)
        sys.exit(1)
    if not firebase_admin._apps:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(
            cred,
            {"storageBucket": os.environ.get("FIREBASE_BUCKET", "myvogueai.firebasestorage.app")},
        )
    return firestore.client()


def id_token_for(uid: str) -> str:
    custom = auth.create_custom_token(uid)
    if isinstance(custom, bytes):
        custom = custom.decode()
    req = urllib.request.Request(
        f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={API_KEY}",
        data=json.dumps({"token": custom, "returnSecureToken": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    return data["idToken"]


def quickpair(
    *,
    uid: str,
    base_id: str,
    stagione: str,
    token: str,
    variants: int | None = None,
) -> tuple[int, dict]:
    params = {
        "userId": uid,
        "baseId": base_id,
        "stagione": stagione,
        "lang": "it",
    }
    if variants is not None:
        params["variants"] = str(variants)
    url = f"{API}/quickpair?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {"raw": body}
        return e.code, payload


def quota_used(uid: str, db: firestore.Client) -> int | None:
    doc = db.collection("quota").document(uid).get()
    if not doc.exists:
        return 0
    from datetime import datetime

    today = datetime.utcnow().date().isoformat()
    data = doc.to_dict() or {}
    return (data.get(today) or {}).get("quickpair", 0)


def distinct_slot_combos(variants: list[dict]) -> int:
    keys = []
    for v in variants:
        sug = v.get("suggestion") or {}
        keys.append(
            "|".join(
                [
                    str((sug.get("bottom") or {}).get("id") or "-"),
                    str((sug.get("shoes") or {}).get("id") or "-"),
                    str((sug.get("layer") or {}).get("id") or "-"),
                ]
            )
        )
    return len(set(keys))


def main() -> None:
    db = init_firebase()
    premium_token = id_token_for(PREMIUM_UID)
    free_token = id_token_for(FREE_UID)

    # OpenAPI deploy probe
    with urllib.request.urlopen(f"{API}/openapi.json", timeout=30) as resp:
        spec = json.loads(resp.read().decode())
    qp_params = spec["paths"]["/quickpair"]["get"]["parameters"]
    has_variants = any(p.get("name") == "variants" for p in qp_params)
    print("=== DEPLOY PROBE ===")
    print(f"GitHub main expected: f973fc8")
    print(f"OpenAPI has variants param: {has_variants}")

    print("\n=== TEST 1 — Backward compatibility (no variants) ===")
    sc1, body1 = quickpair(
        uid=PREMIUM_UID,
        base_id=BASE_ID,
        stagione=STAGIONE,
        token=premium_token,
    )
    has_variants_field = "variants" in body1
    suggestion_ok = isinstance(body1.get("suggestion"), dict) and any(
        body1["suggestion"].get(k) for k in ("top", "bottom", "piece", "shoes", "layer")
    )
    print(f"HTTP {sc1}")
    print(json.dumps({
        "hasVariants": has_variants_field,
        "suggestionKeys": list((body1.get("suggestion") or {}).keys()),
        "score": body1.get("score"),
        "stylingReasonPrefix": (body1.get("stylingReason") or "")[:80],
        "pass": sc1 == 200 and not has_variants_field and suggestion_ok,
    }, ensure_ascii=False, indent=2))

    print("\n=== TEST 2 — Premium variants=3 ===")
    sc2, body2 = quickpair(
        uid=PREMIUM_UID,
        base_id=BASE_ID,
        stagione=STAGIONE,
        token=premium_token,
        variants=3,
    )
    variants = body2.get("variants") or []
    report2 = {
        "HTTP": sc2,
        "variantsRequested": body2.get("variantsRequested"),
        "variantsAvailable": body2.get("variantsAvailable"),
        "distinctSlotCombos": distinct_slot_combos(variants) if variants else 0,
        "variants": [],
        "pass": sc2 == 200 and isinstance(variants, list) and len(variants) >= 1,
    }
    for v in variants:
        sug = v.get("suggestion") or {}
        report2["variants"].append({
            "variant": v.get("variant"),
            "score": v.get("score"),
            "capi": {
                "top": (sug.get("top") or {}).get("nome"),
                "bottom": (sug.get("bottom") or {}).get("nome"),
                "layer": (sug.get("layer") or {}).get("nome"),
                "shoes": (sug.get("shoes") or {}).get("nome"),
            },
            "slotIds": {
                "bottom": (sug.get("bottom") or {}).get("id"),
                "shoes": (sug.get("shoes") or {}).get("id"),
                "layer": (sug.get("layer") or {}).get("id"),
            },
            "stylingReason": (v.get("stylingReason") or "")[:120],
        })
    print(json.dumps(report2, ensure_ascii=False, indent=2))

    print("\n=== TEST 3 — Free gate variants=3 ===")
    q_before = quota_used(FREE_UID, db)
    sc3, body3 = quickpair(
        uid=FREE_UID,
        base_id=BASE_ID,
        stagione=STAGIONE,
        token=free_token,
        variants=3,
    )
    q_after = quota_used(FREE_UID, db)
    code = (body3.get("detail") or {}).get("code") if isinstance(body3.get("detail"), dict) else None
    print(json.dumps({
        "HTTP": sc3,
        "detailCode": code,
        "detailMessage": (body3.get("detail") or {}).get("message") if isinstance(body3.get("detail"), dict) else body3,
        "quotaBefore": q_before,
        "quotaAfter": q_after,
        "quotaUnchanged": q_before == q_after,
        "pass": sc3 == 403 and code == "PREMIUM_REQUIRED" and q_before == q_after,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
