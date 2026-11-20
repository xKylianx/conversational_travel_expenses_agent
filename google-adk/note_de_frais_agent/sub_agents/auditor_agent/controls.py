"""Contrôles déterministes de détection d'anomalies sur les transactions ONEO / Airplus.

Les transactions sont attendues dans un format normalisé (voir ``normalize_transaction``) :
    id, source (ONEO|AIRPLUS), matricule, spender, date (YYYY-MM-DD), amount, category,
    merchant, country, trip_id, approval_date, nights
"""

from __future__ import annotations

import os
import re
import statistics
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

# ---------------------------------------------------------------------------
# Paramétrage (surchargeable via variables d'environnement)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# Seuils unitaires par famille de dépense (EUR). La famille est déduite par mots-clés
# présents dans ``category`` / ``merchant``. Pour l'hôtel le seuil est par nuitée.
THRESHOLDS: dict[str, float] = {
    "repas": _env_float("AUDIT_SEUIL_REPAS", 80.0),
    "hotel": _env_float("AUDIT_SEUIL_HOTEL_NUIT", 200.0),
    "taxi": _env_float("AUDIT_SEUIL_TAXI", 100.0),
    "train": _env_float("AUDIT_SEUIL_TRAIN", 300.0),
    "avion": _env_float("AUDIT_SEUIL_AVION", 1000.0),
    "location": _env_float("AUDIT_SEUIL_LOCATION", 400.0),
    "default": _env_float("AUDIT_SEUIL_DEFAUT", 500.0),
}

FAMILY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "repas": ("repas", "restaurant", "dejeuner", "diner", "meal", "food", "invitation"),
    "hotel": ("hotel", "hebergement", "nuit", "accommodation", "lodging"),
    "taxi": ("taxi", "vtc", "uber", "transfer"),
    "train": ("train", "sncf", "rail", "tgv", "eurostar"),
    "avion": ("avion", "vol", "air", "flight", "aerien"),
    "location": ("location", "car rental", "rental", "vehicule", "voiture"),
}

# Doublons / double remboursement
DUPLICATE_WINDOW_DAYS = int(_env_float("AUDIT_DOUBLON_FENETRE_JOURS", 1))
CROSS_SOURCE_TOLERANCE = _env_float("AUDIT_DOUBLON_TOLERANCE_PCT", 0.05)

# Fractionnement
SPLIT_MIN_COUNT = int(_env_float("AUDIT_FRACTIONNEMENT_MIN_TX", 2))

# Outliers statistiques
OUTLIER_MIN_SAMPLE = int(_env_float("AUDIT_OUTLIER_MIN_ECHANTILLON", 8))
OUTLIER_IQR_FACTOR = _env_float("AUDIT_OUTLIER_IQR_FACTEUR", 1.5)
OUTLIER_MEDIAN_FACTOR = _env_float("AUDIT_OUTLIER_MEDIANE_FACTEUR", 3.0)


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------

def _strip_accents(value: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", value) if not unicodedata.combining(c)
    )


def _norm_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", _strip_accents(str(value)).lower()).strip()


def _parse_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("€", "").replace("EUR", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def expense_family(category: Any, merchant: Any = None) -> str:
    haystack = f"{_norm_text(category)} {_norm_text(merchant)}"
    for family, keywords in FAMILY_KEYWORDS.items():
        if any(k in haystack for k in keywords):
            return family
    return "default"


def person_key(tx: dict[str, Any]) -> str:
    return _norm_text(tx.get("matricule")) or _norm_text(tx.get("spender")) or "inconnu"


def normalize_transaction(raw: dict[str, Any], index: int) -> dict[str, Any] | None:
    """Nettoie une transaction fournie par le LLM ; retourne None si inexploitable."""
    amount = _parse_amount(raw.get("amount"))
    tx_date = _parse_date(raw.get("date"))
    if amount is None or tx_date is None:
        return None
    source = _norm_text(raw.get("source")).upper() or "INCONNU"
    nights = _parse_amount(raw.get("nights"))
    return {
        "id": str(raw.get("id") or f"{source}-{index}"),
        "source": source,
        "matricule": raw.get("matricule"),
        "spender": raw.get("spender"),
        "date": tx_date,
        "amount": round(amount, 2),
        "category": raw.get("category"),
        "merchant": raw.get("merchant"),
        "country": raw.get("country"),
        "trip_id": raw.get("trip_id"),
        "approval_date": _parse_date(raw.get("approval_date")),
        "nights": nights if nights and nights > 0 else None,
        "family": expense_family(raw.get("category"), raw.get("merchant")),
    }


def _anomaly(control: str, severity: str, tx_ids: list[str], detail: str, **extra: Any) -> dict[str, Any]:
    return {"control": control, "severity": severity, "transaction_ids": tx_ids, "detail": detail, **extra}


# ---------------------------------------------------------------------------
# Contrôle 1 : dépassement de seuil
# ---------------------------------------------------------------------------

def check_thresholds(txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anomalies = []
    for tx in txs:
        family = tx["family"]
        seuil = THRESHOLDS[family]
        montant = tx["amount"]
        unit = ""
        if family == "hotel" and tx.get("nights"):
            montant = round(tx["amount"] / tx["nights"], 2)
            unit = " par nuitée"
        if montant > seuil:
            ratio = montant / seuil
            severity = "HAUTE" if ratio >= 2 else "MOYENNE"
            anomalies.append(_anomaly(
                "DEPASSEMENT_SEUIL", severity, [tx["id"]],
                f"{montant:.2f} EUR{unit} > seuil {seuil:.0f} EUR pour la famille '{family}' "
                f"(x{ratio:.1f}) — {tx.get('spender')} le {tx['date'].isoformat()}",
                amount=montant, threshold=seuil, family=family,
            ))
    return anomalies


# ---------------------------------------------------------------------------
# Contrôle 2 : transactions le week-end
# ---------------------------------------------------------------------------

def check_weekend(txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anomalies = []
    for tx in txs:
        if tx["date"].weekday() < 5:
            continue
        jour = "samedi" if tx["date"].weekday() == 5 else "dimanche"
        # Un hôtel ou un transport le week-end est courant en déplacement : sévérité moindre.
        severity = "BASSE" if tx["family"] in ("hotel", "train", "avion") else "MOYENNE"
        anomalies.append(_anomaly(
            "WEEKEND", severity, [tx["id"]],
            f"Dépense de {tx['amount']:.2f} EUR ({tx.get('category')}) effectuée un {jour} "
            f"{tx['date'].isoformat()} par {tx.get('spender')}",
            weekday=jour, family=tx["family"],
        ))
    return anomalies


# ---------------------------------------------------------------------------
# Contrôle 3 : doublons et double remboursement ONEO <-> Airplus
# ---------------------------------------------------------------------------

def check_duplicates(txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anomalies = []
    seen_pairs: set[tuple[str, str]] = set()
    by_person: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tx in txs:
        by_person[person_key(tx)].append(tx)

    window = timedelta(days=DUPLICATE_WINDOW_DAYS)
    for person, items in by_person.items():
        items.sort(key=lambda t: t["date"])
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if b["date"] - a["date"] > window:
                    break
                pair = tuple(sorted((a["id"], b["id"])))
                if pair in seen_pairs:
                    continue
                same_source = a["source"] == b["source"]
                if same_source:
                    # Doublon strict : même montant, même date, même famille
                    if a["amount"] == b["amount"] and a["date"] == b["date"] and a["family"] == b["family"]:
                        seen_pairs.add(pair)
                        anomalies.append(_anomaly(
                            "DOUBLON", "HAUTE", list(pair),
                            f"Deux transactions {a['source']} identiques ({a['amount']:.2f} EUR, "
                            f"{a['family']}) le {a['date'].isoformat()} pour {a.get('spender')}",
                            person=person,
                        ))
                else:
                    # Double remboursement : une NDF ONEO qui recouvre un paiement carte Airplus
                    if a["family"] != b["family"] or a["family"] == "default":
                        continue
                    tol = max(a["amount"], b["amount"]) * CROSS_SOURCE_TOLERANCE
                    if abs(a["amount"] - b["amount"]) <= tol:
                        seen_pairs.add(pair)
                        anomalies.append(_anomaly(
                            "DOUBLE_REMBOURSEMENT", "HAUTE", list(pair),
                            f"Dépense '{a['family']}' présente à la fois en NDF ONEO et en paiement carte "
                            f"Airplus ({a['amount']:.2f} / {b['amount']:.2f} EUR, "
                            f"{a['date'].isoformat()} / {b['date'].isoformat()}) pour {a.get('spender')}",
                            person=person,
                        ))
    return anomalies


# ---------------------------------------------------------------------------
# Contrôle 4 : fractionnement (contournement de seuil)
# ---------------------------------------------------------------------------

def check_split_transactions(txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anomalies = []
    groups: dict[tuple[str, date, str], list[dict[str, Any]]] = defaultdict(list)
    for tx in txs:
        groups[(person_key(tx), tx["date"], tx["family"])].append(tx)

    for (person, day, family), items in groups.items():
        if len(items) < SPLIT_MIN_COUNT:
            continue
        seuil = THRESHOLDS[family]
        under = [t for t in items if t["amount"] <= seuil]
        total = sum(t["amount"] for t in under)
        if len(under) >= SPLIT_MIN_COUNT and total > seuil:
            anomalies.append(_anomaly(
                "FRACTIONNEMENT", "HAUTE", [t["id"] for t in under],
                f"{len(under)} dépenses '{family}' le {day.isoformat()} pour {items[0].get('spender')} "
                f"toutes sous le seuil ({seuil:.0f} EUR) mais totalisant {total:.2f} EUR",
                person=person, total=round(total, 2), threshold=seuil, family=family,
            ))
    return anomalies


# ---------------------------------------------------------------------------
# Contrôle 5 : montant atypique (outlier statistique par famille)
# ---------------------------------------------------------------------------

def _quartiles(values: list[float]) -> tuple[float, float]:
    q = statistics.quantiles(values, n=4, method="inclusive")
    return q[0], q[2]


def check_statistical_outliers(txs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    anomalies = []
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for tx in txs:
        by_family[tx["family"]].append(tx)

    for family, items in by_family.items():
        amounts = [t["amount"] for t in items]
        if len(amounts) < 3:
            continue
        median = statistics.median(amounts)
        if len(amounts) >= OUTLIER_MIN_SAMPLE:
            q1, q3 = _quartiles(amounts)
            upper = q3 + OUTLIER_IQR_FACTOR * (q3 - q1)
            method = f"IQR (Q3 + {OUTLIER_IQR_FACTOR:g}×IQR = {upper:.2f} EUR)"
        else:
            upper = OUTLIER_MEDIAN_FACTOR * median
            method = f"{OUTLIER_MEDIAN_FACTOR:g}× médiane ({median:.2f} EUR)"
        if upper <= 0:
            continue
        for tx in items:
            if tx["amount"] > upper:
                anomalies.append(_anomaly(
                    "MONTANT_ATYPIQUE", "MOYENNE", [tx["id"]],
                    f"{tx['amount']:.2f} EUR pour '{family}' très au-dessus du lot "
                    f"(médiane {median:.2f} EUR, borne {method}) — {tx.get('spender')} "
                    f"le {tx['date'].isoformat()}",
                    family=family, median=round(median, 2), upper_bound=round(upper, 2),
                ))
    return anomalies


# ---------------------------------------------------------------------------
# Point d'entrée (outil exposé à l'agent)
# ---------------------------------------------------------------------------

CONTROLS = (
    check_thresholds,
    check_weekend,
    check_duplicates,
    check_split_transactions,
    check_statistical_outliers,
)

SEVERITY_SCORE = {"HAUTE": 3, "MOYENNE": 2, "BASSE": 1}


def run_anomaly_controls(transactions: list[dict[str, Any]]) -> dict[str, Any]:
    """Applique les 5 contrôles de détection d'anomalies sur une liste de transactions.

    Args:
        transactions: liste de transactions normalisées. Chaque élément est un dict avec les clés :
            id (str), source ("ONEO" ou "AIRPLUS"), matricule (str|None), spender (str|None),
            date (str "YYYY-MM-DD"), amount (float, EUR), category (str|None), merchant (str|None),
            country (str|None), trip_id (str|None), approval_date (str|None), nights (float|None).

    Returns:
        dict avec :
            - summary : nb de transactions analysées / ignorées, nb d'anomalies par contrôle
            - anomalies : liste détaillée (control, severity, transaction_ids, detail, ...)
            - risk_by_transaction : score de risque cumulé par transaction (plus il est élevé, plus
              la transaction mérite une revue)
            - thresholds : seuils utilisés
    """
    normalized: list[dict[str, Any]] = []
    ignored: list[str] = []
    for i, raw in enumerate(transactions or []):
        tx = normalize_transaction(raw, i)
        if tx is None:
            ignored.append(str(raw.get("id") or i))
        else:
            normalized.append(tx)

    anomalies: list[dict[str, Any]] = []
    for control in CONTROLS:
        anomalies.extend(control(normalized))

    risk: dict[str, int] = defaultdict(int)
    for a in anomalies:
        for tx_id in a["transaction_ids"]:
            risk[tx_id] += SEVERITY_SCORE[a["severity"]]

    per_control: dict[str, int] = defaultdict(int)
    for a in anomalies:
        per_control[a["control"]] += 1

    return {
        "summary": {
            "transactions_analysees": len(normalized),
            "transactions_ignorees": ignored,
            "nb_anomalies": len(anomalies),
            "anomalies_par_controle": dict(per_control),
            "nb_transactions_a_risque": len(risk),
        },
        "anomalies": sorted(anomalies, key=lambda a: -SEVERITY_SCORE[a["severity"]]),
        "risk_by_transaction": dict(sorted(risk.items(), key=lambda kv: -kv[1])),
        "thresholds": THRESHOLDS,
    }
