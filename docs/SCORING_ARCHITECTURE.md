# Architettura scoring MyVogue AI

Documentazione del motore di scoring outfit in `main.py`. Descrive il comportamento **attuale** in produzione; non sostituisce test automatici né golden set.

---

## Motore produttivo: `outfit_score`

`outfit_score` è il motore usato da **`/outfit`**, **`/quickpair`** e da `_best_layer_for_outfit`.

- **Scala**: punteggio **cumulativo**, **non** clampato in `[-1, +1]` (a differenza di `outfit_score_v2`).
- **Parametri rilevanti**: `top`, `bottom`, `piece`, `shoes`, **`layer`** (non `top_layer`), `prefer_palette`, `target_style`, `base_item`, `apply_archetype`.
- **Componenti principali** (somma pesata implicita tramite contributi additivi):
  1. Coppie colore via `color_relation_score` × moltiplicatori (es. 1.4 top/bottom, 0.7 con layer).
  2. Bias `prefer_palette` (+0.25 per capo che matcha, accumulabile su più capi).
  3. Regole stilistiche “reali”: `_score_real_shoes`, `_score_real_toplayer`, `_score_shoes_layer_combo`, `_light_formality_tune`.
  4. `_score_visual_balance`, `_score_base_item_fit` (forte in QuickPair).
  5. Penalità mismatch stile base vs target: **`-1.5`** se `base_item.stile` ≠ `target_style`.
  6. `archetype_combo_bonus` (cap globale `_ARCH_MAX_TOTAL`).
  7. `_pattern_mix_penalty` (due pattern non solidi → **-0.45**).

Colore in scoring v1: `_color_raw_for_score_v1` → **`effective_color` prima del campo Firestore `colore`**.

---

## Motore deprecato: `outfit_score_v2`

Marcato **DEPRECATED / DO NOT USE IN PRODUCTION** sopra la definizione.

- Scala **normalizzata** `[-1, +1]` con media pesata (colore 35%, palette 25%, scarpe 20%, stile 20%).
- Usa `shoes_score`, `palette_score`, `style_score` (contributi morbidi in `[-1, 1]`).
- **`prefer_palette`**: bonus fino a +0.05 (accumulabile per match multipli).
- **Non** sostituisce `outfit_score` negli endpoint senza migrazione completa e golden test.

---

## Scale non omogenee

| Componente | Scala tipica | Note |
|------------|--------------|------|
| `outfit_score` | cumulativo, aperto | Dominato da `_score_real_shoes` (+4 / -5.5 streetwear) |
| `outfit_score_v2` | `[-1, +1]` | Clamp finale |
| `color_relation_score` | circa `[-1, +1]` | Usato moltiplicato in v1 |
| `style_score` | `[-1, +1]` | Solo in v2 |
| `archetype_combo_bonus` | piccolo, cap 0.15 | v1 |
| Penalità pattern | -0.45 | v1 |
| Mismatch stile base | -1.5 | v1 QuickPair |

Confrontare numericamente score v1 e v2 **non ha senso** senza normalizzazione.

---

## `_score_real_shoes` (dominante rispetto agli archetipi)

Per `target_style` normalizzato, le keyword nel testo capo (`nome`, `categoria`, `stile`, `colore`) guidano bonus/penalità grandi:

- **streetwear**: sneaker/trainer ~ **+4.0**; mocassino/oxford ~ **-5.5**.
- **elegante**: formali ~ **+3.4**; sneaker ~ **-2.0**; running/sport ~ **-2.5**.
- **sportivo**: sport ~ **+3.0**; formali ~ **-4.0**.

`archetype_combo_bonus` resta deliberatamente piccolo (`_ARCH_MAX_TOTAL = 0.15`) rispetto a queste regole.

---

## `_score_base_item_fit` (QuickPair)

Quando `base_item` è il capo cliccato, `_score_base_item_fit` aggiunge contributi colore **moltiplicati** (×2.0, ×2.4, ecc.) in base a `base_item.categoria` (`bottom`, `topBase`, `scarpe`, `topLayer`, `pezzoUnico`).

In QuickPair questo termine può **spostare l’ordinamento** più degli archetipi o del bilanciamento visivo.

---

## `effective_color` vs Firestore `colore`

`effective_color` analizza **`nome`** con lessico fisso (navy, oliva, cognac, bordeaux, …). Se trova un match, **vince** sul campo `colore` del documento.

Esempio: blazer con `colore: "grigio"` ma `nome: "Blazer navy"` → scoring usa **navy**.

`_color_raw_for_score_v1` e `outfit_score_v2` (copia item con `colore` sovrascritto) dipendono da questa priorità.

---

## `style_score` (v2) — fragile con nomi generici

`style_score` concatena `nome`, `categoria`, `stile` normalizzato. Nomi generici (“maglietta”, “pantaloni”) **non** attivano bonus/penalità keyword → score vicino a 0.

Rischio: capi ben descritti in UI ma generici in DB sottopesano la coerenza stilistica in v2.

---

## Penalità mismatch stile `-1.5`

In `outfit_score`, se `base_item` e `target_style` sono entrambi valorizzati e `normalize_stile(base_item.stile) != normalize_stile(target_style)`:

```text
score -= 1.5
```

Rilevante per QuickPair quando l’utente forza uno stile diverso dal capo base.

---

## `prefer_palette` accumulabile (v1)

Per ogni capo tra `piece`, `top`, `bottom`, `shoes`, `layer` con colore in palette preferita: **+0.25** ciascuno (nessun cap globale nel loop v1).

In v2: bonus unico derivato da `match_n` (max +0.05).

---

## Randomizzazione QuickPair

- Endpoint outfit con `refreshSeed`: `random.Random(seed_material)` per campionamento più deterministico.
- **`/quickpair`**: usa `random.Random(time.time())` locale; `_SCORE_MARGIN = 1.5` e `_QUICKPAIR_DOMINANCE_GAP = 0.38` sono **costanti dentro la funzione endpoint**, non importabili globalmente.
- Filtraggio candidati: `s >= best_score - _SCORE_MARGIN`; tie-break con gap `_QUICKPAIR_DOMINANCE_GAP` tra primi due score distinti.

**TODO prodotto**: rendere QuickPair seedabile/controllabile come `/outfit` per test ripetibili e A/B.

---

## Scelta layer: `_LAYER_MIN_GAIN`

`_best_layer_for_outfit` promuove un layer solo se `best_sc >= base_sc + _LAYER_MIN_GAIN` con `_LAYER_MIN_GAIN = 0.08` (modulo globale).

---

## Golden test e test di sensibilità

### Golden test statici (consigliati)

30–50 outfit “noti buoni” con score atteso o ordinamento relativo (non valori assoluti rigidi). Rieseguire dopo ogni tweak a pesi/soglie.

### Test di sensibilità (TODO in `test_scoring.py`)

Parametri ad alto impatto — piccole variazioni possono cambiare l’outfit vincitore in QuickPair:

| Parametro | Dove | Effetto |
|-----------|------|---------|
| `_LAYER_MIN_GAIN` | globale (0.08) | Layer opzionale entra o resta fuori |
| `_SCORE_MARGIN` | locale `/quickpair` (1.5) | Ampiezza pool candidati |
| `_QUICKPAIR_DOMINANCE_GAP` | locale `/quickpair` (0.38) | Auto-pick vs diversità |
| Pesi `_score_real_shoes` | funzione | Dominanza scarpe vs resto |
| `_score_base_item_fit` | funzione | QuickPair centrato sul capo base |

---

## Invarianti da non rompere (senza migrazione esplicita)

1. **`/outfit` e `/quickpair` devono continuare a chiamare `outfit_score`**, non `outfit_score_v2`.
2. Firma layer: parametro **`layer`**, non `top_layer`.
3. **`effective_color` > campo `colore`** per scoring v1/v2.
4. **`_score_real_shoes` resta il driver principale** dello stile scarpe vs archetipi.
5. **Mismatch stile base −1.5** in QuickPair con `base_item` + `target_style`.
6. **`prefer_palette` v1**: bonus per capo, accumulabile.
7. **Pattern**: due non-solid → penalità -0.45; assenza campo `pattern` → neutro.
8. **Non clampare `outfit_score`** a `[-1, 1]` senza rifare tutti i confronti QuickPair.
9. Costanti QuickPair **locali** all’endpoint: non assumere che siano esportate da `main`.

---

## Roadmap testabilità

Importare `main.py` in pytest può fallire per:

- init **Firebase** (`GOOGLE_APPLICATION_CREDENTIALS`);
- dipendenze pesanti (FastAPI, Vision, …).

**Raccomandazione**: estrarre in futuro le funzioni pure (`outfit_score`, `normalize_color`, `effective_color`, …) in un modulo `scoring.py` importato da `main.py`, lasciando init Firebase solo nel bootstrap app.

Fino ad allora: scaffold `test_scoring.py` con skip documentato se l’import fallisce.
