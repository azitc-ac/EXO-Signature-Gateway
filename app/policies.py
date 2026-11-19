"""Auflösung der Vorlagen für einen Absender — EINE Quelle für alle Leser.

Herausgelöst aus handler.py (v1.9.68), damit der Signatur-Hot-Path, die zentrale
Abwesenheitsnotiz, Self-Service, Add-in, Vorschau und Health-Check DIESELBE
Auflösung teilen. Wer eine Vorlage für ein Postfach braucht, ruft `vorlage_fuer()`
bzw. `aufloesen()` — nie `TEMPLATE_POLICIES` oder `sender_cfg["template"]` direkt.

Zwei Arten von Slots (seit v1.9.113):

* **Wahl-Slots** `sig`, `min`, `oof` — gehören dem Nutzer. Rangfolge:
  Postfach-Wahl > Gruppen-Default (CUSTOM_POLICIES, first-match-wins) >
  globaler Default (TEMPLATE_POLICIES) — ausser die Gruppe schreibt den Slot
  fest (siehe unten). Eine Postfach-Wahl liegt vor, sobald
  das Feld im Eintrag STEHT — auch mit `""` („ausdrücklich keine"). Fehlt das
  Feld, folgt das Postfach dem Default. `use_policy` spielt hier keine Rolle.
* **Richtlinien-Slots** `banner`, `disclaimer` — gehören der Firma. Bei
  `use_policy` (Vorgabe true): globale Richtlinie, von der Gruppe übersteuert.
  Bei `use_policy=false`: das Postfach-eigene Feld (Betreiber-Ausnahme).

⚠️ Vor v1.9.113 koppelte `use_policy` ALLE Slots: eine Signaturwahl im
Self-Service setzte use_policy=false und nahm dem Postfach damit still auch
Banner und Disclaimer der Richtlinie. Die Trennung oben hebt das auf; die
Bestandsmigration ist `settings_store._migrate_v3_to_v4`.

Gruppen-Regeln (`CUSTOM_POLICIES`, Format seit v1.12.90):

    {"condition_type": "group", "group_name": G,
     "vorlagen": {"sig": "X", "min": "", "addin": ["A", "B"] | "*", …},
     "fest": ["sig", "min"]}

* Fehlt ein Slot in `vorlagen`, sagt die Regel dazu nichts; `""` heisst
  „ausdrücklich keine“. Je Slot gilt die ERSTE passende Regel, die ihn trägt;
  `addin` ist die Vereinigung über alle passenden Regeln (`addin_freigabe`).
* `fest` (nur Wahl-Slots): Ist der Slot in der für ihn GREIFENDEN Regel fest,
  zählt die Postfach-Wahl nicht (`aufloesen`), Self-Service und Postfachliste
  sperren die Auswahl. Die Wahl bleibt gespeichert und gilt wieder, sobald die
  Sperre fällt. `use_policy` hebt die Sperre nicht auf.
* Das Altformat (`applies_to` + `template`, eine Regel je Slot) liest
  `regeln()` weiter — jeder Leser geht darüber, auch für zurückgespielte
  Sicherungen. Migration: `settings_store._migrate_v7_to_v8`.
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


# Slots, die eine Gruppen-Regel tragen kann, und die davon festschreibbaren.
REGEL_SLOTS = ("sig", "min", "intern", "oof", "banner", "disclaimer", "addin")
FEST_SLOTS = WAHL_SLOTS


def _regel_wert(slot: str, wert):
    """Wert eines Regel-Slots in seiner einzigen Form: `addin` "*" oder
    Namensliste (ohne Doppelte), alle übrigen eine Zeichenkette."""
    if slot == "addin":
        if wert == "*":
            return "*"
        namen: list[str] = []
        for n in (wert if isinstance(wert, list) else []):
            if isinstance(n, str) and n not in namen:
                namen.append(n)
        return namen
    return wert if isinstance(wert, str) else ""


def regeln(custom) -> list[dict]:
    """Gruppen-Regeln in EINER Form — für jeden Leser von CUSTOM_POLICIES.

    Liefert `[{"condition_type": "group", "group_name", "vorlagen", "fest"}]`.
    Das Altformat `{applies_to, template}` wird zu `{"vorlagen": {applies_to:
    template}, "fest": []}`. Verworfen wird, was die Auflösung ohnehin nie
    ausgewertet hätte: keine Gruppen-Regel, Gruppenname keine Zeichenkette,
    unbekannter Slot im Altformat. `fest` enthält nur Wahl-Slots, die in
    `vorlagen` stehen. Verändert die Eingabe nicht."""
    if not isinstance(custom, list):
        return []
    ergebnis: list[dict] = []
    for pol in custom:
        if not isinstance(pol, dict) or pol.get("condition_type") != "group":
            continue
        gruppe = pol.get("group_name", "")
        if not isinstance(gruppe, str):
            continue
        if "vorlagen" in pol:
            roh = pol.get("vorlagen")
            if not isinstance(roh, dict):
                continue
            vorlagen = {s: _regel_wert(s, roh[s]) for s in REGEL_SLOTS if s in roh}
            fest_roh = pol.get("fest")
            fest_roh = fest_roh if isinstance(fest_roh, list) else []
            fest = [s for s in FEST_SLOTS if s in fest_roh and s in vorlagen]
        else:
            slot = pol.get("applies_to")
            if slot not in REGEL_SLOTS:
                continue
            vorlagen = {slot: _regel_wert(slot, pol.get("template"))}
            fest = []
        ergebnis.append({"condition_type": "group", "group_name": gruppe,
                         "vorlagen": vorlagen, "fest": fest})
    return ergebnis


def zusammenfassen(rg: list[dict]) -> tuple[list[dict], list[str]]:
    """Benachbarte Regeln DERSELBEN Gruppe zu einer zusammenfassen — ohne die
    Wirkung zu ändern (Migration aus dem Altformat, eine Regel je Slot).

    Zwischen zwei benachbarten Regeln steht keine andere, und sie treffen
    dieselben Postfächer: Jeder Slot der zweiten gilt also genau dann, wenn er
    in der ersten fehlt. Steht er in beiden, war der zweite Wert nie wirksam
    (first-match) und entfällt — er wird gemeldet. `addin` wird vereinigt,
    wie `addin_freigabe` es über alle Regeln tut. Liefert (Regeln, Meldungen)."""
    aus: list[dict] = []
    tot: list[str] = []
    for r in rg:
        if not (aus and aus[-1]["group_name"] == r["group_name"]):
            aus.append({"condition_type": "group", "group_name": r["group_name"],
                        "vorlagen": dict(r["vorlagen"]), "fest": list(r["fest"])})
            continue
        letzte = aus[-1]
        for slot, wert in r["vorlagen"].items():
            if slot == "addin" and slot in letzte["vorlagen"]:
                bisher = letzte["vorlagen"]["addin"]
                if bisher == "*" or wert == "*":
                    letzte["vorlagen"]["addin"] = "*"
                else:
                    letzte["vorlagen"]["addin"] = bisher + [n for n in wert if n not in bisher]
            elif slot in letzte["vorlagen"]:
                tot.append(f"{r['group_name']}/{slot}={wert!r}")
            else:
                letzte["vorlagen"][slot] = wert
                if slot in r["fest"]:
                    letzte["fest"].append(slot)
        letzte["fest"] = [s for s in FEST_SLOTS if s in letzte["fest"]]
    return aus, tot


def _gruppen_auswertung(sender: str, mailbox_cfg: dict) -> tuple[dict, dict]:
    """(overrides, fest) aus den Gruppen-Richtlinien — siehe `_auswerten`."""
    rg = regeln(settings_store.get("CUSTOM_POLICIES") or [])
    if not rg:
        return {}, {}
    groups = gruppen.interne_gruppen()
    sender_key = mailbox_match.match_sender_key(mailbox_cfg, sender)
    return _auswerten(rg, groups, sender_key)


def _auswerten(rg: list[dict], groups: dict, sender_key: str) -> tuple[dict, dict]:
    """({slot: vorlage}, {slot: gruppe}) für einen Postfach-Schlüssel.

    Je Slot die ERSTE passende Regel, die ihn trägt (first-match-wins); ist er
    dort in `fest`, ist er festgeschrieben — durch genau diese Gruppe. Eine
    spätere Regel mit `fest` ändert daran nichts. `addin` fehlt hier
    (Auswahlliste, `addin_freigabe`). `rg` kommt aus `regeln()`."""
    if not sender_key:
        return {}, {}
    overrides: dict[str, str] = {}
    fest: dict[str, str] = {}
    for r in rg:
        if sender_key not in (groups.get(r["group_name"]) or ()):
            continue
        for slot, wert in r["vorlagen"].items():
            if slot == "addin" or slot in overrides:
                continue
            overrides[slot] = wert
            if slot in r["fest"]:
                fest[slot] = r["group_name"]
    return overrides, fest


def addin_freigabe(sender: str, mailbox_cfg: dict | None = None):
    """Welche Vorlagen darf der Absender im Knopf-Add-in wählen — laut GRUPPEN?

    Gruppen-Regeln mit `vorlagen["addin"]` tragen eine Liste
    von Vorlagennamen oder "*" (alle). Ist der Absender in mehreren solchen
    Gruppen, gilt die VEREINIGUNG (Wunsch aus einem Kundentermin, 11.11.2026:
    drei Firmen mit eigenem Markenauftritt, je Gruppe die passenden Vorlagen).
    Anders als bei den Einzelwert-Slots gibt es hier kein first-match-wins —
    eine Auswahl, die von der Regelreihenfolge abhängt, wäre nicht vorhersehbar.
    Unabhängig von `fest`: Eine festgeschriebene Signatur schränkt die
    Add-in-Auswahl nicht ein.

    None = keine Gruppen-Regel greift; dann gilt die globale Freigabe
    (TEMPLATE_POLICIES["addin"]).
    """
    rg = regeln(settings_store.get("CUSTOM_POLICIES") or [])
    if not rg:
        return None
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    sender_key = mailbox_match.match_sender_key(mailbox_cfg, sender)
    if not sender_key:
        return None
    groups = gruppen.interne_gruppen()
    treffer = False
    namen: list[str] = []
    for r in rg:
        if "addin" not in r["vorlagen"]:
            continue
        if sender_key not in (groups.get(r["group_name"]) or []):
            continue
        treffer = True
        wert = r["vorlagen"]["addin"]
        if wert == "*":
            return "*"
        for n in wert:
            if n not in namen:
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
    return _std_aus(_gruppen_auswertung(sender, mailbox_cfg)[0])


def _std_aus(over: dict, tp: dict | None = None) -> dict[str, str]:
    if tp is None:
        tp = settings_store.get("TEMPLATE_POLICIES") or {}
    return {slot: _norm(slot, over.get(slot, tp.get(slot))) for slot in SLOT_FELD}


def festgeschrieben(sender: str, mailbox_cfg: dict | None = None) -> dict[str, str]:
    """{slot: gruppe} der Wahl-Slots, die eine Gruppen-Regel für dieses Postfach
    festschreibt — die Postfach-Wahl zählt dort nicht (siehe Modul-Docstring)."""
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    return _gruppen_auswertung(sender, mailbox_cfg)[1]


def standards_alle(adressen, mailbox_cfg: dict) -> dict[str, dict[str, str]]:
    """Siehe `standards_und_fest_alle` — nur die Standards."""
    return standards_und_fest_alle(adressen, mailbox_cfg)[0]


def festgeschrieben_alle(adressen, mailbox_cfg: dict) -> dict[str, dict[str, str]]:
    """`festgeschrieben()` für viele Adressen — {adresse: {slot: gruppe}}."""
    return standards_und_fest_alle(adressen, mailbox_cfg)[1]


def standards_und_fest_alle(adressen, mailbox_cfg: dict):
    """`standards()` und `festgeschrieben()` für viele Adressen auf einmal —
    ({adresse: {slot: vorlage}}, {adresse: {slot: gruppe}}).

    Gleiches Ergebnis wie `standards(a, mailbox_cfg)` je Adresse
    (tests/test_postfach_index.py), aber Gruppen, Schlüsselindex und
    Richtlinien werden EINMAL gelesen: In der Postfachliste kostete die
    Einzelfassung mit einer Gruppenregel je Zeile einen Lauf über alle Einträge
    und eine Kopie aller Gruppenlisten — n² bei hunderten Postfächern."""
    tp = settings_store.get("TEMPLATE_POLICIES") or {}
    custom = regeln(settings_store.get("CUSTOM_POLICIES") or [])
    groups: dict = {}
    keys: dict[str, str] = {}
    if custom:
        # Nur Zeichenketten: ein Schlüssel ist immer eine, und set() vertrüge
        # nichts Unhashbares aus einer verbogenen gespeicherten Gruppe.
        groups = {n: {x for x in (m or ()) if isinstance(x, str)}
                  for n, m in gruppen.interne_gruppen().items()}
        keys = mailbox_match.build_key_index(mailbox_cfg)
    ergebnis: dict[str, dict[str, str]] = {}
    fest_alle: dict[str, dict[str, str]] = {}
    for a in adressen:
        over, fest = (_auswerten(custom, groups, keys.get((a or "").lower(), ""))
                      if custom else ({}, {}))
        ergebnis[a] = _std_aus(over, tp)
        fest_alle[a] = fest
    return ergebnis, fest_alle


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
    over, fest = _gruppen_auswertung(sender, mailbox_cfg)
    std = _std_aus(over)
    use_pol = bool(sender_cfg.get("use_policy", True))   # wie bisher: Wahrheitswert
    ergebnis: dict[str, str] = {}
    for slot in WAHL_SLOTS:
        # Festgeschrieben: die eigene Wahl ruht (bleibt gespeichert, zählt nicht).
        if slot not in fest and hat_eigene_wahl(sender_cfg, slot):
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
