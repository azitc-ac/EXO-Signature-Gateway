"""Oberflächen-Tests im echten Browser.

WOFÜR
-----
Die übrigen Tests lesen Quelltext. Das genügt für Verträge und Rechenwege, aber
nicht für die Oberfläche: Ob ein Kästchen neben seiner Beschriftung steht, ob
ein Knopf sichtbar ist, ob eine Zeilenbegrenzung greift — das entscheidet sich
erst aus HTML, CSS und JavaScript zusammen, und keine dieser Fragen lässt sich
aus einer Datei beantworten.

Jeder Test hier steht für einen Fehler, der in dieser Form aufgetreten ist:

* `flex-wrap` liess das Ankreuzfeld bei schmaler Anzeige unter seine
  Beschriftung rutschen. Auf dem Bildschirmfoto zu sehen — von mir angesehen
  und nicht als Fehler erkannt.
* Die Zeilenbegrenzung für lange Erklärtexte galt im CSS nur für `p` und `div`.
  Ein `span` bekam im JavaScript eine Bewertung, aber nie eine Begrenzung; die
  Überlaufmessung fiel deshalb immer negativ aus.
* Zwischen geändertem Feld und Speichern-Knopf lagen bis zu zwei
  Bildschirmhöhen.
* Ein Auswahlfeld war mit einem Bezugsweg vorbelegt, über den sich gar nicht
  sammelbestellen lässt.

AUSFÜHRUNG
----------
Braucht `playwright` und einen Chromium. Fehlt eines, werden die Tests
übersprungen — in der CI ist das der Normalfall (dort steht kein Browser).
Lokal:  .venv/bin/python -m pytest tests/test_ui_browser.py
"""
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

pytest.importorskip("playwright", reason="playwright nicht installiert")
pytest.importorskip("uvicorn", reason="uvicorn nicht installiert")

from playwright.sync_api import sync_playwright  # noqa: E402


def _freier_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    """Die Anwendung als echter HTTP-Server, damit ein Browser sie laden kann.

    Bewusst kein Zugriff auf den laufenden Container: Ein Test, der eine
    bestimmte Maschine voraussetzt, läuft nirgends sonst — und er könnte
    Produktivdaten verändern.
    """
    import uvicorn
    from webui.app import app

    port = _freier_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical",
                            lifespan="off")
    server = uvicorn.Server(config)
    faden = threading.Thread(target=server.run, daemon=True)
    faden.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    else:
        pytest.skip("Server ist nicht hochgekommen")
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    faden.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            yield b
            b.close()
    except Exception as exc:                       # kein Browser installiert
        pytest.skip(f"Chromium nicht startbar: {str(exc)[:80]}")


@pytest.fixture
def seite(browser, server, monkeypatch):
    """Angemeldete Sitzung als Verwaltung, Fenstergrösse wie ein Telefon."""
    import sso

    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-oberflaeche")
    keks = sso.create_session_cookie("chefin@example.org", role=sso.ROLE_ADMIN)

    def oeffnen(pfad, breite=393, hoehe=850, thema="light"):
        ctx = browser.new_context(viewport={"width": breite, "height": hoehe})
        ctx.add_cookies([{"name": sso.SESSION_COOKIE, "value": keks,
                          "domain": "127.0.0.1", "path": "/"}])
        pg = ctx.new_page()
        pg.on("dialog", lambda d: d.accept())
        pg.goto(f"{server}{pfad}", wait_until="domcontentloaded", timeout=30000)
        pg.evaluate("t => document.documentElement.setAttribute('data-theme', t)", thema)
        pg.wait_for_timeout(400)
        return pg

    return oeffnen


# ── Ankreuzfelder stehen neben ihrer Beschriftung ────────────────────────────

@pytest.mark.parametrize("breite,schrift", [(393, None), (393, 20), (320, 24)])
def test_kaestchen_bleibt_neben_seiner_beschriftung(seite, breite, schrift):
    """⚠️ `flex-wrap:wrap` liess das Kästchen in eine eigene Zeile rutschen; die
    Beschriftung stand darunter wie eine Überschrift, die Zugehörigkeit war
    nicht mehr zu sehen. Bei grosser Schrift auf schmaler Anzeige tritt es
    zuerst auf — genau der Fall, den ein Blick auf einen 1280er Bildschirm
    nicht zeigt.
    """
    pg = seite("/settings/smime", breite=breite)
    if schrift:
        pg.add_style_tag(content=f"body{{font-size:{schrift}px!important}}")
        pg.wait_for_timeout(200)
    kaputt = pg.evaluate("""() => {
      const fehler = [];
      document.querySelectorAll('input[type=checkbox][id]').forEach(cb => {
        const lb = document.querySelector(`label[for="${CSS.escape(cb.id)}"]`);
        if (!lb) return;
        const a = cb.getBoundingClientRect(), b = lb.getBoundingClientRect();
        if (!a.height || !b.height) return;               // unsichtbar
        if (b.left < a.right - 2) fehler.push(cb.id);     // Beschriftung nicht rechts daneben
      });
      return fehler;
    }""")
    assert not kaputt, f"Kästchen und Beschriftung getrennt bei {breite}px: {kaputt}"


# ── Lange Erklärtexte ────────────────────────────────────────────────────────

def test_lange_erklaertexte_bekommen_einen_schalter_der_wirkt(seite):
    """Zwei Fehler in einem: Der Schalter fehlte (CSS-Selektor auf `p`/`div`
    verengt), und ein Schalter, der nichts aufklappt, wäre schlimmer als keiner.
    """
    pg = seite("/settings/smime")
    pg.wait_for_timeout(700)
    zu_lang = pg.evaluate("""() => [...document.querySelectorAll('.hint')]
        .filter(el => {
          const r = el.getBoundingClientRect();
          if (!r.height) return false;
          const zh = parseFloat(getComputedStyle(el).lineHeight) || 16;
          const hatSchalter = !!(el.nextElementSibling &&
                el.nextElementSibling.classList.contains('hint-toggle'));
          return !hatSchalter && Math.round(r.height / zh) >= 3;
        })
        .map(el => (el.textContent || '').trim().slice(0, 60))""")
    assert not zu_lang, ("Erklärtexte über zwei Zeilen ohne Schalter:\n  "
                         + "\n  ".join(zu_lang))

    # Und der Schalter muss etwas bewirken
    hat = pg.evaluate("""() => {
      const s = document.querySelector('.hint-toggle');
      if (!s) return null;
      const p = s.previousElementSibling;
      const vorher = p.getBoundingClientRect().height;
      s.click();
      return {vorher, nachher: p.getBoundingClientRect().height};
    }""")
    if hat:
        assert hat["nachher"] > hat["vorher"] + 4, (
            f"Schalter klappt nichts auf: {hat}")


# ── Speicherwache und Leiste ─────────────────────────────────────────────────

def test_speicherknopf_ist_erst_nach_einer_aenderung_bedienbar(seite):
    pg = seite("/settings/smime")
    stand = pg.evaluate("""() => {
      const b = document.getElementById('auto-enroll-btn');
      return {gesperrt: b.disabled, hinweis: (b.nextElementSibling||{}).textContent || ''};
    }""")
    assert stand["gesperrt"] is True, "Knopf ist ohne Änderung bedienbar"
    assert not stand["hinweis"].strip(), "meldet etwas, obwohl nichts offen ist"

    danach = pg.evaluate("""() => {
      const cb = document.getElementById('auto-enroll');
      cb.checked = !cb.checked;
      cb.dispatchEvent(new Event('change', {bubbles: true}));
      const b = document.getElementById('auto-enroll-btn');
      return {gesperrt: b.disabled, hinweis: (b.nextElementSibling||{}).textContent || ''};
    }""")
    assert danach["gesperrt"] is False, "Knopf bleibt nach einer Änderung gesperrt"
    assert "nicht gespeichert" in danach["hinweis"], danach


def test_leiste_erscheint_nur_bei_offenen_aenderungen(seite):
    """⚠️ Zwischen Feld und Knopf lagen bis zu zwei Bildschirmhöhen."""
    pg = seite("/settings/smime")
    sichtbar = lambda: pg.evaluate(
        "() => { const el = document.getElementById('speicher-leiste');"
        "        return !!el && !el.hidden; }")
    assert not sichtbar(), "Leiste ist ohne Änderung da"

    pg.evaluate("""() => {
      const el = document.getElementById('smime-tag-encrypted');
      el.value = (el.value || '') + '!';
      el.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    pg.wait_for_timeout(200)
    assert sichtbar(), "Leiste erscheint nicht"
    text = pg.evaluate("() => document.querySelector('.speicher-leiste-text').textContent")
    assert "nicht gespeichert" in text, text


def test_die_leiste_liegt_im_blick_nicht_am_seitenende(seite):
    """Eine Leiste, die mitscrollt statt zu stehen, wäre nur ein weiterer Knopf
    weit unten."""
    pg = seite("/settings/smime")
    pg.evaluate("""() => {
      const el = document.getElementById('smime-tag-encrypted');
      el.value = (el.value || '') + '!';
      el.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    pg.wait_for_timeout(200)
    pg.evaluate("window.scrollTo(0, 1500)")
    pg.wait_for_timeout(300)
    lage = pg.evaluate("""() => {
      const el = document.getElementById('speicher-leiste');
      const r = el.getBoundingClientRect();
      return {position: getComputedStyle(el).position, unten: window.innerHeight - r.bottom};
    }""")
    assert lage["position"] == "fixed", f"Leiste scrollt mit: {lage}"
    assert lage["unten"] < 60, f"Leiste nicht am unteren Rand: {lage}"


# ── Auswahlfelder ────────────────────────────────────────────────────────────

def test_sammelauswahl_bietet_nur_taugliche_bezugswege(seite):
    """⚠️ Das Feld war mit „assistiert manuell" vorbelegt — ein Weg, über den
    sich nicht sammelbestellen lässt. Der erste Klick quittierte mit einer
    Meldung über den Katalog."""
    pg = seite("/smime", breite=1280)
    pg.wait_for_timeout(500)
    optionen = pg.evaluate("""() => {
      const sel = document.getElementById('sammel-anbieter');
      return sel ? [...sel.options].map(o => o.value) : null;
    }""")
    if optionen is None:
        pytest.skip("Sammelbereich nicht vorhanden (kein tauglicher Bezugsweg eingerichtet)")
    assert optionen, "Auswahl ist leer"
    assert "assisted_manual" not in optionen, (
        "Handbetrieb steht zur Wahl, obwohl er je Postfach einen Schritt von "
        "Hand verlangt")


# ── Rolle ────────────────────────────────────────────────────────────────────

def test_bearbeiter_sieht_nur_die_signaturen_in_der_navigation(browser, server, monkeypatch):
    """Die Rechte greifen serverseitig (siehe test_rollen.py). Hier geht es um
    das Gegenstück: Ein Menüpunkt, der zu 403 führt, ist eine Einladung ins
    Leere."""
    import sso
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-oberflaeche")
    keks = sso.create_session_cookie("bearbeiter@example.org", role=sso.ROLE_EDITOR)
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.add_cookies([{"name": sso.SESSION_COOKIE, "value": keks,
                      "domain": "127.0.0.1", "path": "/"}])
    pg = ctx.new_page()
    pg.goto(f"{server}/template", wait_until="domcontentloaded", timeout=30000)
    ziele = pg.evaluate(
        "() => [...document.querySelectorAll('#nav-links a')].map(a => a.getAttribute('href'))")
    ctx.close()
    assert ziele, "Navigation ist leer"
    verboten = [z for z in ziele if z in ("/", "/mailboxes", "/smime", "/settings", "/log")]
    assert not verboten, f"Bearbeiter sieht Menüpunkte, die er nicht öffnen darf: {verboten}"


# ── Wachen über Bereiche mit wechselndem Inhalt ──────────────────────────────

@pytest.mark.parametrize("pfad,knopf", [
    ("/settings/signature", "overrides-btn"),
    ("/settings/signature", "custom-vars-btn"),
    ("/settings/smime", "smime-rules-btn"),
    ("/settings/smime", "kv-mode-btn"),
])
def test_container_wachen_sind_eingerichtet_und_gesperrt(seite, pfad, knopf):
    """⚠️ Vier Knöpfe liessen sich nicht mit festen Feldern überwachen: Ihre
    Zeilen entstehen zur Laufzeit (Overrides, eigene Variablen, S/MIME-Regeln),
    und die Key-Vault-Wahl ist eine Radiogruppe ohne id.

    Der erste Anlauf richtete gar keine Wache ein — `wacheEinrichten()` suchte
    nur `button[data-wache]`, die neuen Knöpfe tragen aber
    `data-wache-container`. Ein Knopf ohne Wache sieht aus wie einer, bei dem
    gerade nichts zu tun ist.
    """
    pg = seite(pfad, breite=1280)
    pg.evaluate("""() => document.querySelectorAll('input[id^=adv-cb-]').forEach(cb => {
        if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true})); }
    })""")
    pg.wait_for_timeout(400)
    d = pg.evaluate("""(id) => {
      const b = document.getElementById(id);
      if (!b) return {fehlt: true};
      const c = document.querySelector(b.dataset.wacheContainer || '');
      return {gesperrt: b.disabled, container: !!c};
    }""", knopf)
    assert not d.get("fehlt"), f"Knopf {knopf} nicht gefunden"
    assert d["container"], f"{knopf}: data-wache-container zeigt ins Leere"
    assert d["gesperrt"] is True, f"{knopf} ist ohne Änderung bedienbar"


@pytest.mark.parametrize("pfad,knopf", [
    ("/settings/signature", "custom-vars-btn"),
    ("/settings/smime", "smime-rules-btn"),
])
def test_neue_zeile_zaehlt_als_aenderung(seite, pfad, knopf):
    """⚠️ Die ANZAHL der Zeilen gehört in den Vergleich: Wer eine leere Zeile
    hinzufügt, hat etwas geändert. Ohne sie wäre „drei leere Felder" derselbe
    Stand wie „keine Felder" — und der Knopf bliebe grau."""
    pg = seite(pfad, breite=1280)
    pg.evaluate("""() => document.querySelectorAll('input[id^=adv-cb-]').forEach(cb => {
        if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true})); }
    })""")
    pg.wait_for_timeout(400)
    pg.evaluate("""(id) => {
      const b = document.getElementById(id);
      const c = document.querySelector(b.dataset.wacheContainer);
      const el = document.createElement('div');
      el.innerHTML = '<input type="text" value="neu">';
      c.appendChild(el);
    }""", knopf)
    pg.wait_for_timeout(400)
    assert pg.evaluate("(id) => !document.getElementById(id).disabled", knopf), (
        "eine neue Zeile hat den Knopf nicht freigegeben")
