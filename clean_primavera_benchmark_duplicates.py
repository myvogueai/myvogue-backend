"""
Ripulisce duplicati benchmark primavera impostando isDirty=True sugli eccedenti,
lasciando un solo capo canonico per ogni nome atteso.

Non cancella documenti.

Uso: stesse env di seed_test_items.py (GOOGLE_APPLICATION_CREDENTIALS, FIREBASE_BUCKET).
"""

from __future__ import annotations

import os
import sys

DRY_RUN = True

USER_ID = "LYVA0KexDIRnIwROWDsuDdTAdEl2"
STAGIONE = "primavera"
BENCHMARK_TAG = "primavera_benchmark_v1"

EXPECTED: list[tuple[str, str, str, str]] = [
    ("Camicia bianca Oxford", "topBase", "bianco", "elegante"),
    ("Camicia azzurra popeline", "topBase", "azzurro", "elegante"),
    ("T-shirt bianca premium", "topBase", "bianco", "casual"),
    ("Polo beige", "topBase", "beige", "casual"),
    ("T-shirt nera basic", "topBase", "nero", "streetwear"),
    ("Felpa grigio chiaro", "topBase", "grigio", "sportivo"),
    ("Blazer navy leggero", "topLayer", "navy", "elegante"),
    ("Trench beige", "topLayer", "beige", "elegante"),
    ("Giacca denim chiara", "topLayer", "azzurro", "casual"),
    ("Bomber verde oliva", "topLayer", "verde oliva", "streetwear"),
    ("Cardigan grigio", "topLayer", "grigio", "casual"),
    ("Pantalone chino beige", "bottom", "beige", "casual"),
    ("Pantalone nero sartoriale", "bottom", "nero", "elegante"),
    ("Jeans blu dritto", "bottom", "blu", "casual"),
    ("Gonna midi nera", "bottom", "nero", "elegante"),
    ("Pantalone grigio elegante", "bottom", "grigio", "elegante"),
    ("Cargo verde oliva", "bottom", "verde oliva", "streetwear"),
    ("Mocassini cognac", "scarpe", "cognac", "elegante"),
    ("Sneakers bianche minimal", "scarpe", "bianco", "casual"),
    ("Derby nere", "scarpe", "nero", "elegante"),
    ("Sneakers blu", "scarpe", "blu", "streetwear"),
    ("Running grigie", "scarpe", "grigio", "sportivo"),
    ("Vestito nero midi", "pezzoUnico", "nero", "elegante"),
    ("Abito beige leggero", "pezzoUnico", "beige", "casual"),
]

_BENCHMARK_NAMES = {nome for nome, *_ in EXPECTED}
_SPECS_BY_NOME = {nome: (nome, cat, col, sty) for nome, cat, col, sty in EXPECTED}


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


def _created_ts(snapshot) -> float:
    data = snapshot.to_dict() or {}
    ca = data.get("createdAt")
    if ca is None:
        return -1.0
    if hasattr(ca, "timestamp"):
        try:
            t = ca.timestamp()
            return float(t)
        except Exception:
            return -1.0
    return -1.0


def _pick_keeper(snaps: list):
    """Preferisce seedTag=BENCHMARK_TAG; poi createdAt più recente; poi id lessicografico."""
    if not snaps:
        return None
    with_tag = [
        s
        for s in snaps
        if (s.to_dict() or {}).get("seedTag") == BENCHMARK_TAG
    ]
    pool = with_tag if with_tag else snaps
    rated = [(s, _created_ts(s)) for s in pool]
    mx = max((r for _, r in rated), default=-1.0)
    bests = [s for s, r in rated if r == mx]
    return sorted(bests, key=lambda x: x.id)[0]


def main() -> None:
    db = _init_firestore()
    items = db.collection("clothingItems")

    snaps = list(items.where("userId", "==", USER_ID).where("stagione", "==", STAGIONE).stream())
    total_primavera = len(snaps)

    from collections import defaultdict

    by_nome: dict[str, list] = defaultdict(list)
    for s in snaps:
        nome = ((s.to_dict() or {}).get("nome")) or ""
        by_nome[nome].append(s)

    keepers: dict[str, object] = {}

    for nome, *_rest in EXPECTED:
        grp = by_nome.get(nome, [])
        if not grp:
            continue
        k = _pick_keeper(grp)
        if k:
            keepers[nome] = k

    all_ids = {s.id for s in snaps}
    keeper_ids = {k.id for k in keepers.values()}
    # Ogni documento primavera che non è il keeper del proprio benchmark va nascosto.
    all_to_hide = all_ids - keeper_ids

    non_benchmark_to_hide = {
        s.id
        for s in snaps
        if (((s.to_dict() or {}).get("nome")) or "") not in _BENCHMARK_NAMES
    }

    if DRY_RUN:
        print("DRY_RUN=True — nessuna scrittura su Firestore.\n")
        print(f"Documenti primavera totali trovati: {total_primavera}\n")
        print("--- Per nome benchmark (documento mantenuto attivo) ---\n")

        for nome, *_spec in EXPECTED:
            grp = by_nome.get(nome, [])
            if not grp:
                print(f"  {nome!r}: NESSUN documento trovato (previsto).\n")
                continue
            k = keepers.get(nome)
            other_ids = sorted([x.id for x in grp if k and x.id != k.id])
            print(f"  {nome!r}:")
            print(f"    -> tenere attivo: {k.id if k else None}")
            if other_ids:
                print(f"    -> duplicati da nascondere: {other_ids}")

        extras = sorted(non_benchmark_to_hide - keeper_ids)
        if extras:
            print("\nAltri documenti primavera (nome non nella lista benchmark) da nascondere:")
            print(f"    {extras}")

        print(f"\nDocumenti benchmark che verrebbero tenuti attivi: {len(keepers)}")
        print(f"Documenti da nascondere (isDirty=True): {len(all_to_hide)}")
        return

    updated_keep = 0
    updated_hide = 0

    batch = db.batch()
    ops = 0

    def commit_if_needed(force: bool = False):
        nonlocal batch, ops
        if ops >= 450 or force:
            if ops:
                batch.commit()
                batch = db.batch()
                ops = 0

    for nome, keeper in keepers.items():
        spec = _SPECS_BY_NOME[nome]
        _, cat, color, sty = spec
        batch.update(
            items.document(keeper.id),
            {
                "nome": nome,
                "categoria": cat,
                "colore": color,
                "stile": sty,
                "stagione": STAGIONE,
                "userId": USER_ID,
                "isDirty": False,
                "isPublic": False,
                "seedTag": BENCHMARK_TAG,
            },
        )
        ops += 1
        updated_keep += 1
        commit_if_needed()

    for doc_id in all_to_hide:
        batch.update(items.document(doc_id), {"isDirty": True})
        ops += 1
        updated_hide += 1
        commit_if_needed()

    commit_if_needed(force=True)

    print(f"Aggiornati (canonici tenuti attivi): {updated_keep}")
    print(f"Aggiornati (nascosti, isDirty=True): {updated_hide}")


if __name__ == "__main__":
    main()
