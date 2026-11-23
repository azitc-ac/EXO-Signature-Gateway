"""Auflösung der Vorlagen für einen Absender — EINE Quelle für alle Leser.

Herausgelöst aus handler.py (v1.9.68), damit der Signatur-Hot-Path, die zentrale
Abwesenheitsnotiz, Self-Service, Add-in, Vorschau und Health-Check DIESELBE
Auflösung teilen. Wer eine Vorlage für ein Postfach braucht, ruft `vorlage_fuer()`
bzw. `aufloesen()` (Add-in-Auswahl: `addin_auswahl()`) — nie `TEMPLATE_POLICIES`
oder `sender_cfg["template"]` direkt.

Zwei Ebenen (seit v1.12.99), für JEDEN Slot gleich — sig, min, oof, intern,
banner, disclaimer und addin:

1. **Standardrichtlinie** (`TEMPLATE_POLICIES`, je Slot) — niedrigste Ebene.
2. **Benutzerdefinierte Richtlinie** (`CUSTOM_POLICIES`, Gruppen-Regeln) —
   eine Ebene tiefer, mit Vorrang: Je Slot gilt die ERSTE passende Regel, die
   ihn trägt; `addin` ist die Vereinigung über alle passenden Regeln.

Die für einen Slot GREIFENDE Ebene ist die Gruppen-Regel, sobald eine passende
Regel den Slot trägt, sonst die Standardrichtlinie. Sie liefert den Standard des
Postfachs und entscheidet, ob er FEST ist:

* Gruppen-Regel greift → fest, wenn `RICHTLINIE_GRUPPE_FEST` (Vorgabe an).
* Standardrichtlinie greift → fest, wenn `RICHTLINIE_STANDARD_FEST` (Vorgabe aus).

Ist der Slot fest, gilt der Standard, und die Auswahl ist in Postfachliste und
Self-Service gesperrt. Sonst gilt die Postfach-Wahl, sobald das Feld im Eintrag
STEHT (auch `""` = „ausdrücklich keine“, bei `addin` `[]` = nur die
Standardsignatur); fehlt es, der Standard. Eine Wahl unter Sperre RUHT: Sie
bleibt gespeichert und gilt wieder, sobald die Sperre entfällt.

Es gibt kein Unterbrechen der Vererbung: Wer selbst wählen soll, muss außerhalb
jeder Gruppen-Regel für den Slot liegen (oder der Schalter ist aus).

⚠️ Bis v1.12.98 steuerte `use_policy` je Postfach, ob Banner, Disclaimer und
Add-in-Auswahl aus der Richtlinie kamen, und jede Regel trug `fest` je Slot.
Beides ist entfallen; Bestandsmigration `settings_store._migrate_v8_to_v9` mit
`use_policy_abloesen`, das auch beim Start und beim Import zurückgespielter
Konfigurationen läuft.

Gruppen-Regeln (`CUSTOM_POLICIES`, Format seit v1.12.90):

    {"condition_type": "group", "group_name": G,
     "vorlagen": {"sig": "X", "min": "", "addin": ["A", "B"] | "*", …}}

* Fehlt ein Slot in `vorlagen`, sagt die Regel dazu nichts; `""` heisst
  „ausdrücklich keine“.
* Das Altformat (`applies_to` + `template`, eine Regel je Slot) liest
  `regeln()` weiter — jeder Leser geht darüber, auch für zurückgespielte
  Sicherungen. Ein mitgeliefertes `fest` wird verworfen.
"""
from __future__ import annotations

import gruppen
import mailbox_match
import settings_store

# Einzelwert-Slots — alle sind Postfach-Wahl unter der Festschreibung.
WAHL_SLOTS = ("sig", "min", "oof", "intern", "banner", "disclaimer")
# Dazu die Add-in-Auswahl ("*" oder Namensliste).
ALLE_SLOTS = WAHL_SLOTS + ("addin",)

# Oberflächen-Wert „folgt dem Standard" (Gruppen- bzw. globaler Default) — beim
# Speichern wird das Feld aus dem Postfach-Eintrag ENTFERNT. "" heisst dagegen
# „ausdrücklich keine". Self-Service und Postfach-Seite nutzen denselben Wert.
STANDARD_WAHL = "__standard__"

# Slot → Feld im MAILBOX_CONFIG-Eintrag.
SLOT_FELD = {
    "sig": "template",
    "min": "min_template",
    "oof": "oof_template",
    "intern": "intern_template",     # nur interne Empfänger (seit v1.11.56); "" = keine
    "banner": "banner_template",
    "disclaimer": "disclaimer_template",
    "addin": "addin_templates",      # "*" oder Liste; [] = nur die Standardsignatur
}

# Slots, die eine Gruppen-Regel tragen kann.
REGEL_SLOTS = ("sig", "min", "intern", "oof", "banner", "disclaimer", "addin")

# Die beiden Schalter (Einstellungen → Signatur → Erweitert).
SCHALTER_STANDARD = "RICHTLINIE_STANDARD_FEST"     # Vorgabe aus
SCHALTER_GRUPPE = "RICHTLINIE_GRUPPE_FEST"         # Vorgabe an

# Quelle einer Festschreibung durch die Standardrichtlinie. Sonst steht im
# fest-Wörterbuch der Gruppenname. ⚠️ Leer ist falsy: Leser prüfen
# `slot in fest`, nie `fest.get(slot)`.
QUELLE_STANDARD = ""


def _regel_wert(slot: str, wert):
    """Wert eines Slots in seiner einzigen Form: `addin` "*" oder
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

    Liefert `[{"condition_type": "group", "group_name", "vorlagen"}]`.
    Das Altformat `{applies_to, template}` wird zu `{"vorlagen": {applies_to:
    template}}`. Verworfen wird, was die Auflösung ohnehin nie ausgewertet
    hätte: keine Gruppen-Regel, Gruppenname keine Zeichenkette, unbekannter
    Slot im Altformat — und `fest` (bis v1.12.98 je Regel; seither entscheiden
    die beiden Schalter). Verändert die Eingabe nicht."""
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
        else:
            slot = pol.get("applies_to")
            if slot not in REGEL_SLOTS:
                continue
            vorlagen = {slot: _regel_wert(slot, pol.get("template"))}
        ergebnis.append({"condition_type": "group", "group_name": gruppe,
                         "vorlagen": vorlagen})
    return ergebnis


def zusammenfassen(rg: list[dict]) -> tuple[list[dict], list[str]]:
    """Benachbarte Regeln DERSELBEN Gruppe zu einer zusammenfassen — ohne die
    Wirkung zu ändern (Migration aus dem Altformat, eine Regel je Slot).

    Zwischen zwei benachbarten Regeln steht keine andere, und sie treffen
    dieselben Postfächer: Jeder Slot der zweiten gilt also genau dann, wenn er
    in der ersten fehlt. Steht er in beiden, war der zweite Wert nie wirksam
    (first-match) und entfällt — er wird gemeldet. `addin` wird vereinigt,
    wie die Auswertung es über alle Regeln tut. Liefert (Regeln, Meldungen)."""
    aus: list[dict] = []
    tot: list[str] = []
    for r in rg:
        if not (aus and aus[-1]["group_name"] == r["group_name"]):
            aus.append({"condition_type": "group", "group_name": r["group_name"],
                        "vorlagen": dict(r["vorlagen"])})
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
    return aus, tot


def _schalter() -> tuple[bool, bool]:
    """(Gruppen-Regeln schreiben fest, Standardrichtlinie schreibt fest).

    Gruppe: alles außer ausdrücklich False ist „an“ (Vorgabe an); Standard:
    nur True ist „an“ (Vorgabe aus) — ein fehlender Wert fällt jeweils auf die
    Vorgabe."""
    return (settings_store.get(SCHALTER_GRUPPE) is not False,
            settings_store.get(SCHALTER_STANDARD) is True)


def _gruppen_auswertung(sender: str, mailbox_cfg: dict) -> tuple[dict, dict]:
    """(overrides, quelle) aus den Gruppen-Richtlinien — siehe `_auswerten`."""
    rg = regeln(settings_store.get("CUSTOM_POLICIES") or [])
    if not rg:
        return {}, {}
    groups = gruppen.interne_gruppen()
    sender_key = mailbox_match.match_sender_key(mailbox_cfg, sender)
    return _auswerten(rg, groups, sender_key)


def _auswerten(rg: list[dict], groups: dict, sender_key: str) -> tuple[dict, dict]:
    """({slot: wert}, {slot: gruppe}) für einen Postfach-Schlüssel.

    Je Einzelwert-Slot die ERSTE passende Regel, die ihn trägt
    (first-match-wins); `quelle` nennt deren Gruppe. `addin` ist die
    VEREINIGUNG über alle passenden Regeln (Wunsch aus einem Kundentermin,
    11.11.2026: drei Firmen mit eigenem Markenauftritt, je Gruppe die passenden
    Vorlagen) — eine Auswahl, die von der Regelreihenfolge abhängt, wäre nicht
    vorhersehbar; `quelle["addin"]` nennt alle beteiligten Gruppen. `rg` kommt
    aus `regeln()`."""
    if not sender_key:
        return {}, {}
    overrides: dict = {}
    quelle: dict[str, str] = {}
    addin_gruppen: list[str] = []
    for r in rg:
        if sender_key not in (groups.get(r["group_name"]) or ()):
            continue
        for slot, wert in r["vorlagen"].items():
            if slot == "addin":
                if r["group_name"] not in addin_gruppen:
                    addin_gruppen.append(r["group_name"])
                bisher = overrides.get("addin")
                if bisher is None:
                    overrides["addin"] = wert if wert == "*" else list(wert)
                elif bisher != "*":
                    overrides["addin"] = "*" if wert == "*" else (
                        bisher + [n for n in wert if n not in bisher])
                continue
            if slot in overrides:
                continue
            overrides[slot] = wert
            quelle[slot] = r["group_name"]
    if addin_gruppen:
        quelle["addin"] = ", ".join(addin_gruppen)
    return overrides, quelle


def _fest_aus(quelle: dict, schalter: tuple[bool, bool]) -> dict[str, str]:
    """{slot: quelle} der festen Slots — Gruppenname oder `QUELLE_STANDARD`.

    Entscheidend ist die GREIFENDE Ebene: Greift eine Gruppen-Regel, zählt nur
    der Gruppen-Schalter; der Standard-Schalter betrifft allein Slots, für die
    keine Gruppen-Regel greift."""
    gruppe_fest, standard_fest = schalter
    fest: dict[str, str] = {}
    for slot in ALLE_SLOTS:
        if slot in quelle:
            if gruppe_fest:
                fest[slot] = quelle[slot]
        elif standard_fest:
            fest[slot] = QUELLE_STANDARD
    return fest


def addin_freigabe(sender: str, mailbox_cfg: dict | None = None):
    """Welche Vorlagen darf der Absender im Knopf-Add-in wählen — laut GRUPPEN?

    Die Vereinigung über alle passenden Gruppen-Regeln mit `vorlagen["addin"]`
    ("*" = alle). None = keine Gruppen-Regel greift. Die wirksame Auswahl
    (mit Standardrichtlinie und Postfach-Wahl) liefert `addin_auswahl`."""
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    over, _q = _gruppen_auswertung(sender, mailbox_cfg)
    return over.get("addin")


def _norm(slot: str, wert):
    if slot == "addin":
        return _regel_wert("addin", wert)
    wert = (wert or "").strip() if isinstance(wert, str) else ""
    if slot == "sig":
        return wert or "default"
    return wert


def standards(sender: str, mailbox_cfg: dict | None = None) -> dict:
    """Der Standard je Slot für dieses Postfach — ohne dessen eigene Wahl
    (Gruppen-Regel, sonst Standardrichtlinie).

    Für die Oberfläche („Standard: X") und für die Auflösung.
    """
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    return _std_aus(_gruppen_auswertung(sender, mailbox_cfg)[0])


def _std_aus(over: dict, tp: dict | None = None) -> dict:
    if tp is None:
        tp = settings_store.get("TEMPLATE_POLICIES") or {}
    std = {slot: _norm(slot, over.get(slot, tp.get(slot))) for slot in WAHL_SLOTS}
    # Ohne Angabe bietet das Add-in alle Vorlagen an (wie bisher).
    std["addin"] = _norm("addin", over["addin"] if "addin" in over else tp.get("addin", "*"))
    return std


def festgeschrieben(sender: str, mailbox_cfg: dict | None = None) -> dict[str, str]:
    """{slot: quelle} der Slots, die für dieses Postfach FEST sind — die
    Postfach-Wahl zählt dort nicht. Quelle: Gruppenname oder `QUELLE_STANDARD`
    (siehe Modul-Docstring). Prüfen mit `slot in …`."""
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    return _fest_aus(_gruppen_auswertung(sender, mailbox_cfg)[1], _schalter())


def standards_alle(adressen, mailbox_cfg: dict) -> dict[str, dict]:
    """Siehe `standards_und_fest_alle` — nur die Standards."""
    return standards_und_fest_alle(adressen, mailbox_cfg)[0]


def festgeschrieben_alle(adressen, mailbox_cfg: dict) -> dict[str, dict[str, str]]:
    """`festgeschrieben()` für viele Adressen — {adresse: {slot: quelle}}."""
    return standards_und_fest_alle(adressen, mailbox_cfg)[1]


def standards_und_fest_alle(adressen, mailbox_cfg: dict):
    """`standards()` und `festgeschrieben()` für viele Adressen auf einmal —
    ({adresse: {slot: vorlage}}, {adresse: {slot: quelle}}).

    Gleiches Ergebnis wie `standards(a, mailbox_cfg)` je Adresse
    (tests/test_postfach_index.py), aber Gruppen, Schlüsselindex, Schalter und
    Richtlinien werden EINMAL gelesen: In der Postfachliste kostete die
    Einzelfassung mit einer Gruppenregel je Zeile einen Lauf über alle Einträge
    und eine Kopie aller Gruppenlisten — n² bei hunderten Postfächern."""
    tp = settings_store.get("TEMPLATE_POLICIES") or {}
    custom = regeln(settings_store.get("CUSTOM_POLICIES") or [])
    schalter = _schalter()
    groups: dict = {}
    keys: dict[str, str] = {}
    if custom:
        # Nur Zeichenketten: ein Schlüssel ist immer eine, und set() vertrüge
        # nichts Unhashbares aus einer verbogenen gespeicherten Gruppe.
        groups = {n: {x for x in (m or ()) if isinstance(x, str)}
                  for n, m in gruppen.interne_gruppen().items()}
        keys = mailbox_match.build_key_index(mailbox_cfg)
    ergebnis: dict[str, dict] = {}
    fest_alle: dict[str, dict[str, str]] = {}
    for a in adressen:
        over, quelle = (_auswerten(custom, groups, keys.get((a or "").lower(), ""))
                        if custom else ({}, {}))
        ergebnis[a] = _std_aus(over, tp)
        fest_alle[a] = _fest_aus(quelle, schalter)
    return ergebnis, fest_alle


def festgeschrieben_schluessel(schluessel, mailbox_cfg: dict) -> dict[str, dict[str, str]]:
    """`festgeschrieben()` je Konfigurationsschlüssel statt je Adresse —
    {schlüssel: {slot: quelle}}; für Leser, die Einträge durchgehen."""
    custom = regeln(settings_store.get("CUSTOM_POLICIES") or [])
    schalter = _schalter()
    groups = {n: set(m or ()) for n, m in gruppen.interne_gruppen().items()} if custom else {}
    return {k: _fest_aus(_auswerten(custom, groups, k)[1] if custom else {}, schalter)
            for k in schluessel}


def hat_eigene_wahl(sender_cfg: dict | None, slot: str) -> bool:
    """Steht für diesen Slot eine Postfach-Wahl fest (Feld vorhanden)?"""
    return isinstance(sender_cfg, dict) and SLOT_FELD[slot] in sender_cfg


def ruhende_wahl(sender_cfg: dict | None, fest: dict) -> list[str]:
    """Slots, deren eigene Wahl gerade ruht (fest UND Feld vorhanden)."""
    return [s for s in ALLE_SLOTS if s in fest and hat_eigene_wahl(sender_cfg, s)]


def _wirksam(slot: str, std: dict, fest: dict, sender_cfg) -> object:
    if slot not in fest and hat_eigene_wahl(sender_cfg, slot):
        return _norm(slot, sender_cfg.get(SLOT_FELD[slot]))
    return std[slot]


# Ebene, aus der die wirksame Vorlage eines Slots stammt (`aufloesen_mit_quelle`).
EBENE_STANDARD = "standard"      # Standardrichtlinie (TEMPLATE_POLICIES)
EBENE_GRUPPE = "gruppe"          # Gruppen-Regel (CUSTOM_POLICIES) — Name in "gruppe"
EBENE_POSTFACH = "postfach"      # eigene Wahl im Postfach-Eintrag


def aufloesen_mit_quelle(sender: str, mailbox_cfg: dict | None = None,
                         sender_cfg: dict | None = None) -> dict[str, dict]:
    """Wie `aufloesen`, je Slot mit Herkunft:
    {slot: {"vorlage", "ebene", "gruppe", "fest"}}.

    `ebene` ist `EBENE_STANDARD`, `EBENE_GRUPPE` (dann nennt `gruppe` die
    Regel) oder `EBENE_POSTFACH`; `fest` sagt, ob die greifende Ebene den Slot
    festschreibt (dann ist die Ebene nie `EBENE_POSTFACH`). Die EINE Stelle,
    an der die Herkunft entsteht — Vorschau und Kampagnenauswahl lesen sie
    hier, statt sie aus `festgeschrieben`/`hat_eigene_wahl` nachzurechnen.
    """
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    if sender_cfg is None:
        sender_cfg = mailbox_match.match_sender(mailbox_cfg, sender)
    over, quelle = _gruppen_auswertung(sender, mailbox_cfg)
    std = _std_aus(over)
    fest = _fest_aus(quelle, _schalter())
    ergebnis: dict[str, dict] = {}
    for slot in WAHL_SLOTS:
        # Fest: die eigene Wahl ruht (bleibt gespeichert, zählt nicht).
        if slot not in fest and hat_eigene_wahl(sender_cfg, slot):
            ebene, gruppe = EBENE_POSTFACH, ""
        elif slot in quelle:
            ebene, gruppe = EBENE_GRUPPE, quelle[slot]
        else:
            ebene, gruppe = EBENE_STANDARD, ""
        ergebnis[slot] = {"vorlage": _wirksam(slot, std, fest, sender_cfg),
                          "ebene": ebene, "gruppe": gruppe, "fest": slot in fest}
    return ergebnis


def aufloesen(sender: str, mailbox_cfg: dict | None = None,
              sender_cfg: dict | None = None) -> dict[str, str]:
    """{slot: vorlagenname} für sig/min/oof/intern/banner/disclaimer.

    `sig` ist nie leer (Rückfall "default"); die übrigen Slots sind `""`, wenn
    keine Vorlage gilt. `mailbox_cfg`/`sender_cfg` können vorberechnet übergeben
    werden (Hot-Path in handler.py hat sie bereits). Die Add-in-Auswahl
    liefert `addin_auswahl`, die Herkunft je Slot `aufloesen_mit_quelle`.
    """
    return {slot: h["vorlage"]
            for slot, h in aufloesen_mit_quelle(sender, mailbox_cfg, sender_cfg).items()}


def addin_auswahl(sender: str, mailbox_cfg: dict | None = None,
                  sender_cfg: dict | None = None):
    """Wirksame Add-in-Auswahl: "*" oder Namensliste ([] = nur die
    Standardsignatur) — nach derselben Regel wie `aufloesen`."""
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    if sender_cfg is None:
        sender_cfg = mailbox_match.match_sender(mailbox_cfg, sender)
    over, quelle = _gruppen_auswertung(sender, mailbox_cfg)
    return _wirksam("addin", _std_aus(over), _fest_aus(quelle, _schalter()), sender_cfg)


# ── Ablösung von use_policy (bis v1.12.98) ───────────────────────────────────
# Postfach-eigene Felder, die nur bei use_policy=false galten.
_RICHTLINIEN_FELDER = ("banner_template", "disclaimer_template", "addin_templates")


def use_policy_abloesen(mc, nur_mit_schluessel: bool) -> tuple[dict, list[str]]:
    """MAILBOX_CONFIG ohne `use_policy` — wirkungsgleich, soweit das neue
    Modell es zulässt. Liefert (neue Konfiguration, Meldungen). Verändert die
    Eingabe nicht.

    * use_policy wahr (oder fehlend, wenn `nur_mit_schluessel` aus ist): Banner,
      Disclaimer und Add-in-Auswahl des Postfachs galten NICHT — die Felder
      entfallen, sonst würden sie jetzt als Wahl greifen.
    * use_policy falsch: Die Felder galten; ein fehlendes Feld hiess „keine“
      bzw. „nur die Standardsignatur“ und wird ausdrücklich gesetzt, weil ein
      fehlendes Feld jetzt „folgt dem Standard“ heisst.

    `nur_mit_schluessel`: Beim Start und beim Import zurückgespielter
    Konfigurationen werden nur Einträge angefasst, die den Schlüssel noch
    tragen — ohne ihn ist ein Eintrag bereits im neuen Modell. Die einmalige
    Migration behandelt einen fehlenden Schlüssel wie alle früheren Leser: als
    wahr.
    """
    if not isinstance(mc, dict):
        return mc, []
    neu: dict = {}
    meldungen: list[str] = []
    for key, eintrag in mc.items():
        if not isinstance(eintrag, dict) or (nur_mit_schluessel and "use_policy" not in eintrag):
            neu[key] = eintrag
            continue
        e = dict(eintrag)
        if bool(e.pop("use_policy", True)):
            weg = [f for f in _RICHTLINIEN_FELDER if f in e]
            for f in weg:
                e.pop(f)
            if weg:
                meldungen.append(f"{key}: folgte der Richtlinie, wirkungslose Felder "
                                 f"{', '.join(weg)} entfernt")
        else:
            gesetzt = [f for f in _RICHTLINIEN_FELDER if f not in e]
            e.setdefault("banner_template", "")
            e.setdefault("disclaimer_template", "")
            e.setdefault("addin_templates", [])
            if gesetzt:
                meldungen.append(f"{key}: eigene Banner/Disclaimer/Add-in-Auswahl, "
                                 f"bisher leer gelassene Felder ausdrücklich gesetzt: "
                                 f"{', '.join(gesetzt)}")
        neu[key] = e
    return neu, meldungen


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


# ── Entscheidung je Mail: was hängt das Gateway an? ──────────────────────────
#
# EINE Funktion für den Mailweg (handler.py) und jede Vorschau des
# Richtlinienergebnisses (vorschau.py). Sie rendert nichts und schickt nichts;
# sie sagt je Teil (Signatur, Banner, Disclaimer), welche Vorlage aus welcher
# Ebene gilt, und warum eine Signatur entfällt. Was nur der Mailweg weiß — eine
# Signatur des Add-ins im Entwurf, die Korrekturen für geteilte Postfächer —
# bleibt im handler.

# Was zusätzlich zur (ggf. entfallenden) Signatur angehängt wird — Werte der
# Einstellungen NOSIG_BEHAELT und KETTE_BEHAELT.
ZUSATZ_WAHL = {
    "": frozenset(),
    "banner": frozenset({"banner"}),
    "disclaimer": frozenset({"disclaimer"}),
    "beide": frozenset({"banner", "disclaimer"}),
}

# Die Fälle der Vorschau → (in_kette, alle_intern) wie im Mailweg.
FAELLE = {"neu": (False, False), "antwort": (True, False), "intern": (False, True)}

# Gründe, aus denen keine Signatur angehängt wird.
GRUND_NICHT_AKTIV = "nicht_aktiv"                  # weder Signatur noch S/MIME
GRUND_INTERN_UNVERAENDERT = "intern_unveraendert"  # rein intern, Gateway signiert nicht
GRUND_POSTFACH_OHNE_SIGNATUR = "postfach_ohne_signatur"   # nur S/MIME


def ohne_signatur(eintrag: dict | None) -> bool:
    """Postfach ist eingetragen, aber nur für S/MIME (Haken „Signatur“ aus):
    Es bekommt NIRGENDS eine Signatur, Banner oder Disclaimer — weder im
    Mailweg noch im Verfassen-/Knopf-Add-in noch unter der Abwesenheitsnotiz.
    EINE Regel für alle Wege (Entscheidung 19.11.2026; bis v1.12.103 bekam
    ein solches Postfach überall die volle Signatur). Nicht eingetragen
    (leer/None) ist kein Fall dieser Regel — dafür gilt „nicht betreut“."""
    return bool(eintrag) and not eintrag.get("sig")
GRUND_NOSIG = "nosig"                              # #nosig im Betreff
GRUND_KEINE_ANTWORT_SIGNATUR = "keine_antwort_signatur"   # Kette, Antwort-Signatur „keine“

# Signatur-Art (vorlage_waehlen) → Slot.
_ART_SLOT = {"standard": "sig", "antwort": "min", "intern": "intern"}


def zusatz_umfang(per_nosig: bool, in_kette: bool, alle_intern: bool = False) -> frozenset:
    """Welche von Banner/Disclaimer an diese Mail gehören.

    #nosig im Betreff → NOSIG_BEHAELT; sonst Antwort in einer Kette, in der der
    Absender schon signiert hat → KETTE_BEHAELT; sonst beide. Sind ALLE
    Empfänger intern, gilt zusätzlich INTERN_BEHAELT (Schnittmenge: es bleibt
    nur, was beide Regeln erlauben). Vorgabe "" = interne Post ohne Banner und
    Disclaimer (Entscheidung 19.11.2026; bis v1.12.112 bekam sie beides, sobald
    das Gateway sie signierte). Die Signatur selbst entscheidet `entscheiden` —
    eine schon vorhandene Add-in-Signatur ändert hieran nichts (Banner und
    Disclaimer liefert kein Add-in mit). Unbekannte Werte gelten wie "" (nichts
    anhängen) — so wie die Vorgabe."""
    if per_nosig:
        umfang = ZUSATZ_WAHL.get(settings_store.get("NOSIG_BEHAELT") or "", frozenset())
    elif in_kette:
        umfang = ZUSATZ_WAHL.get(settings_store.get("KETTE_BEHAELT") or "", frozenset())
    else:
        umfang = ZUSATZ_WAHL["beide"]
    if alle_intern:
        umfang = umfang & ZUSATZ_WAHL.get(settings_store.get("INTERN_BEHAELT") or "", frozenset())
    return umfang


def gateway_signiert_intern(vorlagen: dict) -> bool:
    """Signiert das Gateway rein interne Post? SIGN_INTERNAL_ONLY_MAIL, oder
    INTERN_OHNE_ADDIN mit zugeordneter interner Signatur. Sonst leitet der
    handler sie unverändert weiter (internal_only_skip)."""
    return (settings_store.get("SIGN_INTERNAL_ONLY_MAIL") is True
            or (settings_store.get("INTERN_OHNE_ADDIN") is True and bool(vorlagen.get("intern"))))


def privat_vorlage(sender: str) -> str:
    """Private Signatur von `sender` — "" wenn private Mails ausgeschaltet sind
    oder das Postfach keine hat. Nur Postfächer mit Eintrag dürfen privat senden:
    ohne ihn bleibt ein Kürzel oder Add-in-Kopf wirkungslos (Banner und
    Disclaimer bleiben dann dran)."""
    if settings_store.get("PRIVAT_SIG_AKTIV") is not True:
        return ""
    tab = settings_store.get("PRIVAT_SIGNATUREN")
    if not isinstance(tab, dict):
        return ""
    vorlage = tab.get((sender or "").strip().lower())
    return vorlage.strip() if isinstance(vorlage, str) else ""


def entscheiden(sender: str, *, in_kette: bool, alle_intern: bool, per_nosig: bool = False,
                privat: bool = False,
                mailbox_cfg: dict | None = None, sender_cfg: dict | None = None,
                jetzt=None) -> dict:
    """Was das Gateway an eine Mail von `sender` hängt — ohne zu rendern.

    Liefert::

        {"art": "standard" | "antwort" | "intern" | "privat" | "",
         "signatur": {"slot", "vorlage", "ebene", "gruppe", "fest"} | None,
         "signatur_unterdrueckt": bool, "grund": GRUND_… | "",
         "zusatz": frozenset({"banner", "disclaimer"} ⊇ …),
         "banner": {… wie signatur, "kampagne": Name | ""} | None,
         "disclaimer": {… wie signatur} | None,
         "vorlagen": {slot: vorlage}, "herkunft": aufloesen_mit_quelle(…)}

    `signatur` steht auch, wenn sie unterdrückt ist und eine Vorlage hätte
    (dann sagt `grund` warum) — bei #nosig nennt sie die Vorlage, die ohne den
    Trigger gälte. Eine Kampagne ersetzt den Banner nach `banner_campaigns`;
    ihre Art (eigener Banner / Richtlinie / ohne) gilt für DIESES Postfach,
    nicht für eine Person, die im Auftrag schreibt.

    Ein Postfach mit S/MIME, aber ohne Haken „Signatur“ bekommt weder Signatur
    noch Banner noch Disclaimer: In der Postfachliste hängen Antwort-, interne
    Signatur, Banner und Disclaimer am Signatur-Haken (ohne ihn ausgegraut).
    Bis v1.12.102 hängte der handler sie trotzdem an.
    """
    if mailbox_cfg is None:
        mailbox_cfg = settings_store.get("MAILBOX_CONFIG") or {}
    if sender_cfg is None:
        sender_cfg = mailbox_match.match_sender(mailbox_cfg, sender)
    sender_cfg = sender_cfg if isinstance(sender_cfg, dict) else {}
    herkunft = aufloesen_mit_quelle(sender, mailbox_cfg, sender_cfg)
    vorlagen = {slot: h["vorlage"] for slot, h in herkunft.items()}
    erg = {"art": "", "signatur": None, "signatur_unterdrueckt": True, "grund": "",
           "zusatz": frozenset(), "banner": None, "disclaimer": None,
           "vorlagen": vorlagen, "herkunft": herkunft}

    def _teil(slot: str, vorlage: str) -> dict:
        h = herkunft[slot]
        return {"slot": slot, "vorlage": vorlage, "ebene": h["ebene"],
                "gruppe": h["gruppe"], "fest": h["fest"]}

    if not mailbox_cfg or not (sender_cfg.get("sig") or sender_cfg.get("smime")):
        erg["grund"] = GRUND_NICHT_AKTIV
        return erg
    if alle_intern and not gateway_signiert_intern(vorlagen):
        erg["grund"] = GRUND_INTERN_UNVERAENDERT
        if vorlagen.get("intern"):
            # Nur zur Anzeige: Diese setzt allein das Verfassen-Add-in.
            erg["signatur"] = _teil("intern", vorlagen["intern"])
        return erg
    if ohne_signatur(sender_cfg):
        erg["grund"] = GRUND_POSTFACH_OHNE_SIGNATUR
        return erg
    # Private Mail (Kürzel im Betreff oder Wahl im Add-in): private Signatur,
    # kein Banner, kein Disclaimer — `zusatz` bleibt leer. Vor #nosig und der
    # Kette, weil die Absicht ausdrücklich für DIESE Mail erklärt ist.
    privat_tpl = privat_vorlage(sender) if privat else ""
    if privat_tpl:
        erg.update(art="privat", signatur_unterdrueckt=False,
                   signatur={"slot": "privat", "vorlage": privat_tpl, "ebene": EBENE_POSTFACH,
                             "gruppe": "", "fest": False})
        return erg

    vorlage, art = vorlage_waehlen(vorlagen, in_kette, alle_intern)
    erg["art"] = art
    if vorlage:
        erg["signatur"] = _teil(_ART_SLOT[art], vorlage)
    if per_nosig:
        erg["grund"] = GRUND_NOSIG
    elif not vorlage:
        erg["grund"] = GRUND_KEINE_ANTWORT_SIGNATUR
    else:
        erg["signatur_unterdrueckt"] = False

    zusatz = zusatz_umfang(per_nosig, in_kette, alle_intern)
    erg["zusatz"] = zusatz
    if "banner" in zusatz:
        import banner_campaigns
        regulaer = vorlagen["banner"]
        banner, kampagne = banner_campaigns.kampagne_fuer(
            sender, regulaer, jetzt,
            uebernimmt=herkunft["banner"]["ebene"] != EBENE_POSTFACH,
            mailbox_cfg=mailbox_cfg)
        if banner:
            erg["banner"] = dict(_teil("banner", banner), kampagne="")
            if kampagne:
                erg["banner"]["kampagne"] = (kampagne.get("name") or "").strip() or banner
    if "disclaimer" in zusatz and vorlagen["disclaimer"]:
        erg["disclaimer"] = _teil("disclaimer", vorlagen["disclaimer"])
    return erg


def entscheiden_fall(sender: str, fall: str, per_nosig: bool = False,
                     mailbox_cfg: dict | None = None, sender_cfg: dict | None = None,
                     jetzt=None) -> dict:
    """`entscheiden` für einen Fall der Vorschau (`FAELLE`: neu/antwort/intern)."""
    in_kette, alle_intern = FAELLE[fall]
    return entscheiden(sender, in_kette=in_kette, alle_intern=alle_intern, per_nosig=per_nosig,
                       mailbox_cfg=mailbox_cfg, sender_cfg=sender_cfg, jetzt=jetzt)


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
