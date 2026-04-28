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

IMAGE_URL_PLACEHOLDER = "https://placehold.co/600x800/png?text=MyVogue+Primavera"

_STAGIONE = "primavera"

# (nome, categoria, colore, stile)
_TEST_SPECS: list[tuple[str, str, str, str]] = [
    # topBase
    ("Camicia bianca Oxford", "topBase", "bianco", "elegante"),
    ("Camicia azzurra popeline", "topBase", "azzurro", "elegante"),
    ("T-shirt bianca premium", "topBase", "bianco", "casual"),
    ("Polo beige", "topBase", "beige", "casual"),
    ("T-shirt nera basic", "topBase", "nero", "streetwear"),
    ("Felpa grigio chiaro", "topBase", "grigio", "sportivo"),
    # topLayer
    ("Blazer navy leggero", "topLayer", "navy", "elegante"),
    ("Trench beige", "topLayer", "beige", "elegante"),
    ("Giacca denim chiara", "topLayer", "azzurro", "casual"),
    ("Bomber verde oliva", "topLayer", "verde oliva", "streetwear"),
    ("Cardigan grigio", "topLayer", "grigio", "casual"),
    # bottom
    ("Pantalone chino beige", "bottom", "beige", "casual"),
    ("Pantalone nero sartoriale", "bottom", "nero", "elegante"),
    ("Jeans blu dritto", "bottom", "blu", "casual"),
    ("Gonna midi nera", "bottom", "nero", "elegante"),
    ("Pantalone grigio elegante", "bottom", "grigio", "elegante"),
    ("Cargo verde oliva", "bottom", "verde oliva", "streetwear"),
    # scarpe
    ("Mocassini cognac", "scarpe", "cognac", "elegante"),
    ("Sneakers bianche minimal", "scarpe", "bianco", "casual"),
    ("Derby nere", "scarpe", "nero", "elegante"),
    ("Sneakers blu", "scarpe", "blu", "streetwear"),
    ("Running grigie", "scarpe", "grigio", "sportivo"),
    # pezzoUnico
    ("Vestito nero midi", "pezzoUnico", "nero", "elegante"),
    ("Abito beige leggero", "pezzoUnico", "beige", "casual"),
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
                "seedTag": "primavera_benchmark_v1",
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
