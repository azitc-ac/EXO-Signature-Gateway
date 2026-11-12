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

import gruppen
import mailbox_match
import settings_store

WAHL_SLOTS = ("sig", "min", "oof", "intern")

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
    "intern": "intern_template",     # nur interne Empfänger (seit v1.11.56); "" = keine
    "banner": "banner_template",
    "disclaimer": "disclaimer_template",
}


def _gruppen_overrides(sender: str, mailbox_cfg: dict) -> dict[str, str]:
    """{slot: vorlage} aus den Gruppen-Richtlinien, first-match-wins je Slot."""
    custom = settings_store.get("CUSTOM_POLICIES") or []
    if not custom:
        return {}
    groups = gruppen.interne_gruppen()
    sender_key = mailbox_match.match_sender_key(mailbox_cfg, sender)
    if not sender_key:
        return {}
    overrides: dict[str, str] = {}
    for pol in custom:
        if not isinstance(pol, dict) or pol.get("condition_type") != "group":
            continue
        if sender_key in (groups.get(pol.get("group_name", "")) or []):
            slot = pol.get("applies_to", "")
            if slot == "addin":
                continue        # Auswahlliste, kein Einzelwert — addin_freigabe()
            if slot and slot not in overrides:
                overrides[slot] = pol.get("template", "") or ""
    return overrides


def addin_freigabe(sender: str, mailbox_cfg: dict | None = None):
    """Welche Vorlagen darf der Absender im Knopf-Add-in wählen — laut GRUPPEN?

    Gruppen-Regeln mit `applies_to == "addin"` tragen in `template` eine Liste
    von Vorlagennamen oder "*" (alle). Ist der Absender in mehreren solchen
    Gruppen, gilt die VEREINIGUNG (Wunsch aus einem Kundentermin, 11.11.2026:
    drei Firmen mit eigenem Markenauftritt, je Gruppe die passenden Vorlagen).
    Anders als bei den Einzelwert-Slots gibt es hier kein first-match-wins —
    eine Auswahl, die von der Regelreihenfolge abhängt, wäre nicht vorhersehbar.

    None = keine Gruppen-Regel greift; dann gilt die globale Freigabe
    (TEMPLATE_POLICIES["addin"]).
    """
    custom = settings_store.get("CUSTOM_POLICIES") or []
    if not custom:
        return None
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    sender_key = mailbox_match.match_sender_key(mailbox_cfg, sender)
    if not sender_key:
        return None
    groups = gruppen.interne_gruppen()
    treffer = False
    namen: list[str] = []
    for pol in custom:
        if (not isinstance(pol, dict) or pol.get("condition_type") != "group"
                or pol.get("applies_to") != "addin"):
            continue
        if sender_key not in (groups.get(pol.get("group_name", "")) or []):
            continue
        treffer = True
        wert = pol.get("template")
        if wert == "*":
            return "*"
        for n in (wert if isinstance(wert, list) else []):
            if isinstance(n, str) and n not in namen:
                namen.append(n)
    return namen if treffer else None


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
    """{slot: vorlagenname} für sig/min/oof/intern/banner/disclaimer.

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


def antwort_vorlage(vorlagen: dict, in_kette: bool) -> str:
    """Vorlage für eine Antwort/Weiterleitung — EINE Regel für Gateway
    (handler.py, beim Versand) und Verfassen-Add-in (beim Schreiben).

    Hat der Absender in dieser Kette schon eine vom Gateway signierte Mail,
    gilt die Antwort-Signatur (`""` = keine), sonst die volle Signatur."""
    return vorlagen["min"] if in_kette else vorlagen["sig"]


def vorlage_waehlen(vorlagen: dict, in_kette: bool | None, alle_intern: bool,
                    intern_vor_antwort: bool | None = None) -> tuple[str, str]:
    """(Vorlage, Art) — EINE Regel für Gateway (Versand) und Verfassen-Add-in.

    Art ist "intern", "antwort" oder "standard". Reihenfolge:
      1. Alle Empfänger intern UND eine interne Vorlage zugeordnet → intern —
         außer in einer Antwortkette, wenn die Antwort-Logik Vorrang hat
         (Vorgabe; INTERN_VOR_ANTWORT kehrt das um).
      2. In der Kette → Antwort-Signatur (`antwort_vorlage`, "" = keine).
      3. Sonst die volle Signatur.
    `in_kette=None` heisst: keine Antwort bzw. unbekannt.
    """
    if intern_vor_antwort is None:
        intern_vor_antwort = settings_store.get("INTERN_VOR_ANTWORT") is True
    if alle_intern and vorlagen.get("intern") and (not in_kette or intern_vor_antwort):
        return vorlagen["intern"], "intern"
    if in_kette:
        return antwort_vorlage(vorlagen, True), "antwort"
    return vorlagen["sig"], "standard"


def vorlage_fuer(sender: str, slot: str, mailbox_cfg: dict | None = None,
                 sender_cfg: dict | None = None) -> str:
    """Name der Vorlage eines Slots für einen Absender (siehe `aufloesen`)."""
    return aufloesen(sender, mailbox_cfg, sender_cfg)[slot]


def group_vars_for(sender: str, mailbox_cfg: dict | None = None) -> dict:
    """Custom-Variablenwerte, die den Gruppen des Absenders zugewiesen sind.

    `GROUP_VARS = {Gruppenname: {var: wert}}`. Ist der Absender in mehreren Gruppen,
    gewinnt der ERSTE Treffer je Variable (first-match, wie bei den Richtlinien) —
    die Reihenfolge folgt `gruppen.interne_gruppen()` („Alle Postfächer" zuletzt). Rangfolge insgesamt (aufgelöst beim
    Aufrufer): Entra-Feld < Gruppen-Wert < Postfach-Override.
    """
    group_vars = settings_store.get("GROUP_VARS") or {}
    if not group_vars:
        return {}
    groups = gruppen.interne_gruppen()
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
