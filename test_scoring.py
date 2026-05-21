"""
Scaffold di test per il motore di scoring MyVogue (main.py).

TODO (stabilità CI/local):
  Per rendere questi test eseguibili stabilmente, in futuro estrarre le funzioni pure
  di scoring in `scoring.py` (senza init Firebase / FastAPI) e importarle da `main.py`.
  Oggi `import main` può fallire per credenziali Firebase mancanti o dipendenze non installate.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_MAIN_IMPORT_ERROR: str | None = None
try:
    # Evita path credenziali invalido se la variabile è vuota ma presente.
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip():
        os.environ.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    import main as _main  # noqa: F401
except Exception as exc:  # pragma: no cover - ambiente senza deps/Firebase
    _main = None  # type: ignore[assignment]
    _MAIN_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

pytestmark = pytest.mark.skipif(
    _main is None,
    reason=(
        "import main fallito — "
        + (_MAIN_IMPORT_ERROR or "sconosciuto")
        + ". Vedi docstring: estrarre scoring.py."
    ),
)


def _item(
    item_id: str,
    nome: str,
    categoria: str,
    colore: str,
    stile: str,
    *,
    image_url: str = "https://example.test/img.jpg",
    **extra,
) -> dict:
    doc = {
        "docId": item_id,
        "id": item_id,
        "nome": nome,
        "categoria": categoria,
        "colore": colore,
        "stile": stile,
        "imageUrl": image_url,
    }
    doc.update(extra)
    return doc


# --- fixture capi riutilizzabili ---

@pytest.fixture
def sneaker_street():
    return _item(
        "sh1",
        "Sneaker bianche Air Force",
        "scarpe",
        "bianco",
        "streetwear",
    )


@pytest.fixture
def mocassino_formal():
    return _item(
        "sh2",
        "Mocassino pelle nera",
        "scarpe",
        "nero",
        "elegante",
    )


@pytest.fixture
def running_sport():
    return _item(
        "sh3",
        "Scarpe da running marathon",
        "scarpe",
        "nero",
        "sportivo",
    )


@pytest.fixture
def oxford_elegant():
    return _item(
        "sh4",
        "Oxford stringate pelle",
        "scarpe",
        "nero",
        "elegante",
    )


@pytest.fixture
def top_bianco():
    return _item("t1", "Maglietta bianca", "topBase", "bianco", "casual")


@pytest.fixture
def bottom_jeans():
    return _item("b1", "Jeans denim blu", "bottom", "blu", "casual")


# --- test funzioni pure / scoring relativo ---


def test_normalize_color_synonyms():
    assert _main.normalize_color("navy") == "navy"
    assert _main.normalize_color("cognac") == "marrone"
    assert _main.normalize_color("verde oliva") == "oliva"


def test_effective_color_nome_vince_su_colore_db():
    it = _item("x", "Blazer navy slim", "topLayer", "grigio", "elegante")
    assert _main.effective_color(it) == "navy"


def test_streetwear_sneaker_batte_mocassino(sneaker_street, mocassino_formal):
    s_sneaker = _main.outfit_score(
        shoes=sneaker_street,
        target_style="streetwear",
        apply_archetype=False,
    )
    s_moc = _main.outfit_score(
        shoes=mocassino_formal,
        target_style="streetwear",
        apply_archetype=False,
    )
    assert s_sneaker > s_moc


def test_elegante_formal_batte_running(oxford_elegant, running_sport):
    s_oxford = _main.outfit_score(
        shoes=oxford_elegant,
        target_style="elegante",
        apply_archetype=False,
    )
    s_run = _main.outfit_score(
        shoes=running_sport,
        target_style="elegante",
        apply_archetype=False,
    )
    assert s_oxford > s_run


def test_outfit_coerente_batte_incoerente(top_bianco, bottom_jeans, sneaker_street, oxford_elegant):
    coherent = _main.outfit_score(
        top=top_bianco,
        bottom=bottom_jeans,
        shoes=sneaker_street,
        target_style="casual",
        apply_archetype=False,
    )
    clash_top = _item("t2", "Felpa rossa", "topBase", "rosso", "casual")
    clash_bottom = _item("b2", "Pantaloni verde acceso fluo", "bottom", "verde acceso", "casual")
    incoherent = _main.outfit_score(
        top=clash_top,
        bottom=clash_bottom,
        shoes=oxford_elegant,
        target_style="sportivo",
        apply_archetype=False,
    )
    assert coherent > incoherent


def test_rosso_verde_acceso_peggio_di_bianco_blu():
    clash = _main.outfit_score(
        top=_item("t3", "Top rosso", "topBase", "rosso", "casual"),
        bottom=_item("b3", "Pantaloni verde acceso", "bottom", "verde acceso", "casual"),
        apply_archetype=False,
    )
    calm = _main.outfit_score(
        top=_item("t4", "Top bianco", "topBase", "bianco", "casual"),
        bottom=_item("b4", "Pantaloni blu", "bottom", "blu", "casual"),
        apply_archetype=False,
    )
    assert calm > clash


def test_base_item_fit_aumenta_score_quickpair_like(top_bianco, bottom_jeans, sneaker_street):
    base = bottom_jeans
    without = _main.outfit_score(
        top=top_bianco,
        bottom=bottom_jeans,
        shoes=sneaker_street,
        target_style="casual",
        apply_archetype=False,
    )
    with_base = _main.outfit_score(
        top=top_bianco,
        bottom=bottom_jeans,
        shoes=sneaker_street,
        target_style="casual",
        base_item=base,
        apply_archetype=False,
    )
    assert with_base >= without


def test_due_pattern_non_solidi_penalizzati(top_bianco, bottom_jeans):
    plain = _main.outfit_score(
        top=top_bianco,
        bottom=bottom_jeans,
        apply_archetype=False,
    )
    striped_top = {**top_bianco, "pattern": "righe"}
    dotted_bottom = {**bottom_jeans, "pattern": "pois"}
    busy = _main.outfit_score(
        top=striped_top,
        bottom=dotted_bottom,
        apply_archetype=False,
    )
    assert plain > busy


def test_prefer_palette_puo_aumentare_score(top_bianco, bottom_jeans):
    base = _main.outfit_score(
        top=top_bianco,
        bottom=bottom_jeans,
        apply_archetype=False,
    )
    boosted = _main.outfit_score(
        top=top_bianco,
        bottom=bottom_jeans,
        prefer_palette=["bianco", "blu"],
        apply_archetype=False,
    )
    assert boosted >= base


def test_layer_parametro_accettato(top_bianco, bottom_jeans, sneaker_street):
    layer = _item("l1", "Bomber street", "topLayer", "nero", "streetwear")
    score = _main.outfit_score(
        top=top_bianco,
        bottom=bottom_jeans,
        shoes=sneaker_street,
        layer=layer,
        target_style="casual",
        apply_archetype=False,
    )
    assert isinstance(score, (int, float))


# --- outfit_score_v2 (deprecata): smoke relativo opzionale ---


def test_outfit_score_v2_bianco_blu_migliore_di_rosso_verde():
    calm = _main.outfit_score_v2(
        top=_item("v1", "Top bianco", "topBase", "bianco", "casual"),
        bottom=_item("v2", "Pantaloni blu", "bottom", "blu", "casual"),
    )
    clash = _main.outfit_score_v2(
        top=_item("v3", "Top rosso", "topBase", "rosso", "casual"),
        bottom=_item("v4", "Pantaloni verde acceso", "bottom", "verde acceso", "casual"),
    )
    assert calm > clash


# =============================================================================
# TODO — test di sensibilità (non implementati: costanti locali / import main)
# =============================================================================
#
# - _LAYER_MIN_GAIN (globale 0.08): layer entra solo se gain >= soglia in _best_layer_for_outfit.
# - _SCORE_MARGIN (locale in /quickpair, 1.5): ampiezza filtro candidati topK.
# - _QUICKPAIR_DOMINANCE_GAP (locale in /quickpair, 0.38): scelta automatica vs diversità.
# - Pesi _score_real_shoes (+4 streetwear sneaker, -5.5 mocassino): piccolo delta → cambio vincitore.
# - _score_base_item_fit: moltiplicatori 2.0–2.4 su coppie colore QuickPair.
#
# Per eseguirli serve golden set + seed QuickPair controllabile; vedi docs/SCORING_ARCHITECTURE.md.
