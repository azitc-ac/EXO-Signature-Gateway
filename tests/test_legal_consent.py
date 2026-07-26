"""Zustimmung zu den Rechtsdokumenten.

Zwei Invarianten, die still brechen würden:

1. **Pfade in CURRENT_DOCUMENTS.** Zeigt ein Pfad ins Leere, liefert
   `compute_document_hash()` den leeren String, `has_valid_consent()` gibt für
   immer False zurück und die Gates sperren dauerhaft — ohne Fehlermeldung.
   Genau das passiert bei einem Versionssprung, wenn die Datei umbenannt und
   die Registry vergessen wird.

2. **Textänderung macht die Zustimmung ungültig.** Darauf beruht Ziffer 13.3
   der Nutzungsbedingungen (aktive Zustimmung statt Zustimmungsfiktion). Fiele
   der Prüfsummenvergleich weg, würde eine geänderte Fassung stillschweigend
   als zugestimmt gelten — also genau die Fiktion, die dort ausgeschlossen ist.
"""
import re
from pathlib import Path

import pytest

import legal_consent

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def lc(tmp_path, monkeypatch):
    """legal_consent gegen einen temporären Dokumentenbaum und eine temporäre
    Datenbank. Niemals gegen /app/data — dort läge die echte Zustimmungshistorie.
    """
    legal = tmp_path / "legal" / "de"
    legal.mkdir(parents=True)
    (legal / "muster-v1.0.md").write_text("Fassung eins.\n", encoding="utf-8")
    monkeypatch.setattr(legal_consent, "_LEGAL_DIR", tmp_path / "legal")
    monkeypatch.setattr(legal_consent, "_DB_PATH", tmp_path / "consent.db")
    monkeypatch.setattr(legal_consent, "CURRENT_DOCUMENTS", {
        "muster": {"version": "1.0", "label_de": "Muster", "label_en": "Sample",
                   "path_de": "de/muster-v1.0.md", "path_en": "de/muster-v1.0.md"},
    })
    monkeypatch.setattr(legal_consent, "CONTEXT_DOCUMENTS", {"gate": ["muster"]})
    return tmp_path / "legal" / "de" / "muster-v1.0.md"


def _zustimmen(doc_id="muster"):
    legal_consent.record_consent(
        doc_id, legal_consent.CURRENT_DOCUMENTS[doc_id]["version"],
        legal_consent.compute_document_hash(doc_id), context="test")


# ── 1. Registry ──────────────────────────────────────────────────────────────

def test_alle_registrierten_dokumente_existieren():
    """Jeder Pfad in CURRENT_DOCUMENTS muss auf eine vorhandene Datei zeigen.

    Bricht, sobald ein Dokument umbenannt und die Registry nicht nachgezogen
    wird — der Fehlerfall, der beim Sprung auf v2.0 drohte.
    """
    fehlend = []
    for doc_id, doc in legal_consent.CURRENT_DOCUMENTS.items():
        for schluessel in ("path_de", "path_en"):
            rel = doc.get(schluessel)
            if rel and not (REPO / "legal" / rel).exists():
                fehlend.append(f"{doc_id}.{schluessel} → {rel}")
    assert not fehlend, "Registry zeigt ins Leere: " + ", ".join(fehlend)


def test_dokumentversion_stimmt_mit_dateiname_und_text_ueberein():
    """Version in der Registry, im Dateinamen und in der Kopfzeile müssen
    zusammenpassen. Sonst behauptet die Oberfläche eine Version, die im
    Dokument selbst nicht steht."""
    abweichend = []
    for doc_id, doc in legal_consent.CURRENT_DOCUMENTS.items():
        v = doc["version"]
        rel = doc["path_de"]
        if f"-v{v}.md" not in rel:
            abweichend.append(f"{doc_id}: Registry {v} ≠ Dateiname {rel}")
            continue
        kopf = (REPO / "legal" / rel).read_text(encoding="utf-8")[:400]
        if not re.search(rf"Version {re.escape(v)}\b", kopf):
            abweichend.append(f"{doc_id}: '{v}' steht nicht in der Kopfzeile")
    assert not abweichend, "; ".join(abweichend)


# ── 2. Prüfsummenbindung ─────────────────────────────────────────────────────

def test_zustimmung_gilt_fuer_den_zugestimmten_text(lc):
    _zustimmen()
    assert legal_consent.has_valid_consent("muster")
    assert legal_consent.context_consented("gate")


def test_textaenderung_macht_zustimmung_ungueltig(lc):
    """Kern von Ziffer 13.3: eine geänderte Fassung gilt NICHT als zugestimmt."""
    _zustimmen()
    lc.write_text("Fassung zwei — geänderte Pflichten.\n", encoding="utf-8")
    assert not legal_consent.has_valid_consent("muster")
    assert not legal_consent.context_consented("gate")


def test_schon_ein_geaendertes_zeichen_reicht(lc):
    """Auch eine redaktionelle Korrektur zieht die Zustimmung. Die README
    behauptete früher das Gegenteil (Minor-Bump ohne erneute Zustimmung);
    diesen Pfad gibt es nicht."""
    _zustimmen()
    lc.write_text("Fassung eins!\n", encoding="utf-8")
    assert not legal_consent.has_valid_consent("muster")


def test_erneute_zustimmung_stellt_gueltigkeit_wieder_her(lc):
    _zustimmen()
    lc.write_text("Fassung zwei.\n", encoding="utf-8")
    assert not legal_consent.has_valid_consent("muster")
    _zustimmen()
    assert legal_consent.has_valid_consent("muster")


# ── 3. Offene Wiederzustimmung ───────────────────────────────────────────────

def test_ohne_jede_zustimmung_kein_banner(lc):
    """Frisches Gateway: die Gates führen durch die Erstzustimmung, ein Banner
    wäre nur Lärm."""
    assert legal_consent.pending_reconsent() == []


def test_nach_textaenderung_erscheint_das_dokument_als_offen(lc):
    _zustimmen()
    lc.write_text("Fassung zwei.\n", encoding="utf-8")
    offen = legal_consent.pending_reconsent()
    assert [d["doc_id"] for d in offen] == ["muster"]
    assert offen[0]["previous_version"] == "1.0"
    assert offen[0]["previous_accepted_at"]


def test_nach_erneuter_zustimmung_ist_nichts_mehr_offen(lc):
    _zustimmen()
    lc.write_text("Fassung zwei.\n", encoding="utf-8")
    assert legal_consent.pending_reconsent()
    _zustimmen()
    assert legal_consent.pending_reconsent() == []


def test_informationsdokumente_erscheinen_nie_als_offen(lc, monkeypatch):
    """Die Datenschutzerklärung ist eine Information nach Art. 13/14 DSGVO,
    keine Willenserklärung — sie darf kein Zustimmungsbanner auslösen."""
    monkeypatch.setitem(legal_consent.CURRENT_DOCUMENTS, "muster",
                        {**legal_consent.CURRENT_DOCUMENTS["muster"],
                         "no_consent_required": True})
    _zustimmen()
    lc.write_text("Fassung zwei.\n", encoding="utf-8")
    assert legal_consent.pending_reconsent() == []


def test_fehlende_datei_erzeugt_kein_banner(lc):
    """Ohne lesbare Datei könnte niemand zustimmen — ein Banner würde ins Leere
    führen. Die Gates sperren dann ohnehin über has_valid_consent()."""
    _zustimmen()
    lc.unlink()
    assert legal_consent.pending_reconsent() == []
    assert not legal_consent.has_valid_consent("muster")
