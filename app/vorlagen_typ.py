"""Auto-Typisierung des Vorlagen-Bestands (kind) beim Start.

WARUM ES DIESE DATEI GIBT
-------------------------
Bis zur Einführung der Vorlagen-Arten hatte jede Vorlage die Art `signatur`
(bzw. gar keine Meta, was auf `signatur` zurückfällt). `banner` und `disclaimer`
waren nur Zuweisungs-Slots, keine eigenen Arten — dieselbe Liste
`list_templates("signatur")` füllte jedes Dropdown. Sobald die Oberfläche je
Zweck nur noch die passende Art zeigt, fielen genau die Vorlagen aus ihren
Banner-/Disclaimer-Dropdowns, die dort zugewiesen sind — sie sind ja technisch
`signatur`.

Diese Migration setzt deshalb einmalig `kind` für die Vorlagen, die sich
**eindeutig** einer Sonderart zuordnen lassen: solche, die AUSSCHLIESSLICH als
Banner (bzw. Disclaimer) zugewiesen sind und nirgends als Signatur. Mehrdeutige
Fälle (eine Vorlage dient als Signatur UND als Banner) bleiben `signatur` —
für sie greift das Sicherheitsnetz der Oberfläche (das Dropdown zeigt die
aktuell zugewiesene Vorlage zusätzlich).

⚠️ KONSERVATIV MIT ABSICHT
--------------------------
* Handgeschriebene Vorlagen ohne Meta bekommen eine, die NUR `kind` trägt.
  Bis v1.9.129 wurden sie ausgelassen, weil eine solche Meta im Editor einen
  leeren Baukasten vorgetäuscht hätte; seit `signature_engine.hat_bausteine()`
  öffnet der Editor sie weiter als Quelltext. Ausgelassen blieb damit gerade
  der häufigste Fall — ein von Hand abgelegter Banner.
* Idempotent: eine Vorlage, deren `kind` schon passt, wird nicht angefasst.
* Ein bereits gesetztes abweichendes `kind` (z.B. schon `disclaimer`) wird NICHT
  überschrieben — eine bewusste Zuordnung sticht die Heuristik.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import config
import settings_store
import signature_engine

log = logging.getLogger("vorlagen_typ")


def zuweisungen_nach_art() -> dict[str, set[str]]:
    """Vorlagenname → Menge der Arten, in denen er zugewiesen ist.

    Wertet ALLE Zuweisungsquellen aus (Richtlinien, Gruppen-Richtlinien,
    Postfach-Übersteuerungen, Banner-Kampagnen). Grundlage der Auto-Typisierung:
    Eine Vorlage mit `{"banner"}` ist ein eindeutiger Banner, eine mit
    `{"signatur", "banner"}` bleibt Signatur.
    """
    art_von_slot = signature_engine.SLOT_ART
    ergebnis: dict[str, set[str]] = {}

    def merke(name: object, art: str) -> None:
        if isinstance(name, str) and name.strip():
            ergebnis.setdefault(name.strip(), set()).add(art)

    # 1) Globale Richtlinien: {slot: vorlagenname}
    tp = settings_store.get("TEMPLATE_POLICIES") or {}
    for slot, art in art_von_slot.items():
        wert = tp.get(slot)
        if slot == "addin":
            # addin ist "*" (alle) oder eine explizite Namensliste von Signaturen.
            if isinstance(wert, list):
                for n in wert:
                    merke(n, art)
        else:
            merke(wert, art)

    # 2) Gruppen-Richtlinien: [{applies_to, template}]
    for pol in settings_store.get("CUSTOM_POLICIES") or []:
        if not isinstance(pol, dict):
            continue
        art = art_von_slot.get(pol.get("applies_to", ""))
        if art:
            merke(pol.get("template"), art)

    # 3) Postfach-Übersteuerungen: {email: {template, min_template, ...}}
    slot_feld = {
        "template": "sig",
        "min_template": "min",
        "banner_template": "banner",
        "disclaimer_template": "disclaimer",
        "oof_template": "oof",
    }
    for cfg in (settings_store.get("MAILBOX_CONFIG") or {}).values():
        if not isinstance(cfg, dict):
            continue
        for feld, slot in slot_feld.items():
            merke(cfg.get(feld), art_von_slot[slot])
        addin = cfg.get("addin_templates")
        if isinstance(addin, list):
            for n in addin:
                merke(n, art_von_slot["addin"])

    # 4) Banner-Kampagnen: [{banner: vorlagenname}]
    for camp in settings_store.get("BANNER_CAMPAIGNS") or []:
        if isinstance(camp, dict):
            merke(camp.get("banner"), "banner")

    return ergebnis


# Arten, die als Sonderart automatisch gesetzt werden dürfen. `signatur` ist die
# Vorgabe (kein Schreiben nötig), `usermail` eine bewusste Zuordnung.
_MIGRIERBAR = ("banner", "disclaimer")


def _setze_kind(name: str, art: str) -> bool:
    """`kind` setzen, wenn die Vorlage noch als Signatur gilt. True, wenn geschrieben.

    Ein bereits gesetztes abweichendes `kind` bleibt unangetastet — eine
    bewusste Zuordnung sticht die Heuristik. Fehlt die Meta (handgeschriebene
    Vorlage), entsteht eine, die NUR die Art trägt; das ist seit
    `signature_engine.hat_bausteine()` gefahrlos, weil der Editor eine solche
    Vorlage weiter als Quelltext öffnet.
    """
    if signature_engine.vorlagen_art(name) != "signatur":
        return False
    return signature_engine.setze_art(name, art)


def _seed_arten() -> dict[str, str]:
    """Name → Art der mitgelieferten Vorlagen, soweit nicht Signatur."""
    import template_seed
    arten: dict[str, str] = {}
    try:
        namen = os.listdir(template_seed.SEED_DIR)
    except OSError:
        return arten
    for f in namen:
        if not f.endswith(".meta.json"):
            continue
        try:
            art = json.loads((Path(template_seed.SEED_DIR) / f).read_text(
                encoding="utf-8")).get("kind")
        except (OSError, ValueError):
            continue
        if art in _MIGRIERBAR:
            arten[f[:-len(".meta.json")]] = art
    return arten


def migriere_bestand() -> list[tuple[str, str]]:
    """Eindeutige Banner/Disclaimer typisieren. Gibt die Änderungen zurück.

    Eine Vorlage wird nur dann umtypisiert, wenn sie AUSSCHLIESSLICH als Banner
    (bzw. Disclaimer) zugewiesen ist. `default`/`signature` bleiben immer außen
    vor. Idempotent — mehrfaches Ausführen ändert nach dem ersten Lauf nichts.
    """
    zuweisungen = zuweisungen_nach_art()
    geaendert: list[tuple[str, str]] = []

    # 1) Mitgelieferte Vorlagen: Bis v1.9.129 trugen „Banner" und „Disclaimer"
    #    im Seed KEINE Art und landeten auf jeder Installation in der
    #    Signaturliste. Die Art kommt jetzt aus dem Seed — aber nur, wenn die
    #    Vorlage nirgends als etwas anderes zugewiesen ist. Wer den Seed-
    #    „Banner" zu seiner Signatur umgebaut und so zugewiesen hat, behält sie.
    #
    #    ⚠️ Nur ohne JEDE Art. Dieser Schritt läuft bei jedem Start; hat jemand
    #    im Editor ausdrücklich „Signatur" gewählt, würde er sonst bei jedem
    #    Neustart zurückgedreht.
    for name, art in _seed_arten().items():
        if zuweisungen.get(name, set()) - {art}:
            continue
        if "kind" in (signature_engine._meta_lesen(name) or {}):
            continue
        if _setze_kind(name, art):
            geaendert.append((name, art))

    # 2) Bestand: eindeutig als Banner bzw. Disclaimer zugewiesen.
    for name, arten in zuweisungen.items():
        if name in ("default", "signature"):
            continue
        # Eindeutig genau eine Sonderart, sonst nichts.
        if len(arten) != 1:
            continue
        art = next(iter(arten))
        if art not in _MIGRIERBAR:
            continue
        if _setze_kind(name, art):
            geaendert.append((name, art))
    if geaendert:
        signature_engine._reload_env()
    return geaendert


# Postfach-eigene Felder der RICHTLINIEN-Slots. Bei use_policy=true wirkungslos
# (die Richtlinie entscheidet) — siehe policies.py und addin.py:api_addin_templates.
# ⚠️ NICHT template/min_template/oof_template: Das sind seit v1.9.113 Nutzerwahlen,
# die unabhängig von use_policy gelten (policies.WAHL_SLOTS). Stünden sie hier,
# löschte dieser bei JEDEM Start laufende Aufräumer jede Self-Service-Wahl.
_POLICY_FELDER = ("banner_template", "disclaimer_template", "addin_templates")


def bereinige_eingefrorene_vorlagen() -> int:
    """Entfernt postfach-eigene Vorlagenfelder aus Einträgen mit use_policy=true.

    Diese Felder wurden früher beim Speichern mitgeschrieben, auch wenn das
    Postfach den Richtlinien folgt — eine eingefrorene Kopie der damaligen
    Richtlinie. Wirkungslos, solange die Übernahme an ist; schaltet man sie
    später ab, würden die veralteten Werte unbemerkt aktiv. Idempotent; gibt die
    Zahl der bereinigten Einträge zurück.

    ⚠️ Nur bei EXPLIZITEM use_policy=true. Ein Alt-Eintrag ohne use_policy-Schlüssel
    (vor Einführung der Richtlinien-Übernahme) trug seine Vorlagen postfach-eigen
    und MUSS sie behalten — darum `is True`, nicht `.get(..., True)`.
    """
    import settings_store
    cfg = settings_store.get("MAILBOX_CONFIG") or {}
    if not isinstance(cfg, dict):
        return 0
    neu: dict = {}
    bereinigt = 0
    for key, eintrag in cfg.items():
        if isinstance(eintrag, dict) and eintrag.get("use_policy") is True:
            rest = {k: v for k, v in eintrag.items() if k not in _POLICY_FELDER}
            if len(rest) != len(eintrag):
                bereinigt += 1
                neu[key] = rest
                continue
        neu[key] = eintrag
    if bereinigt:
        settings_store.update({"MAILBOX_CONFIG": neu})
    return bereinigt
