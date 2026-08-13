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

from hilfen import endpunkt_block, webui_quelltext

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


def test_aenderungshistorie_nennt_die_geltende_fassung():
    """Wo eine Änderungshistorie im Dokument steht, muss ihr oberster Eintrag
    die geltende Fassung sein.

    Sonst liest jemand eine Liste, die die aktuelle Änderung nicht enthält —
    also genau das, wofür sie da ist: nicht das ganze Dokument lesen zu müssen,
    um zu erfahren, was neu ist. Wer die Fassung hochzählt und den Eintrag
    vergisst, fällt hier auf.

    Dokumente ohne Historie werden übersprungen; sie bekommen eine, wenn sie
    das nächste Mal geändert werden.
    """
    fehlend = []
    for doc_id, doc in legal_consent.CURRENT_DOCUMENTS.items():
        for schluessel in ("path_de", "path_en"):
            rel = doc.get(schluessel)
            if not rel:
                continue
            pfad = REPO / "legal" / rel
            if not pfad.exists():
                continue
            text = pfad.read_text(encoding="utf-8")
            if not re.search(r"^##\s+(Änderungen gegenüber|Changes from)", text, re.M):
                continue                      # keine Historie — in Ordnung
            # Erster Listeneintrag nach der Überschrift
            m = re.search(r"^##\s+(?:Änderungen gegenüber|Changes from)[^\n]*\n+-\s+\*\*([^\s*]+)",
                          text, re.M)
            if not m:
                fehlend.append(f"{doc_id}/{schluessel}: Historie ohne Eintrag")
            elif m.group(1) != doc["version"]:
                fehlend.append(
                    f"{doc_id}/{schluessel}: oberster Eintrag {m.group(1)} "
                    f"≠ Fassung {doc['version']}")
    assert not fehlend, "; ".join(fehlend)


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


# ── 3. Zahlwege ──────────────────────────────────────────────────────────────
# Anlass (28.07.2026): Aufladen war moeglich, obwohl `hub-terms` in Fassung 2.2
# vorlag und nur 2.1 bestaetigt war. Der Lizenzkauf hatte sein Gate ueber
# `_HUB_AKTIONEN`, die drei Zahlwege daneben gar keines. Die Registry
# `_ZAHLWEG_KONTEXT` erzwingt jetzt fuer JEDEN eine Entscheidung; diese Tests
# halten fest, dass sie vollstaendig bleibt und die Endpunkte sie auch abrufen.

def test_zahlungskontext_ist_deklariert():
    """`billing_charge` muss in CONTEXT_DOCUMENTS stehen, sonst wirft
    context_consented() bei jedem Aufladeversuch."""
    assert "billing_charge" in legal_consent.CONTEXT_DOCUMENTS
    assert legal_consent.CONTEXT_DOCUMENTS["billing_charge"] == ["hub-terms"]


def test_jeder_kontext_verweist_auf_bekannte_dokumente():
    """Ein Tippfehler im Dokumentnamen liesse das Gate dauerhaft sperren —
    has_valid_consent() findet das Dokument nie."""
    unbekannt = []
    for kontext, docs in legal_consent.CONTEXT_DOCUMENTS.items():
        for d in docs:
            if d not in legal_consent.CURRENT_DOCUMENTS:
                unbekannt.append(f"{kontext} → {d}")
    assert not unbekannt, "Kontext zeigt auf unbekanntes Dokument: " + ", ".join(unbekannt)


def _app_quelltext() -> str:
    """Die GESAMTE Oberfläche, nicht nur `app.py`.

    Die Hub-Endpunkte lagen bis v1.7.189 in `app.py` und liegen jetzt in
    `routen/hub.py`. Eine Prüfung, die weiter nur `app.py` läse, fände sie
    nicht mehr — und meldete „Endpunkt nicht gefunden" statt „Endpunkt ohne
    Gate". Siehe `tests/hilfen.py`.
    """
    return webui_quelltext()


def test_jeder_zahlweg_ist_der_registry_bekannt():
    """Jeder `_zahlweg_gate("x")`-Aufruf muss einen Eintrag in
    `_ZAHLWEG_KONTEXT` haben. Fehlt er, wirft die Funktion zur Laufzeit 500 —
    dieser Test findet es vorher."""
    quelltext = _app_quelltext()
    m = re.search(r'_ZAHLWEG_KONTEXT\s*=\s*\{(.*?)\n\}', quelltext, re.S)
    assert m, "_ZAHLWEG_KONTEXT nicht gefunden"
    bekannt = set(re.findall(r'"([a-z_]+)"\s*:', m.group(1)))
    gerufen = set(re.findall(r'_zahlweg_gate\("([a-z_]+)"\)', quelltext))
    fehlend = gerufen - bekannt
    assert not fehlend, f"nicht in _ZAHLWEG_KONTEXT eingetragen: {sorted(fehlend)}"


def test_registry_hat_keine_toten_eintraege():
    """Ein Eintrag ohne Aufruf ist Dekoration und taeuscht Schutz vor."""
    quelltext = _app_quelltext()
    m = re.search(r'_ZAHLWEG_KONTEXT\s*=\s*\{(.*?)\n\}', quelltext, re.S)
    bekannt = set(re.findall(r'"([a-z_]+)"\s*:', m.group(1)))
    gerufen = set(re.findall(r'_zahlweg_gate\("([a-z_]+)"\)', quelltext))
    tot = bekannt - gerufen
    assert not tot, f"in der Registry, aber nirgends gerufen: {sorted(tot)}"


def test_geldbewegende_endpunkte_haben_ein_gate():
    """Die drei Wege, die am 28.07.2026 offen waren. Wer eines der Gates
    wieder herausnimmt, faellt hier auf."""
    quelltext = _app_quelltext()
    for pfad, zahlweg in (("/api/hub/billing/topup", "topup"),
                          ("/api/hub/billing/auto/setup", "auto_setup"),
                          ("/api/hub/billing/auto/amount", "auto_amount")):
        block = endpunkt_block(quelltext, "post", pfad)
        assert block, f"Endpunkt {pfad} nicht gefunden"
        assert f'_zahlweg_gate("{zahlweg}")' in block, (
            f"{pfad} bewegt Geld, ruft aber _zahlweg_gate('{zahlweg}') nicht auf")


def test_zahlwege_reichen_die_fassungen_an_den_hub():
    """Ohne `doc_versions` faellt die Hub-Pruefung auf die blosse Existenz
    zurueck — dann waere der zweite Riegel so loechrig wie vorher."""
    quelltext = _app_quelltext()
    for pfad in ("/api/hub/billing/topup", "/api/hub/billing/auto/setup",
                 "/api/hub/billing/auto/amount"):
        block = endpunkt_block(quelltext, "post", pfad)
        assert block, f"Endpunkt {pfad} nicht gefunden"
        assert "doc_versions=_doc_versions()" in block, (
            f"{pfad} reicht die geltenden Fassungen nicht an den Hub durch")


def test_abschalten_der_automatik_ist_frei():
    """Eine Bremse braucht kein Gate — sonst haelt eine ausstehende Zustimmung
    den Kunden in der Abbuchung fest."""
    assert legal_consent  # Modul geladen
    quelltext = _app_quelltext()
    m = re.search(r'_ZAHLWEG_KONTEXT\s*=\s*\{(.*?)\n\}', quelltext, re.S)
    eintrag = re.search(r'"auto_disable"\s*:\s*(None|"[a-z_]+")', m.group(1))
    assert eintrag and eintrag.group(1) == "None", (
        "auto_disable muss bewusst ohne Kontext bleiben")
