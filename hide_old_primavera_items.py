"""
Nasconde (isDirty=True) i capi primavera vecchi di un utente, senza cancellarli.
Mantiene visibili i benchmark con seedTag = primavera_benchmark_v1.

Uso: stesse env di seed_test_items.py (GOOGLE_APPLICATION_CREDENTIALS, FIREBASE_BUCKET).
DRY_RUN=True → solo stampa cosa verrebbe nascosto.
DRY_RUN=False → aggiorna Firestore.
"""

from __future__ import annotations

import os
import sys

DRY_RUN = True

USER_ID = "LYVA0KexDIRnIwROWDsuDdTAdEl2"
STAGIONE = "primavera"
BENCHMARK_TAG = "primavera_benchmark_v1"


def _init_firestore():
    import firebase_admin
    from firebase_admin import credentials, firestore

    cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    bucket = os.getenv("FIREBASE_BUCKET", "").strip()
    if not cred_path:
        print("ERRORE: imposta GOOGLE_APPLICATION_CREDENTIALS (path al JSON).", file=sys.stderr)
        sys.exit(1)
    if not bucket:
        print("ERRORE: imposta FIREBASE_BUCKET.", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(cred_path):
        print(f"ERRORE: file credenziali non trovato: {cred_path}", file=sys.stderr)
        sys.exit(1)

    if not firebase_admin._apps:
        cred = credentials.Certificate(cred_path)
        firebase_admin.initialize_app(cred, {"storageBucket": bucket})
    return firestore.client()


def main() -> None:
    db = _init_firestore()

    col = db.collection("clothingItems")
    q = col.where("userId", "==", USER_ID).where("stagione", "==", STAGIONE)

    to_hide: list[tuple[str, dict]] = []
    for snap in q.stream():
        data = snap.to_dict() or {}
        if data.get("seedTag") == BENCHMARK_TAG:
            continue
        to_hide.append((snap.id, data))

    if DRY_RUN:
        print("DRY_RUN=True — nessuna scrittura su Firestore.\n")
        print(
            f"Utente: {USER_ID} | stagione: {STAGIONE} | "
            f"documenti da nascondere (isDirty=True): {len(to_hide)}\n"
        )
        for doc_id, data in to_hide:
            print(
                {
                    "id": doc_id,
                    "nome": data.get("nome"),
                    "categoria": data.get("categoria"),
                    "colore": data.get("colore"),
                    "stile": data.get("stile"),
                    "seedTag": data.get("seedTag"),
                }
            )
        return

    updated = 0
    for doc_id, _data in to_hide:
        col.document(doc_id).update({"isDirty": True})
        updated += 1
    print(f"Aggiornati {updated} documenti (isDirty=True) in clothingItems per userId={USER_ID!r}.")


if __name__ == "__main__":
    main()
