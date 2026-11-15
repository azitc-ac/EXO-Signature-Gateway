"""Jede Seite der Oberfläche auf Telefonbreite — gemessen, nicht fotografiert.

ANLASS (2026-11-15)
-------------------
In der Liste der Bypass-Wächter standen drei Knöpfe per `nowrap` neben dem
Text; auf dem Telefon blieben dem Namen wenige Zeichen, er stand Wort für Wort
untereinander. Kein Test hat es gefunden:

* `tools/mobilecheck.py` (seit 27.07.) fotografierte alle Seiten — und
  beurteilte nichts. Es lief nirgends automatisch; wer die Bilder nicht ansah,
  hatte nichts geprüft. Ein Netz, das nichts hält.
* Die Liste entsteht per JavaScript aus einer API. In der Testumgebung gibt es
  keinen Wächter, also stand dort „— noch keiner —“. Eine leere Liste kann
  nicht umbrechen.

Deshalb hier: Seitenliste aus der Routen-Momentaufnahme (eine neue Seite wird
automatisch mitgeprüft), Listen mit Beispieldaten gefüllt, und zwei Messungen
je Seite bei 320 und 393 px:

a) nichts ragt rechts aus dem Fenster, ausser in einem eigenen Rollbereich
   (`.tabellen-rollbereich`, wischbare Reiter). Weil `main` per `overflow-x:
   clip` abschneidet, rollt die Seite nie — die Dokumentbreite allein misst
   deshalb nichts; gemessen wird je Element.
b) kein zusammengequetschter Text: In einer waagerechten Flex-Zeile, in der
   ein Kind Knöpfe trägt, darf ein Kind mit Text (ohne Knöpfe) nicht unter
   GRENZE_PX schrumpfen. Genau die Form der Wächterzeile.

Beispieldaten: wo es geht über die echten Speicher (Setter der Module) — dann
liefert der echte Endpunkt das echte Format. Nur was Azure/EXO braucht, wird
nachgespielt.
"""
import json

import pytest

from test_ui_browser import _infrastruktur_fehlt, browser, server  # noqa: F401

GRENZE_PX = 120
# Ab so vielen Zeilen gilt ein schmaler Textblock als gequetscht. Kalibriert
# 15.11.2026: Kurzbeschriftungen in engen Spalten (1–2 Zeilen) sind kein Befund,
# der Wächtername (Wort für Wort untereinander) brach auf 8+ Zeilen.
ZEILEN_MIN = 3
BREITEN = (393, 320)

# Routen, die keine Seite sind oder die man nicht aufrufen darf. Alles andere
# aus routes_snapshot.json, das mit 200 und text/html antwortet, wird geprüft.
NICHT_AUFRUFEN = {
    "/log/stream": "Server-Sent Events — die Antwort endet nie",
    "/auth/logout": "beendet die Sitzung des Tests",
    "/auth/callback": "Rücksprung von Entra, braucht einen Code",
    "/auth/start": "Weiterleitung zu Entra",
    "/auth/start-redirect": "Weiterleitung zu Entra",
    "/auth/login/microsoft": "Weiterleitung zu Entra",
}

# Seiten, die gemessen werden, deren Befund aber bewusst hingenommen wird —
# NUR mit Grund. Leer ist der Sollzustand.
BEFUND_AKZEPTIERT: dict[str, str] = {}


def _seiten() -> list[str]:
    from pathlib import Path
    schnappschuss = Path(__file__).parent / "routes_snapshot.json"
    return sorted(r["pfad"] for r in json.loads(schnappschuss.read_text())
                  if "GET" in r["methoden"] and "{" not in r["pfad"]
                  and not r["pfad"].startswith(("/static", "/api"))
                  and r["pfad"] not in NICHT_AUFRUFEN)


# ── Beispieldaten ────────────────────────────────────────────────────────────

LANGER_NAME = "Azure – rg-beispiel-waechter/beispiel-waechter-funktion2"

WAECHTER = {"ok": True, "enabled": True, "code_stand": "neu", "rule_state": "an",
            "watchers": [{"id": "w1", "kind": "azure", "name": LANGER_NAME,
                          "last_seen": None,
                          "azure": {"app_name": "beispiel-waechter-funktion2",
                                    "code_stand": "alt", "resource_group": "rg-beispiel-waechter",
                                    "location": "westeurope", "grants_done": True}}]}


def _beispieldaten_anlegen(monkeypatch):
    """Echte Speicher füllen, damit die echten Endpunkte gefüllte Listen liefern."""
    import domain_routing
    import exo_mailboxes
    import held_mails
    import relay_hosts
    import settings_store

    settings_store.update({"SETUP_COMPLETE": True})
    domain_routing.setze_ziel("onprem-beispiel", "mail.beispiel-firma.example", 25, True)
    domain_routing.setze_route("tochter.beispiel-firma.example", "onprem-beispiel")
    domain_routing.setze_tenant_route("11111111-2222-3333-4444-555555555555", "onprem-beispiel")
    relay_hosts.speichern("10.99.0.21", dns="drucker-buchhaltung-obergeschoss.beispiel.example",
                          kommentar="Multifunktionsgerät im Obergeschoss, scannt nach E-Mail",
                          ansprechpartner="Max Mustermann")

    postfaecher = [{"primary": f"postfach{i}@beispiel-firma.example",
                    "display_name": f"Max Mustermann mit recht langem Anzeigenamen {i}",
                    "type": "UserMailbox"} for i in range(3)]
    monkeypatch.setattr(exo_mailboxes, "list_mailboxes", lambda refresh=False: postfaecher)

    roh = (b"From: max@beispiel-firma.example\r\nTo: erika@extern.example\r\n"
           b"Subject: Angebot fuer das Projekt mit einem ziemlich langen Betreff\r\n"
           b"Message-ID: <beispiel@beispiel-firma.example>\r\n\r\nText\r\n")
    held_mails.hold("max@beispiel-firma.example", ["erika@extern.example"], roh)


@pytest.fixture(scope="module")
def sitzung(browser, server):  # noqa: F811
    import sso
    mp = pytest.MonkeyPatch()
    mp.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-oberflaeche")
    mp.setattr(sso, "_noch_gueltig", lambda p: True)
    _beispieldaten_anlegen(mp)
    keks = sso.create_session_cookie("chefin@example.org", role=sso.ROLE_ADMIN)
    yield keks
    mp.undo()


@pytest.fixture(scope="module")
def html_seiten(server, sitzung):  # noqa: F811
    """Die Seiten, die tatsächlich HTML liefern — ermittelt, nicht gepflegt."""
    import httpx
    import sso
    seiten = []
    with httpx.Client(base_url=server, cookies={sso.SESSION_COOKIE: sitzung},
                      timeout=15, follow_redirects=False) as c:
        for pfad in _seiten():
            r = c.get(pfad)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("text/html"):
                seiten.append(pfad)
    if len(seiten) < 15:
        _infrastruktur_fehlt(f"nur {len(seiten)} HTML-Seiten ermittelt: {seiten}")
    return seiten


# ── Messung ──────────────────────────────────────────────────────────────────

_MESSUNG = """([grenze, zeilenMin]) => {
  const sichtbar = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const name = e => (e.id ? '#' + e.id : e.tagName.toLowerCase()
                     + (e.className && typeof e.className === 'string' ? '.' + e.className.trim().split(/\\s+/).join('.') : ''));
  // a) Abgeschnitten oder seitlich rollend. `main` hat `overflow-x: clip` als
  // Netz (style.css) — die Seite rollt dann nie, zu Breites wird still
  // abgeschnitten und ist unerreichbar. Gemessen wird deshalb nicht die
  // Dokumentbreite, sondern jedes Element in `main`, das rechts aus dem
  // Fenster ragt, ohne in einem EIGENEN Roll-/Schneidebereich zu stecken
  // (.tabellen-rollbereich, wischbare Reiter, Ellipsen). Das Netz von `main`
  // selbst zählt nicht als solcher Bereich.
  const ueberlauf = [];
  const haupt = document.querySelector('main') || document.body;
  const eingeschlossen = e => { for (let p = e.parentElement; p && p !== haupt; p = p.parentElement)
      if (getComputedStyle(p).overflowX !== 'visible') return true; return false; };
  // Gemeldet wird das ÄUSSERSTE Element, das hinausragt (sein Elternelement
  // passt noch) — nicht nur Blätter: Eine Tabelle mit `overflow: hidden`
  // schliesst ihre eigenen Zellen ein, ragt aber selbst hinaus.
  const ragt = e => e.getBoundingClientRect().right > innerWidth + 1;
  for (const e of haupt.querySelectorAll('*')) {
    if (!sichtbar(e) || !ragt(e) || eingeschlossen(e)) continue;
    if (e.parentElement && e.parentElement !== haupt && ragt(e.parentElement)) continue;
    ueberlauf.push(name(e) + ' bis x=' + Math.round(e.getBoundingClientRect().right));
    if (ueberlauf.length >= 5) break;
  }
  if (document.documentElement.scrollWidth > innerWidth + 1)
    ueberlauf.push('Dokument ' + document.documentElement.scrollWidth + ' px breit');
  const gequetscht = [];
  for (const c of document.body.querySelectorAll('*')) {
    const s = getComputedStyle(c);
    if (!s.display.includes('flex') || !s.flexDirection.startsWith('row') || !sichtbar(c)) continue;
    const kinder = [...c.children].filter(sichtbar);
    const knopf = k => k.matches('button, .btn') || k.querySelector('button, .btn');
    if (!kinder.some(knopf)) continue;
    for (const k of kinder) {
      if (knopf(k)) continue;
      const text = (k.innerText || '').trim();
      if (text.length < 12) continue;                       // Kurzbeschriftung, Symbol
      const r = k.getBoundingClientRect();
      if (r.width >= grenze) continue;
      // Schmal allein ist kein Fehler („nur bei Bedarf“ in einer engen Spalte).
      // Gequetscht heisst: der Text bricht auf ZEILEN_MIN+ Zeilen um, oder ein
      // Wort läuft über. Gemessen an der ersten Textzeile (Schrift des Blocks).
      const ks = getComputedStyle(k);
      const zeile = parseFloat(ks.lineHeight) || parseFloat(ks.fontSize) * 1.25;
      const zeilen = Math.round(r.height / zeile);
      if (zeilen < zeilenMin && k.scrollWidth <= k.clientWidth + 1) continue;
      gequetscht.push(name(k) + ' ' + Math.round(r.width) + ' px, ' + zeilen + ' Zeilen: „'
                      + text.replace(/\\s+/g, ' ').slice(0, 40) + '“');
    }
  }
  return {ueberlauf, gequetscht};
}"""


def _messen(browser, server, keks, pfad, breite):  # noqa: F811
    import sso
    ctx = browser.new_context(viewport={"width": breite, "height": 850})
    try:
        ctx.add_cookies([{"name": sso.SESSION_COOKIE, "value": keks,
                          "domain": "127.0.0.1", "path": "/"}])
        ctx.route("**/api/watchdog/status*",
                  lambda r: r.fulfill(status=200, content_type="application/json",
                                      body=json.dumps(WAECHTER)))
        pg = ctx.new_page()
        pg.on("dialog", lambda d: d.dismiss())
        pg.goto(f"{server}{pfad}", wait_until="domcontentloaded", timeout=30000)
        try:
            pg.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass                                            # Seiten mit Dauerabfrage
        pg.wait_for_timeout(300)
        return pg.evaluate(_MESSUNG, [GRENZE_PX, ZEILEN_MIN])
    finally:
        ctx.close()


@pytest.mark.parametrize("breite", BREITEN)
def test_keine_seite_laeuft_seitlich_ueber_oder_quetscht_text(browser, server, sitzung,  # noqa: F811
                                                              html_seiten, breite):
    befunde = []
    for pfad in html_seiten:
        m = _messen(browser, server, sitzung, pfad, breite)
        if (m["ueberlauf"] or m["gequetscht"]) and pfad not in BEFUND_AKZEPTIERT:
            befunde.append(f"{pfad}: " + "; ".join(
                [f"ragt hinaus ({', '.join(m['ueberlauf'])})"] * bool(m["ueberlauf"])
                + [f"gequetscht: {g}" for g in m["gequetscht"]]))
    assert not befunde, f"Bei {breite} px:\n  " + "\n  ".join(befunde)


def test_seitenliste_ist_vollstaendig(html_seiten):
    """Die Ermittlung muss die Hauptseiten finden — sonst prüft der Test still
    weniger, als er vorgibt."""
    for pfad in ("/", "/advanced", "/relay", "/settings/routing", "/mailboxes", "/settings"):
        assert pfad in html_seiten, f"{pfad} fehlt in der ermittelten Seitenliste: {html_seiten}"


def test_beispieldaten_kommen_an(browser, server, sitzung):  # noqa: F811
    """Ohne gefüllte Listen misst der Test leere Platzhalter — wie bis v1.12.18."""
    import sso
    ctx = browser.new_context(viewport={"width": 393, "height": 850})
    try:
        ctx.add_cookies([{"name": sso.SESSION_COOKIE, "value": sitzung,
                          "domain": "127.0.0.1", "path": "/"}])
        ctx.route("**/api/watchdog/status*",
                  lambda r: r.fulfill(status=200, content_type="application/json",
                                      body=json.dumps(WAECHTER)))
        pg = ctx.new_page()
        pg.goto(f"{server}/advanced", wait_until="domcontentloaded")
        pg.wait_for_function("() => document.querySelector('#wd-liste strong')", timeout=10000)
        assert LANGER_NAME in pg.inner_text("#wd-liste")
        pg.goto(f"{server}/settings/routing", wait_until="domcontentloaded")
        pg.wait_for_function("() => document.body.innerText.includes('onprem-beispiel')", timeout=10000)
        pg.goto(f"{server}/relay", wait_until="domcontentloaded")
        pg.wait_for_function("() => document.body.innerText.includes('10.99.0.21')", timeout=10000)
        pg.goto(f"{server}/mailboxes", wait_until="domcontentloaded")
        pg.wait_for_function("() => document.body.innerText.includes('postfach1@beispiel-firma.example')",
                             timeout=15000)
    finally:
        ctx.close()
