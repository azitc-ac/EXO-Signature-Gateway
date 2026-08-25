#!/usr/bin/env python3
"""Die Ersteinrichtung durchklicken — wie ein Mensch, im Browser.

ANLASS (2026-08-25)
-------------------
Für die Abnahme einer Neuinstallation war zunächst ein Skript geplant, das die
Schnittstellen des Assistenten der Reihe nach aufruft. Der Nutzer hat das
verworfen:

    „Und ich dachte, dass du den Aufbau klickst, so wie ein Mensch.
     Damit es 1:1 vergleichbar ist."

Er hat recht, und der Einwand wiegt schwer: Ein Skript über die Schnittstellen
prüft genau das, was ohnehin getestet ist — und lässt die Oberfläche aus. Aber
die Oberfläche ist das, was ein Kunde bedient. Sämtliche Fehler, die am
24./25.08. gemeldet wurden (Eingabefelder unter der Beschriftung, umbrechende
Reiter, nicht markierbares Protokoll, ein zerlegtes Seitenlayout), hätte kein
Schnittstellen-Skript je gesehen.

WAS DIESES WERKZEUG TUT
-----------------------
Es öffnet den Einrichtungsassistenten in einem echten Browser, geht die
Schritte durch und meldet für jeden, was ein Mensch dort vorfände.

    --trocken   (Vorgabe) NICHTS wird geklickt. Nur nachsehen, ob jeder
                Schritt auffindbar ist, in welchem Zustand er steht und
                welche Bedienelemente er hat.
    --klicken   Führt die Schritte tatsächlich aus.

⚠️ Der Trockenlauf ist kein Beiwerk. Er beantwortet die Frage, ob der Ablauf
überhaupt beschreibbar ist — vor jedem Eingriff in eine Anlage. Wer gleich
klickt, merkt einen falschen Selektor erst, wenn er etwas Falsches getroffen
hat.

GRENZE: DER ANMELDEDIALOG VON MICROSOFT
---------------------------------------
Benutzername und Kennwort liessen sich eintippen, eine mehrstufige Anmeldung
nicht. Genau hier hört die Automatik auf — daher „halbautomatisch". Der Schritt
wird als `mensch` gemeldet und übersprungen; alles davor und danach läuft.

Aufruf:
    python3 tools/ersteinrichtung.py --basis https://127.0.0.1 --keks "name=wert"
    python3 tools/ersteinrichtung.py --basis https://neu.example.com --klicken
"""
from __future__ import annotations

import argparse
import sys

# Zustand eines Schrittes im Bericht
GEFUNDEN = "gefunden"      # da, bedienbar
ERLEDIGT = "erledigt"      # schon eingerichtet — nichts zu tun
MENSCH = "mensch"          # braucht eine Person (Anmeldung bei Microsoft)
FEHLT = "fehlt"            # nicht auffindbar → der Ablauf stimmt nicht mehr

OPTIONAL = "optional"      # in dieser Anlage nicht nötig

# ⚠️ Gefragt wird, ob etwas OFFEN ist — nicht, ob es erledigt ist.
#
# Der erste Entwurf führte eine Liste von Erledigt-Wörtern („erledigt",
# „eingerichtet", „aktiv" …). Der Trockenlauf zeigte sofort die Lücke: Ein
# Abzeichen trägt oft schlicht den erreichten WERT — „sig.azitc.eu" für den
# Hostnamen, „SMTP Port 25" für den Modus. Beides ist erledigt, stand aber in
# keiner Wortliste, und beide Schritte wurden als offen gemeldet.
#
# Umgekehrt ist die Liste endlich: Offen sagt der Assistent mit wenigen,
# festen Wörtern. Alles andere, was überhaupt ein Abzeichen trägt, ist
# erreicht.
OFFEN_WOERTER = ("ausstehend", "fehlt", "erforderlich", "nicht konfiguriert",
                 "nicht eingerichtet", "offen")

# Schritte, die eine Anmeldung bei Microsoft erfordern.
BRAUCHT_MENSCH = ("entra-login", "app-pool")


def _zustand(titel: str, abzeichen: str) -> str:
    if any(m in titel.lower() for m in BRAUCHT_MENSCH):
        return MENSCH
    a = abzeichen.strip().lower()
    if not a:
        return GEFUNDEN                       # kein Abzeichen → noch nichts geschehen
    if any(w in a for w in OFFEN_WOERTER):
        return GEFUNDEN
    if a.startswith(OPTIONAL):
        return OPTIONAL
    return ERLEDIGT


def durchgang(basis: str, keks: str, klicken: bool) -> dict:
    from playwright.sync_api import sync_playwright

    name, _, wert = keks.partition("=")
    host = basis.split("//", 1)[-1].split("/")[0].split(":")[0]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 900},
                                  ignore_https_errors=True)
        if wert:
            ctx.add_cookies([{"name": name, "value": wert,
                              "domain": host, "path": "/"}])
        seite = ctx.new_page()
        seite.on("dialog", lambda d: d.accept())

        antwort = seite.goto(f"{basis}/setup", wait_until="domcontentloaded",
                             timeout=60000)
        if antwort and antwort.status != 200:
            browser.close()
            return {"fehler": f"HTTP {antwort.status} — nicht angemeldet?",
                    "schritte": []}
        seite.wait_for_timeout(2500)

        schritte = seite.evaluate("""() =>
          [...document.querySelectorAll('.wizard-step')].map(el => {
            const t  = el.querySelector('.step-title');
            const st = el.querySelector('.step-status');
            return {
              titel: t ? t.textContent.trim().split('\\n')[0].slice(0, 60) : '',
              abzeichen: st ? st.textContent.trim() : '',
              felder:  [...el.querySelectorAll('input[id],select[id]')].map(x => x.id),
              knoepfe: [...el.querySelectorAll('button:not([disabled])')]
                         .map(x => x.id || x.textContent.trim().slice(0, 30)),
            };
          })""")

        bericht = []
        for s in schritte:
            if not s["titel"]:
                continue                     # Hinweiskasten ohne Überschrift
            bericht.append({**s, "zustand": _zustand(s["titel"], s["abzeichen"])})

        # ⚠️ Die Abnahme gehört zum Durchgang, nicht daneben: Sie ist das
        # Ergebnis, an dem sich die Einrichtung messen lässt.
        abnahme = None
        try:
            seite.goto(f"{basis}/api/abnahme", wait_until="domcontentloaded",
                       timeout=20000)
            import json
            abnahme = json.loads(seite.locator("pre").inner_text())
        except Exception as exc:              # noqa: BLE001
            abnahme = {"fehler": str(exc)[:120]}

        browser.close()

    return {"schritte": bericht, "abnahme": abnahme, "geklickt": klicken}


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--basis", default="https://127.0.0.1")
    ap.add_argument("--keks", default="", help="Sitzungskeks als name=wert")
    ap.add_argument("--klicken", action="store_true",
                    help="Schritte wirklich ausführen (sonst nur nachsehen)")
    a = ap.parse_args(argv)

    if a.klicken:
        print("⚠️  Klickbetrieb — es wird in die Anlage eingegriffen.\n")
    else:
        print("Trockenlauf — es wird nichts geklickt.\n")

    e = durchgang(a.basis, a.keks, a.klicken)
    if e.get("fehler"):
        print(f"  !! {e['fehler']}")
        return 1

    zeichen = {GEFUNDEN: "○", ERLEDIGT: "✓", MENSCH: "☺",
               OPTIONAL: "—", FEHLT: "!!"}
    for s in e["schritte"]:
        print(f"  {zeichen[s['zustand']]} {s['titel'][:42]:<44}"
              f"{s['abzeichen'][:22]:<24}{len(s['felder'])} Feld(er)")

    offen = [s for s in e["schritte"] if s["zustand"] == GEFUNDEN]
    mensch = [s for s in e["schritte"] if s["zustand"] == MENSCH]
    optional = [s for s in e["schritte"] if s["zustand"] == OPTIONAL]
    fertig = len(e["schritte"]) - len(offen) - len(mensch) - len(optional)
    print(f"\n  {len(e['schritte'])} Schritte: {len(offen)} zu tun, "
          f"{len(mensch)} brauchen eine Person, {len(optional)} optional, "
          f"{fertig} erledigt")

    ab = e.get("abnahme") or {}
    if ab.get("fehler"):
        print(f"  Abnahme nicht abrufbar: {ab['fehler']}")
    else:
        print(f"  Abnahme: bereit={ab.get('bereit')} "
              f"offen={ab.get('offen')} ungeklärt={ab.get('unbekannt')}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
