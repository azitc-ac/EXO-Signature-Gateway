"""Auflösung der Vorlagen für einen Absender — EINE Quelle für alle Leser.

Herausgelöst aus handler.py (v1.9.68), damit der Signatur-Hot-Path, die zentrale
Abwesenheitsnotiz, Self-Service, Add-in, Vorschau und Health-Check DIESELBE
Auflösung teilen. Wer eine Vorlage für ein Postfach braucht, ruft `vorlage_fuer()`
bzw. `aufloesen()` — nie `TEMPLATE_POLICIES` oder `sender_cfg["template"]` direkt.

Zwei Arten von Slots (seit v1.9.113):

* **Wahl-Slots** `sig`, `min`, `oof` — gehören dem Nutzer. Rangfolge:
  Postfach-Wahl > Gruppen-Default (CUSTOM_POLICIES, first-match-wins) >
  globaler Default (TEMPLATE_POLICIES). Eine Postfach-Wahl liegt vor, sobald
  das Feld im Eintrag STEHT — auch mit `""` („ausdrücklich keine"). Fehlt das
  Feld, folgt das Postfach dem Default. `use_policy` spielt hier keine Rolle.
* **Richtlinien-Slots** `banner`, `disclaimer` — gehören der Firma. Bei
  `use_policy` (Vorgabe true): globale Richtlinie, von der Gruppe übersteuert.
  Bei `use_policy=false`: das Postfach-eigene Feld (Betreiber-Ausnahme).

⚠️ Vor v1.9.113 koppelte `use_policy` ALLE Slots: eine Signaturwahl im
Self-Service setzte use_policy=false und nahm dem Postfach damit still auch
Banner und Disclaimer der Richtlinie. Die Trennung oben hebt das auf; die
Bestandsmigration ist `settings_store._migrate_v3_to_v4`.
"""
from __future__ import annotations

import mailbox_match
import settings_store

WAHL_SLOTS = ("sig", "min", "oof")

# Oberflächen-Wert „folgt dem Standard" (Gruppen- bzw. globaler Default) — beim
# Speichern wird das Feld aus dem Postfach-Eintrag ENTFERNT. "" heisst dagegen
# „ausdrücklich keine". Self-Service und Postfach-Seite nutzen denselben Wert.
STANDARD_WAHL = "__standard__"
RICHTLINIEN_SLOTS = ("banner", "disclaimer")

# Slot → Feld im MAILBOX_CONFIG-Eintrag.
SLOT_FELD = {
    "sig": "template",
    "min": "min_template",
    "oof": "oof_template",
    "banner": "banner_template",
    "disclaimer": "disclaimer_template",
}


def _gruppen_overrides(sender: str, mailbox_cfg: dict) -> dict[str, str]:
    """{slot: vorlage} aus den Gruppen-Richtlinien, first-match-wins je Slot."""
    custom = settings_store.get("CUSTOM_POLICIES") or []
    groups = settings_store.get("INTERNAL_GROUPS") or {}
    if not custom or not groups:
        return {}
    sender_key = mailbox_match.match_sender_key(mailbox_cfg, sender)
    if not sender_key:
        return {}
    overrides: dict[str, str] = {}
    for pol in custom:
        if not isinstance(pol, dict) or pol.get("condition_type") != "group":
            continue
        if sender_key in (groups.get(pol.get("group_name", "")) or []):
            slot = pol.get("applies_to", "")
            if slot and slot not in overrides:
                overrides[slot] = pol.get("template", "") or ""
    return overrides


def _norm(slot: str, wert) -> str:
    wert = (wert or "").strip() if isinstance(wert, str) else ""
    if slot == "sig":
        return wert or "default"
    return wert


def standards(sender: str, mailbox_cfg: dict | None = None) -> dict[str, str]:
    """Der DEFAULT je Slot für dieses Postfach — ohne dessen eigene Wahl.

    Für die Oberfläche („Standard: X") und für die Auflösung der Wahl-Slots.
    """
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    tp = settings_store.get("TEMPLATE_POLICIES") or {}
    over = _gruppen_overrides(sender, mailbox_cfg)
    return {slot: _norm(slot, over.get(slot, tp.get(slot))) for slot in SLOT_FELD}


def hat_eigene_wahl(sender_cfg: dict | None, slot: str) -> bool:
    """Steht für diesen Wahl-Slot eine Postfach-Wahl fest (Feld vorhanden)?"""
    return isinstance(sender_cfg, dict) and SLOT_FELD[slot] in sender_cfg


def aufloesen(sender: str, mailbox_cfg: dict | None = None,
              sender_cfg: dict | None = None) -> dict[str, str]:
    """{slot: vorlagenname} für sig/min/oof/banner/disclaimer.

    `sig` ist nie leer (Rückfall "default"); die übrigen Slots sind `""`, wenn
    keine Vorlage gilt. `mailbox_cfg`/`sender_cfg` können vorberechnet übergeben
    werden (Hot-Path in handler.py hat sie bereits).
    """
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    if sender_cfg is None:
        sender_cfg = mailbox_match.match_sender(mailbox_cfg, sender)
    std = standards(sender, mailbox_cfg)
    use_pol = bool(sender_cfg.get("use_policy", True))   # wie bisher: Wahrheitswert
    ergebnis: dict[str, str] = {}
    for slot in WAHL_SLOTS:
        if hat_eigene_wahl(sender_cfg, slot):
            ergebnis[slot] = _norm(slot, sender_cfg.get(SLOT_FELD[slot]))
        else:
            ergebnis[slot] = std[slot]
    for slot in RICHTLINIEN_SLOTS:
        ergebnis[slot] = (std[slot] if use_pol
                          else _norm(slot, sender_cfg.get(SLOT_FELD[slot])))
    return ergebnis


def vorlage_fuer(sender: str, slot: str, mailbox_cfg: dict | None = None,
                 sender_cfg: dict | None = None) -> str:
    """Name der Vorlage eines Slots für einen Absender (siehe `aufloesen`)."""
    return aufloesen(sender, mailbox_cfg, sender_cfg)[slot]


def group_vars_for(sender: str, mailbox_cfg: dict | None = None) -> dict:
    """Custom-Variablenwerte, die den Gruppen des Absenders zugewiesen sind.

    `GROUP_VARS = {Gruppenname: {var: wert}}`. Ist der Absender in mehreren Gruppen,
    gewinnt der ERSTE Treffer je Variable (first-match, wie bei den Richtlinien) —
    die Reihenfolge folgt `INTERNAL_GROUPS`. Rangfolge insgesamt (aufgelöst beim
    Aufrufer): Entra-Feld < Gruppen-Wert < Postfach-Override.
    """
    group_vars = settings_store.get("GROUP_VARS") or {}
    groups = settings_store.get("INTERNAL_GROUPS") or {}
    if not group_vars or not groups:
        return {}
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    key = mailbox_match.match_sender_key(mailbox_cfg, sender)
    if not key:
        return {}
    ergebnis: dict = {}
    for gname, mitglieder in groups.items():
        if key in (mitglieder or []):
            for vname, vwert in (group_vars.get(gname) or {}).items():
                if vname and vwert and vname not in ergebnis:  # first-match-wins
                    ergebnis[vname] = vwert
    return ergebnis
