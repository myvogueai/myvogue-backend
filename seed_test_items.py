"""
Seed di capi di test su Firestore (collection clothingItems) per MyVogue AI.

Uso:
  - Imposta le variabili d'ambiente come per main.py:
      GOOGLE_APPLICATION_CREDENTIALS  (path al JSON service account)
      FIREBASE_BUCKET                 (bucket Firebase Storage)
  - DRY_RUN=True  → stampa solo i documenti che verrebbero creati (nessun accesso a Firebase).
  - DRY_RUN=False → scrive davvero su Firestore.

Non modifica main.py né le API.
"""

from __future__ import annotations

import os
import sys

# ---------------------------------------------------------------------------
# Imposta a False solo quando vuoi scrivere su Firestore.
# ---------------------------------------------------------------------------
DRY_RUN = True

TEST_USER_ID = "LYVA0KexDIRnIwROWDsuDdTAdEl2"

IMAGE_URL_PLACEHOLDER = "https://placehold.co/600x800/png?text=MyVogue+Test"

_STAGIONE = "estate"

# (nome, categoria, colore, stile)
_TEST_SPECS: list[tuple[str, str, str, str]] = [
    # topBase
    ("T-shirt bianca", "topBase", "bianco", "casual"),
    ("Maglietta verde", "topBase", "verde", "streetwear"),
    ("Camicia azzurra", "topBase", "azzurro", "elegante"),
    ("Polo beige", "topBase", "beige", "casual"),
    # topLayer
    ("Blazer navy", "topLayer", "navy", "elegante"),
    ("Giacca beige", "topLayer", "beige", "casual"),
    ("Bomber verde oliva", "topLayer", "verde oliva", "streetwear"),
    ("Cardigan nero", "topLayer", "nero", "casual"),
    # bottom
    ("Pantalone beige", "bottom", "beige", "casual"),
    ("Jeans denim", "bottom", "blu", "streetwear"),
    ("Pantalone grigio", "bottom", "grigio", "elegante"),
    ("Pantalone nero", "bottom", "nero", "elegante"),
    # scarpe
    ("Sneaker bianche", "scarpe", "bianco", "casual"),
    ("Scarpe cognac", "scarpe", "cognac", "elegante"),
    ("Sneaker grigie", "scarpe", "grigio", "streetwear"),
    ("Scarpe nere", "scarpe", "nero", "elegante"),
    # pezzoUnico
    ("Vestito nero", "pezzoUnico", "nero", "elegante"),
    ("Tuta blu", "pezzoUnico", "blu", "casual"),
]


def _build_payloads() -> list[dict]:
    out: list[dict] = []
    for nome, categoria, colore, stile in _TEST_SPECS:
        out.append(
            {
                "nome": nome,
                "categoria": categoria,
                "colore": colore,
                "stile": stile,
                "stagione": _STAGIONE,
                "userId": TEST_USER_ID,
                "imageUrl": IMAGE_URL_PLACEHOLDER,
                "isDirty": False,
                "isPublic": False,
            }
        )
    return out


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
    payloads = _build_payloads()
    n = len(payloads)

    if DRY_RUN:
        print("DRY_RUN=True — nessuna scrittura su Firestore.\n")
        print(f"Utente: {TEST_USER_ID} | Stagione: {_STAGIONE} | documenti: {n}\n")
        for i, doc in enumerate(payloads, start=1):
            print(f"--- [{i}/{n}] ---")
            for k, v in doc.items():
                print(f"  {k}: {v!r}")
            print(f"  createdAt: <SERVER_TIMESTAMP al write>")
        print(f"\nTotale che verrebbe creato: {n} articoli.")
        return

    from google.cloud.firestore import SERVER_TIMESTAMP

    db = _init_firestore()
    col = db.collection("clothingItems")
    created = 0
    for doc in payloads:
        data = {**doc, "createdAt": SERVER_TIMESTAMP}
        col.add(data)
        created += 1
    print(f"Creati {created} articoli in clothingItems per userId={TEST_USER_ID!r}.")


if __name__ == "__main__":
    main()
