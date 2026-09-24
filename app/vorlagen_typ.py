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
* Nur BESTEHENDE `.meta.json` werden ergänzt. Für handgeschriebene Vorlagen ohne
  Meta würde eine Meta nur mit `kind` einen leeren Baukasten vortäuschen — das
  überlassen wir dem Sicherheitsnetz und der bewussten Umstellung im Editor.
* Idempotent: eine Vorlage, deren `kind` schon passt, wird nicht angefasst.
* Ein bereits gesetztes abweichendes `kind` (z.B. schon `disclaimer`) wird NICHT
  überschrieben — eine bewusste Zuordnung sticht die Heuristik.
"""
from __future__ import annotations

import json
import logging
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
    """`kind` in einer bestehenden Meta-Datei setzen. True, wenn geschrieben.

    Nur wenn die Meta existiert und das aktuelle `kind` fehlt oder `signatur`
    ist — eine schon gesetzte abweichende Zuordnung bleibt unangetastet.
    """
    meta_pfad = Path(config.TEMPLATE_DIR) / f"{name}.meta.json"
    if not meta_pfad.exists():
        return False
    try:
        meta = json.loads(meta_pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("Vorlage %r: Meta nicht lesbar (%s) — übersprungen", name, exc)
        return False
    if not isinstance(meta, dict):
        return False
    aktuell = meta.get("kind") or "signatur"
    if aktuell == art:
        return False
    if aktuell != "signatur":
        # Bewusst gesetzte andere Art (usermail o.a.) — Heuristik hält sich zurück.
        return False
    meta["kind"] = art
    # Atomar schreiben, Rechte auf der Temp-Datei setzen (replace() übernimmt die
    # der Quelldatei — dieselbe Falle wie in settings_store._save()/vorlagen.py).
    tmp = meta_pfad.parent / f"{meta_pfad.name}.tmp"
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.chmod(0o644)
    tmp.replace(meta_pfad)
    return True


def migriere_bestand() -> list[tuple[str, str]]:
    """Eindeutige Banner/Disclaimer typisieren. Gibt die Änderungen zurück.

    Eine Vorlage wird nur dann umtypisiert, wenn sie AUSSCHLIESSLICH als Banner
    (bzw. Disclaimer) zugewiesen ist. `default`/`signature` bleiben immer außen
    vor. Idempotent — mehrfaches Ausführen ändert nach dem ersten Lauf nichts.
    """
    zuweisungen = zuweisungen_nach_art()
    geaendert: list[tuple[str, str]] = []
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
