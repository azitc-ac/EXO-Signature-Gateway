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
Braucht `playwright` (Fassung in tests/requirements-browser.txt) und einen
Chromium. Fehlt eines, werden die Tests übersprungen — lokal bequem.
Lokal:  .venv/bin/python -m pytest tests/test_ui_browser.py

⚠️ In der CI läuft ein eigener Job mit BROWSER_PFLICHT=1. Dann ist fehlende
Infrastruktur (kein playwright, kein Chromium, Server nicht hoch) ein FEHLER,
kein Überspringen. Bis 2026-11-13 liefen diese Tests in der CI nie — der Lauf
war grün, weil alles übersprungen wurde. Ein Netz, das sich still abmeldet,
hält nichts.
"""
import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

PFLICHT = os.environ.get("BROWSER_PFLICHT") == "1"


def _infrastruktur_fehlt(grund: str):
    """Überspringen — außer im Pflichtmodus der CI, dort ist es ein Fehler."""
    if PFLICHT:
        pytest.fail(f"BROWSER_PFLICHT: {grund}", pytrace=False)
    pytest.skip(grund)


try:
    import playwright  # noqa: F401
    import uvicorn  # noqa: F401
except ImportError as exc:
    if PFLICHT:
        raise RuntimeError(f"BROWSER_PFLICHT, aber {exc}") from exc
    pytest.skip(f"playwright/uvicorn nicht installiert: {exc}", allow_module_level=True)

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
        _infrastruktur_fehlt("Server ist nicht hochgekommen")
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
        _infrastruktur_fehlt(f"Chromium nicht startbar: {str(exc)[:80]}")


@pytest.fixture
def seite(browser, server, monkeypatch):
    """Angemeldete Sitzung als Verwaltung, Fenstergrösse wie ein Telefon."""
    import sso

    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-oberflaeche")
    # Widerruf (sso._noch_gueltig) ist hier nicht Gegenstand — eigene Tests
    # in test_sitzung_widerruf.py; die Testkonten stehen in keiner ADMIN_USERS.
    monkeypatch.setattr(sso, "_noch_gueltig", lambda p: True)
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


def test_nachtraeglich_gesetzter_hinweis_bekommt_einen_schalter(seite):
    """Ein Hinweis, der beim Seitenaufbau leer ist und dessen Text ein Skript
    erst danach setzt (DMARC-Ziel, „Letzter Abruf …“), wurde bis 2026-11-21 beim
    nächsten resize auf zwei Zeilen gekürzt — OHNE Schalter: abgeschnitten, nicht
    aufklappbar. `data-clamp="nie"` aus der Vorlage muss dabei ungekürzt bleiben."""
    pg = seite("/settings/dmarc")
    pg.evaluate("""() => {
      const karte = document.querySelector('.settings-card') || document.body;
      for (const [id, nie] of [['t-leer', false], ['t-nie', true]]) {
        const d = document.createElement('div');
        d.id = id; d.className = 'hint';
        if (nie) d.dataset.clamp = 'nie';
        karte.appendChild(d);
      }
    }""")
    pg.wait_for_timeout(400)                                # leer bewertet: „passt“
    lang = "Ein langer Hinweis, der nachträglich gesetzt wird. " * 12
    pg.evaluate("t => { for (const id of ['t-leer', 't-nie']) "
                "document.getElementById(id).textContent = t; }", lang)
    pg.wait_for_timeout(400)
    pg.set_viewport_size({"width": 380, "height": 850})    # Adressleiste o. ä.
    pg.wait_for_timeout(500)
    stand = pg.evaluate("""() => {
      const h = id => document.getElementById(id);
      const s = h('t-leer').nextElementSibling;
      const schalter = !!(s && s.classList.contains('hint-toggle') && s.style.display !== 'none');
      const zu = h('t-leer').getBoundingClientRect().height;
      if (schalter) s.click();
      return {schalter, zu, auf: h('t-leer').getBoundingClientRect().height,
              nie: h('t-nie').dataset.clamp,
              nieZeilen: h('t-nie').scrollHeight > h('t-nie').clientHeight + 4};
    }""")
    assert stand["schalter"], f"gekürzt ohne Schalter: {stand}"
    assert stand["auf"] > stand["zu"] + 4, f"Schalter klappt nichts auf: {stand}"
    assert stand["nie"] == "nie" and not stand["nieZeilen"], f"„nie“ wurde gekürzt: {stand}"


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

def test_sammelauswahl_bietet_nur_taugliche_bezugswege(seite, monkeypatch):
    """⚠️ Das Feld war mit „assistiert manuell" vorbelegt — ein Weg, über den
    sich nicht sammelbestellen lässt. Der erste Klick quittierte mit einer
    Meldung über den Katalog.

    Die Bezugswege werden vorgegeben: In der Testumgebung ist keiner
    einsatzbereit, der Sammelbereich fehlte, und der Test übersprang sich in
    JEDEM CI-Lauf (bis v1.12.23) — er prüfte also nie etwas."""
    import ca_backends

    async def _wege():
        return [
            {"name": "castle", "label": "CASTLE", "auto": True, "ready": True,
             "not_ready_reason": "", "hub": False},
            {"name": "assisted_manual", "label": "Assistiert manuell", "auto": False,
             "ready": True, "not_ready_reason": "", "hub": False},
            {"name": "hub:test", "label": "Katalog-CA", "auto": True, "ready": False,
             "not_ready_reason": "nicht eingerichtet", "hub": True},
        ]
    monkeypatch.setattr(ca_backends, "list_backends_aktuell", _wege)
    # Der Bereich erscheint nur, wenn es S/MIME-Postfächer gibt.
    import smime_store
    monkeypatch.setattr(smime_store, "list_certs", lambda: [{"email": "max@firma.at"}])
    pg = seite("/smime", breite=1280)
    pg.wait_for_timeout(500)
    optionen = pg.evaluate("""() => {
      const sel = document.getElementById('sammel-anbieter');
      return sel ? [...sel.options].map(o => o.value) : null;
    }""")
    assert optionen is not None, "Sammelbereich fehlt trotz einsatzbereitem Bezugsweg"
    assert optionen == ["castle"], optionen
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
    # Widerruf (sso._noch_gueltig) ist hier nicht Gegenstand — eigene Tests
    # in test_sitzung_widerruf.py; die Testkonten stehen in keiner ADMIN_USERS.
    monkeypatch.setattr(sso, "_noch_gueltig", lambda p: True)
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


# ── Beschriftung und Eingabefeld auf einer Zeile ─────────────────────────────

@pytest.mark.parametrize("pfad", [
    "/settings/smime", "/settings/signature", "/advanced",
])
def test_eingabefeld_steht_neben_seiner_beschriftung(seite, pfad):
    """Auf breitem Bildschirm gehört das Feld NEBEN die Beschriftung, nicht darunter.

    ⚠️ ANLASS (24.08.2026): An sieben Stellen auf drei Seiten stand das
    Eingabefeld unter seiner Beschriftung, obwohl daneben reichlich Platz war —
    Portal-Basis-URL, Sammeladresse, Firmenname, beide Trigger-Felder und zwei
    Ankreuzfelder.

    Ursache war nicht die Breite, sondern der Erklärtext: `.settings-control`
    hatte keine Flex-Basis, also bestimmte der längste Hinweistext dieser Spalte
    ihre Breite. Zusammen mit der 200px-Beschriftung sprengte das den Rahmen,
    und `flex-wrap` am Elternteil schob das Feld nach unten. Mit
    `flex: 1 1 0; min-width: 0` nimmt die Spalte den Restplatz, und der Text
    umbricht darin.

    Geprüft wird bei 1280px — schmaler ist der Umbruch richtig und ab 600px
    ohnehin per Medienabfrage gewollt.
    """
    pg = seite(pfad, breite=1280)
    # Erweiterte Bereiche aufklappen, sonst sind mehrere Zeilen unsichtbar
    pg.evaluate("""() => document.querySelectorAll('input[id^=adv-cb-]').forEach(cb => {
        if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true})); }
    })""")
    pg.wait_for_timeout(400)

    umgebrochen = pg.evaluate("""() => {
      const fehler = [];
      document.querySelectorAll('.settings-row').forEach(row => {
        // ⚠️ NUR die Feld-Form. Seit 2026-08-25 gibt es zwei Formen (CLAUDE.md,
        // „Aufbau der Einstellungszeilen"): Beim SCHALTER steht das Kästchen
        // oben und der Erklärtext ABSICHTLICH darunter — dort ist der Umbruch
        // kein Fehler, sondern die Form. Ohne diese Einschränkung meldete der
        // Test fünf richtig gebaute Zeilen.
        const lb = row.querySelector(':scope > label:not(.checkbox-label)');
        const ctl = row.querySelector(':scope > .settings-control');
        if (!lb || !ctl) return;
        const a = lb.getBoundingClientRect(), b = ctl.getBoundingClientRect();
        if (!a.height || !b.height) return;                 // eingeklappt
        // Umgebrochen = die Spalte beginnt unterhalb der Beschriftung
        if (b.top >= a.bottom - 2) fehler.push((lb.textContent || '').trim().slice(0, 40));
      });
      return fehler;
    }""")
    assert not umgebrochen, (
        f"Bei 1280px steht das Feld unter der Beschriftung: {umgebrochen}\n"
        "Meist fehlt `.settings-control` die Flex-Basis, sodass ein langer "
        "Erklärtext die Spaltenbreite bestimmt.")


@pytest.mark.parametrize("pfad", [
    "/advanced", "/settings", "/settings/smime", "/settings/signature", "/settings/routing",
])
def test_knopf_und_auswahl_in_der_feldspalte_nicht_gedehnt(seite, pfad):
    """`.settings-control` ist eine Flex-Spalte; ohne eigene Regel dehnte sie
    Speichern-Knöpfe und Auswahllisten auf die ganze Breite (Firewall-Feld,
    Log-Level, Zeitzone, Exchange-Erkennung)."""
    pg = seite(pfad, breite=1280)
    pg.evaluate("""() => document.querySelectorAll('input[id^=adv-cb-]').forEach(cb => {
        if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true})); }
    })""")
    pg.wait_for_timeout(300)
    befund = pg.evaluate("""() => {
      const r = {gemessen: 0, gedehnt: []};
      document.querySelectorAll('.settings-control > .btn, .settings-control > select').forEach(el => {
        const b = el.getBoundingClientRect(), s = el.parentElement.getBoundingClientRect();
        if (!b.width || s.width < 400) return;
        r.gemessen++;
        // Volle Breite allein ist kein Befund: Eine Auswahl mit langen Optionen
        // (Signaturbilder) braucht sie. Gedehnt ist, was sie über `stretch` bekommt.
        const dehnt = !['flex-start', 'start', 'center', 'flex-end', 'end']
          .includes(getComputedStyle(el).alignSelf);
        if (b.width >= s.width - 2 && dehnt) r.gedehnt.push(el.id || el.textContent.trim().slice(0, 30));
      });
      return r;
    }""")
    if pfad == "/advanced":
        assert befund["gemessen"] >= 2, befund                # der Suchlauf greift
    assert not befund["gedehnt"], f"Über die ganze Spalte gedehnt: {befund['gedehnt']}"


@pytest.mark.parametrize("breite", [1280, 393])
@pytest.mark.parametrize("pfad, ziel", [
    ("/setup#exo-auto-ausnahme", "exo-auto-ausnahme"),
    ("/advanced#watchdog", "watchdog"),
])
def test_ankersprung_zeigt_die_karte_ab_ihrem_kopf(seite, monkeypatch, pfad, ziel, breite):
    """Ein Ankerziel tief in einer Karte landete unter der festen Navigation,
    Kopf und Schritt-Nummer lagen ausser Sicht. Jetzt steht der Kartenkopf
    direkt unter der Navigation und das Ziel darunter im Bild."""
    import settings_store
    # Der Abschnitt mit der Auto-Antwort-Ausnahme steht erst nach eingerichtetem Connector da.
    vorher = bool(settings_store.get("EXO_CONNECTOR_CREATED"))
    settings_store.update({"EXO_CONNECTOR_CREATED": True})
    try:
        pg = seite(pfad, breite=breite, hoehe=850)
    finally:
        settings_store.update({"EXO_CONNECTOR_CREATED": vorher})
    # Erst nach `load` steht die endgültige Lage (ankerZeigen zieht dort nach);
    # auf einem ausgelasteten Rechner kam die Messung sonst davor.
    pg.wait_for_load_state("load")
    pg.wait_for_timeout(600)
    lage = pg.evaluate("""z => {
      const nav = document.querySelector('nav').getBoundingClientRect().bottom;
      const el = document.getElementById(z);
      if (!el) return null;
      const karte = el.closest('.wizard-step, .settings-card, .card') || el;
      return {nav, karte: karte.getBoundingClientRect().top,
              ziel: el.getBoundingClientRect().top, hoehe: innerHeight,
              y: scrollY, eigen: karte === el};
    }""", ziel)
    assert lage, f"Ziel {ziel} fehlt auf der Seite"
    assert lage["y"] > 0, lage                                  # es wurde überhaupt gescrollt
    passt = lage["ziel"] - lage["karte"] + 80 <= lage["hoehe"] - lage["nav"]
    if passt:      # Kopf direkt unter der Navigation, Ziel darunter im Bild
        assert lage["nav"] <= lage["karte"] <= lage["nav"] + 16, lage
        assert lage["ziel"] < lage["hoehe"], lage
    else:          # Karte zu hoch (Telefon): dann das Ziel selbst, nicht darunter versteckt
        assert lage["nav"] <= lage["ziel"] <= lage["nav"] + 16, lage


# ── Lizenzmenge: kostenpflichtig eingeben, Gesamtzahl senden ─────────────────

def test_lizenzfeld_meint_kostenpflichtige_postfaecher(seite):
    """Das Feld trägt die kostenpflichtigen Postfächer — der Hub die Gesamtzahl.

    ⚠️ ANLASS (24.08.2026): Das Feld hiess „Postfächer insgesamt (einschliesslich
    der freien)" und war mit `frei + 1` vorbelegt — also mit einer Kaufabsicht,
    die niemand geäussert hatte. Wer 20 zusätzliche Postfächer wollte, musste
    120 eintragen und selbst umrechnen.

    Hier hängt echtes Geld dran: Die Umrechnung liegt deshalb an genau EINER
    Stelle (`_gesamt()`), und dieser Test hält sie fest. Der Kauf selbst wird
    nicht ausgelöst — geprüft werden die reinen Rechenfunktionen im geladenen
    Dokument.
    """
    pg = seite("/settings/connect", breite=1280)
    pg.evaluate("() => { _licPreise = {frei_postfaecher: 100, monat_cents: 100, "
                "jahr_cents: 1080, vat_percent: 19}; }")

    assert pg.evaluate("() => _gesamt(0)") == 100, (
        "0 kostenpflichtige = 100 gesamt (nur die freien)")
    assert pg.evaluate("() => _gesamt(20)") == 120, "20 kostenpflichtige = 120 gesamt"
    assert pg.evaluate("() => _gesamt(-5)") == 100, (
        "negative Eingabe darf die Gesamtzahl nicht unter die Freigrenze drücken")

    # Der Preistext bekommt jetzt die Lizenzen direkt, nicht die Gesamtzahl
    assert "kein Abo nötig" in pg.evaluate("() => _preisText(0, 'monatlich')"), (
        "bei 0 kostenpflichtigen darf kein Abo verlangt werden")
    text = pg.evaluate("() => _preisText(3, 'monatlich')")
    assert text.startswith("3 Lizenzen"), f"erwartet »3 Lizenzen …«, bekommen: {text}"


def test_lizenzfeld_ist_mit_null_vorbelegt(seite):
    """Wer nichts eintippt, kauft nichts.

    ⚠️ Geprüft wird die RECHNUNG, nicht das HTML-Attribut. Die erste Fassung
    dieses Tests las nur `value` aus dem geladenen Dokument und blieb deshalb
    auch dann grün, als die Vorbelegung testweise wieder auf `frei + 1` stand:
    Die eigentliche Vorbelegung läuft in `licAboLaden()` und damit nur bei
    bestehender Hub-Anbindung. Deshalb steht die Rechnung jetzt in
    `_kaufVorbelegung()`.
    """
    pg = seite("/settings/connect", breite=1280)
    assert pg.evaluate("() => document.getElementById('lic-buy-mailboxes').value") in ("", "0")
    # Unter der Freigrenze: nichts zu kaufen
    assert pg.evaluate("() => _kaufVorbelegung(12, 100)") == 0
    assert pg.evaluate("() => _kaufVorbelegung(100, 100)") == 0
    # Darüber: genau der überziehende Teil
    assert pg.evaluate("() => _kaufVorbelegung(112, 100)") == 12


# ── Massenoperation: erst die Aktion, dann ihr Bereich ───────────────────────

def test_massenoperation_zeigt_erst_nach_der_aktionswahl(seite, monkeypatch):
    """Der Sammelbereich beginnt mit der Frage, WAS geschehen soll.

    Bis v1.8.0 klappte das Aufklappen direkt die Zertifikatsbestellung auf —
    der Sonderfall war das Ganze. Mit der Zwischenebene ist der Aufbau sichtbar:
    Auswahl von Postfächern plus eine Aktion darauf. Heute gibt es genau eine
    Aktion; weitere hängen sich über einen Block `sammel-aktion-<wert>` ein,
    ohne dass hier etwas nachzuziehen wäre.
    """
    # Der Bereich steht unter `{% if smime_users %}` — ohne ein Postfach mit
    # S/MIME gibt es ihn gar nicht. Statt die Elemente im Test selbst zu bauen
    # (das prüfte nur die eigene Annahme) wird ein echter Eintrag gestellt, aus
    # dem die Route ihre Liste bildet.
    import settings_store
    echt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: (
        {"probe-guid": {"smime": True, "sig": True, "primary": "probe@example.org",
                        "known_addresses": ["probe@example.org"]}}
        if k == "MAILBOX_CONFIG" else echt(k, *a, **kw)))

    pg = seite("/smime", breite=1280)
    assert pg.evaluate("() => !!document.getElementById('sammel-box')"), (
        "Der Sammelbereich fehlt — dann prüft dieser Test nichts.")
    pg.evaluate("() => document.getElementById('sammel-box').open = true")
    pg.wait_for_timeout(200)

    sichtbar = lambda: pg.evaluate(
        "() => { const b = document.getElementById('sammel-aktion-zertifikate');"
        "        return !!b && b.offsetParent !== null; }")

    assert not sichtbar(), (
        "Der Bestellbereich steht offen, bevor eine Aktion gewählt wurde — "
        "damit ist die Zwischenebene wirkungslos.")

    pg.select_option("#sammel-aktion", "zertifikate")
    pg.wait_for_timeout(200)
    assert sichtbar(), "Nach der Wahl muss der Bereich erscheinen."

    # Zurück auf „bitte wählen" blendet ihn wieder aus
    pg.select_option("#sammel-aktion", "")
    pg.wait_for_timeout(200)
    assert not sichtbar(), "Ohne gewählte Aktion darf kein Bereich offen stehen."


# ── Hinweis auf nicht erprobte Bereiche ──────────────────────────────────────

# /mailboxes trug bis 01.10.2026 zwei Hinweise (Gruppen, Richtlinien) — beide als
# erprobt entfernt (8b67c5a, bc20213). Der Test lief seither lokal rot; in der CI
# fiel es nicht auf, weil dort kein Playwright installiert ist.
@pytest.mark.parametrize("pfad,anzahl", [("/settings/smime", 1)])
@pytest.mark.parametrize("thema", ["light", "dark"])
def test_preview_hinweis_steht_und_leuchtet_nicht(seite, thema, pfad, anzahl):
    """Gruppen und Richtlinien tragen einen Hinweis — auch im Dunkelmodus lesbar.

    ⚠️ Die Farben stehen als KLASSE in `style.css`, nicht als style-Attribut.
    `darkcheck.py` sieht nur Inline-Styles und hätte den Kasten deshalb nicht
    gemeldet — ohne eigene Regel in `dark-mode.css` leuchtete er im Dunkelmodus
    hell auf. Dieser Test prüft das dort, wo es sichtbar wird: im Browser.
    """
    pg = seite(pfad, breite=1280, thema=thema)
    # Erweiterte Bereiche aufklappen — sonst sind Hinweise darin unsichtbar
    pg.evaluate("""() => document.querySelectorAll('input[id^=adv-cb-]').forEach(cb => {
        if (!cb.checked) { cb.checked = true; cb.dispatchEvent(new Event('change', {bubbles:true})); }
    })""")
    pg.wait_for_timeout(300)
    gefunden = pg.evaluate("() => document.querySelectorAll('.preview-hinweis').length")
    assert gefunden == anzahl, (
        f"{pfad}: erwartet {anzahl} Hinweis(e), gefunden: {gefunden}")

    hell = pg.evaluate("""() => {
      const el = document.querySelector('.preview-hinweis');
      const bg = getComputedStyle(el).backgroundColor;
      const m = bg.match(/\\d+/g).slice(0, 3).map(Number);
      return m.reduce((a, b) => a + b, 0) / 3;      // mittlere Helligkeit
    }""")
    if thema == "dark":
        assert hell < 90, (
            f"Der Hinweis leuchtet im Dunkelmodus (Helligkeit {hell:.0f}) — "
            "es fehlt die Regel in dark-mode.css.")
    else:
        assert hell > 200, f"Im hellen Modus zu dunkel (Helligkeit {hell:.0f})"


# ── Unterreiter: eine Zeile, wischbar ────────────────────────────────────────

@pytest.mark.parametrize("breite", [1280, 393, 320])
def test_unterreiter_bleiben_einzeilig_und_wischbar(seite, breite):
    """Die Unterreiter der Einstellungen stehen in EINER Zeile.

    ⚠️ ANLASS (24.08.2026): Eine Medienabfrage drehte die Grundregel für
    schmale Anzeigen von `nowrap` auf `wrap`. Gemessen: bei 393px drei Zeilen,
    bei 320px vier — die Reiter nahmen mehr Platz ein als der Inhalt darunter.

    Die Grundregel kann das Wischen längst (`overflow-x`, `nowrap`,
    `-webkit-overflow-scrolling`); sie wurde nur überschrieben. Zusätzlich
    braucht es `flex-shrink: 0` auf den Einträgen: Ohne das staucht Flexbox sie
    auf die verfügbare Breite, statt überzulaufen — dann steht zwar alles in
    einer Zeile, aber wischen lässt sich nichts.
    """
    pg = seite("/settings", breite=breite)
    d = pg.evaluate("""() => {
      const ul = document.querySelector('.nav-sub-tabs');
      const tabs = [...ul.querySelectorAll('.nav-sub-tab')];
      return {
        zeilen: new Set(tabs.map(t => Math.round(t.getBoundingClientRect().top))).size,
        wischbar: ul.scrollWidth > ul.clientWidth + 1,
        // Platz, den die Reiter ungestaucht brauchen (Inhaltsbreite, auch wenn
        // sie gestaucht wären). Passt alles in die Zeile, gibt es nichts zu
        // wischen — und das ist richtig.
        bedarf: tabs.reduce((s, t) => s + Math.max(t.getBoundingClientRect().width, t.scrollWidth), 0),
        platz: ul.clientWidth,
        gestaucht: tabs.filter(t => t.scrollWidth > t.clientWidth + 1)
                       .map(t => t.textContent.trim()),
      };
    }""")
    assert d["zeilen"] == 1, (
        f"Bei {breite}px stehen die Unterreiter in {d['zeilen']} Zeilen.")
    # Wischen nur verlangen, wo die Reiter nicht in die Zeile passen. Bis
    # 2026-11-13 hiess es hier pauschal „wischbar“ — lokal lief das bei 1280px
    # grün (breitere Schrift), im ersten CI-Lauf mit Browser nicht: dort passt
    # alles in die Zeile.
    if breite <= 393:
        assert d["bedarf"] > d["platz"], (
            f"Bei {breite}px passen die Reiter in die Zeile — dann prüft dieser "
            "Test das Wischen gar nicht. Breite oder Reiter haben sich geändert.")
    if d["bedarf"] > d["platz"] + 1:
        assert d["wischbar"], (
            f"Bei {breite}px brauchen die Reiter {d['bedarf']:.0f}px bei "
            f"{d['platz']}px Platz, lassen sich aber nicht wischen — sie werden "
            "gestaucht statt überzulaufen.")
    assert not d["gestaucht"], f"Gestauchte Beschriftungen: {d['gestaucht']}"


# ── Live-Protokoll: Auswahl überlebt neue Zeilen ─────────────────────────────

def test_auswahl_im_protokoll_ueberlebt_neue_zeilen(seite):
    """Markierter Text darf nicht verschwinden, sobald eine Zeile eintrifft.

    ⚠️ ANLASS (25.08.2026): Auf dem Telefon liess sich im Live-Protokoll nichts
    markieren. Long-Press setzt die Auswahl — im selben Moment trifft die
    nächste Zeile ein, `autoScroll` springt ans Ende, und die Auswahl ist weg.
    Läuft die markierte Zeile zusätzlich aus dem Puffer, wird ihr Element
    entfernt.

    Geprüft wird der Kern: Besteht eine Auswahl im Protokoll, setzt
    `appendLine()` weder das Scrollen noch das Wegwerfen alter Zeilen fort.
    """
    pg = seite("/log", breite=393)
    if not pg.evaluate("() => typeof appendLine === 'function'"):
        pytest.skip("Protokollseite nicht erreichbar")

    pg.evaluate("""() => {
      for (let i = 0; i < 40; i++) appendLine('2026-08-25 INFO  Zeile ' + i);
      const ziel = pre.children[5];
      const r = document.createRange();
      r.selectNodeContents(ziel);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    }""")
    vorher = pg.evaluate("() => ({text: getSelection().toString(), pos: wrap.scrollTop})")
    assert vorher["text"].strip(), "Testaufbau: es wurde nichts markiert"

    pg.evaluate("() => { for (let i = 0; i < 20; i++) appendLine('INFO  neu ' + i); }")
    pg.wait_for_timeout(150)
    nachher = pg.evaluate("() => ({text: getSelection().toString(), pos: wrap.scrollTop})")

    assert nachher["text"] == vorher["text"], (
        "Die Auswahl ist verlorengegangen, als neue Zeilen eintrafen.")
    assert nachher["pos"] == vorher["pos"], (
        "Es wurde nachgescrollt, obwohl eine Auswahl bestand — auf dem Telefon "
        "reisst genau das die Auswahl weg.")


def test_kopieren_nimmt_die_markierung(seite):
    """Besteht eine Markierung im Kasten, kopiert der Knopf genau sie.

    ⚠️ ANLASS (06.10.2026): iOS legt im Protokollkasten eine echte Auswahl an,
    zeichnet sie aber nicht — kein Menü, kein „Kopieren". Der Knopf ist dort der
    einzige Weg; kopierte er weiter den ganzen Kasten, wäre Markieren nutzlos.
    """
    pg = seite("/log", breite=393)
    if not pg.evaluate("() => typeof appendLine === 'function'"):
        pytest.skip("Protokollseite nicht erreichbar")
    pg.context.grant_permissions(["clipboard-read", "clipboard-write"])
    pg.evaluate("""() => {
      for (let i = 0; i < 30; i++) appendLine('2026-10-06 INFO  Zeile ' + i);
      const r = document.createRange(); r.selectNodeContents(pre.children[pre.children.length - 3]);
      const s = getSelection(); s.removeAllRanges(); s.addRange(r);
    }""")
    pg.wait_for_timeout(200)
    knopf = pg.locator("#btnKopieren")
    assert "Markierung kopieren" in knopf.inner_text(), knopf.inner_text()
    # Wie auf iOS: das Tippen hebt die Auswahl auf, bevor der Klick ankommt.
    pg.evaluate("() => getSelection().removeAllRanges()")
    knopf.click()
    pg.wait_for_timeout(200)
    inhalt = pg.evaluate("() => navigator.clipboard.readText()")
    assert inhalt.strip().endswith("Zeile 27"), f"kopiert wurde: {inhalt[-80:]!r}"
    assert "Zeile 0" not in inhalt, "der ganze Kasten wurde kopiert, nicht die Markierung"


def test_protokoll_diagnose_nur_auf_wunsch(seite):
    """?diag=1 blendet die Messanzeige ein, sonst bleibt sie weg."""
    pg = seite("/log", breite=393)
    if not pg.evaluate("() => typeof appendLine === 'function'"):
        pytest.skip("Protokollseite nicht erreichbar")
    assert pg.evaluate("() => !document.getElementById('log-diag')")
    pg.goto(pg.url.split("?")[0] + "?diag=1")
    pg.wait_for_timeout(500)
    text = pg.evaluate("() => (document.getElementById('log-diag') || {}).textContent || ''")
    assert "Diagnose aktiv" in text and "PRE#logPre" in text, text[:300]


# ── Abwesenheits-Seite: normale Breite, versteckte Knöpfe bleiben versteckt ────

def test_abwesenheit_normal_breit_und_hidden_wirkt(seite):
    """Zwei Befunde aus einem Bildschirmfoto (05.10.2026):
    1. Die Seite lief auf `weit` (1600px) — die Einstellungs-Karten zogen sich
       auseinander, Auswahlfelder über 1200px breit.
    2. „Fehlende Berechtigungen erteilen" stand sichtbar da, obwohl nichts fehlte:
       `hidden` war gesetzt, aber `.btn { display:inline-flex }` überstimmte es.
    Ohne App-Zugangsdaten liefert die Prüfung „nicht prüfbar" — der Knopf muss
    dann ebenfalls verborgen sein (es fehlt nichts Bekanntes)."""
    import settings_store
    vorher = settings_store.get("ABWESENHEIT_AKTIV")
    settings_store.update({"ABWESENHEIT_AKTIV": True})
    try:
        pg = seite("/abwesenheit", breite=1800, hoehe=1000)
        pg.wait_for_timeout(600)
        breite = pg.evaluate("() => document.querySelector('main').getBoundingClientRect().width")
        assert breite <= 960, f"Seite {breite}px breit — erwartet normale Breite (≤960px)"
        sichtbar = pg.evaluate("""() => [...document.querySelectorAll('.ber-erteilen')]
            .filter(b => b.hidden && getComputedStyle(b).display !== 'none').length""")
        assert sichtbar == 0, "ein Knopf mit `hidden` ist trotzdem sichtbar"
        assert pg.evaluate("() => document.querySelectorAll('.ber-erteilen').length") >= 1, \
            "Prüf-Baustein fehlt — der Test prüfte sonst nichts"
    finally:
        settings_store.update({"ABWESENHEIT_AKTIV": vorher})


def test_abwesenheit_filter_und_status_knopf(browser, server, monkeypatch):
    """Freitextfilter mit ×, Knopf „Status = an" (nur eingeschaltete Abwesenheiten),
    und der Knopfzustand überlebt ein Neuladen (localStorage)."""
    import json
    import settings_store
    import sso
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-oberflaeche")
    # Widerruf (sso._noch_gueltig) ist hier nicht Gegenstand — eigene Tests
    # in test_sitzung_widerruf.py; die Testkonten stehen in keiner ADMIN_USERS.
    monkeypatch.setattr(sso, "_noch_gueltig", lambda p: True)
    keks = sso.create_session_cookie("chefin@example.org", role=sso.ROLE_ADMIN)
    daten = {"ts": None, "gesamt": 3, "abwesend": 1, "kein_token": False, "ooo_enabled": True,
             "items": [
                 {"upn": "anna@x.de", "name": "Anna", "status": "alwaysEnabled", "abwesend": True},
                 {"upn": "bert@x.de", "name": "Bert", "status": "disabled"},
                 {"upn": "carla@x.de", "name": "Carla", "status": "scheduled", "abwesend": False},
             ]}
    vorher = settings_store.get("ABWESENHEIT_AKTIV")
    settings_store.update({"ABWESENHEIT_AKTIV": True})
    try:
        ctx = browser.new_context(viewport={"width": 1280, "height": 900})
        ctx.add_cookies([{"name": sso.SESSION_COOKIE, "value": keks, "domain": "127.0.0.1", "path": "/"}])
        ctx.route("**/api/abwesenheit/uebersicht*",
                  lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(daten)))
        pg = ctx.new_page()
        pg.goto(f"{server}/abwesenheit", wait_until="domcontentloaded")
        pg.wait_for_selector("#ab-tbody tr[data-search]")

        def sichtbar():
            return pg.evaluate("""() => [...document.querySelectorAll('#ab-tbody tr[data-search]')]
                .filter(t => t.style.display !== 'none').map(t => t.cells[0].textContent)""")

        assert sichtbar() == ["Anna", "Bert", "Carla"]
        assert not pg.is_visible("#ab-filter-x"), "× ohne Eingabe sichtbar"
        pg.fill("#ab-filter", "bert")
        assert sichtbar() == ["Bert"] and pg.is_visible("#ab-filter-x")
        pg.click("#ab-filter-x")
        assert sichtbar() == ["Anna", "Bert", "Carla"] and pg.input_value("#ab-filter") == ""

        pg.click("#ab-nur-an")
        assert sichtbar() == ["Anna", "Carla"], "„Status = an“ muss aus-Postfächer ausblenden"
        assert pg.get_attribute("#ab-nur-an", "aria-pressed") == "true"
        pg.reload(wait_until="domcontentloaded")
        pg.wait_for_selector("#ab-tbody tr[data-search]")
        assert sichtbar() == ["Anna", "Carla"], "Knopfzustand ging beim Neuladen verloren"
        ctx.close()
    finally:
        settings_store.update({"ABWESENHEIT_AKTIV": vorher})


# /smime fehlt bewusst: Das Feld erscheint dort nur mit S/MIME-Nutzern (die die
# Testumgebung nicht hat). Dass es das Makro nutzt, prüft der Quelltext-Test unten;
# das Makro-Verhalten samt Autofill-Schutz deckt /mailboxes ab.
@pytest.mark.parametrize("pfad,feld", [("/abwesenheit", "ab-filter"),
                                       ("/mailboxes", "mb-filter")])
def test_filterfeld_loeschkreuz(seite, pfad, feld):
    """Gemeinsames Filterfeld (_filterfeld.html): × nur bei Eingabe, Klick leert
    und löst `input` aus (damit die Seite neu filtert)."""
    import settings_store
    vorher = settings_store.get("ABWESENHEIT_AKTIV")
    settings_store.update({"ABWESENHEIT_AKTIV": True})
    try:
        pg = seite(pfad, breite=1280)
        # Die Postfach-Tabelle (samt Filter) erscheint erst nach dem Laden — sichtbar machen.
        pg.evaluate("() => { const w = document.getElementById('mb-table-wrap'); if (w) w.style.display = 'block'; }")
        x = f"#{feld}-x"
        assert not pg.is_visible(x), "× ohne Eingabe sichtbar"
        pg.click(f"#{feld}")          # wie ein Nutzer: Fokus hebt den Autofill-Schutz (readonly) auf
        pg.fill(f"#{feld}", "abc")
        assert pg.is_visible(x), "× erscheint nicht bei Eingabe"
        pg.evaluate(f"() => {{ window._eingaben = 0; document.getElementById('{feld}')"
                    f".addEventListener('input', () => window._eingaben++); }}")
        pg.click(x)
        assert pg.input_value(f"#{feld}") == ""
        assert pg.evaluate("() => window._eingaben") == 1, "Löschen filtert nicht neu"
        assert not pg.is_visible(x)
    finally:
        settings_store.update({"ABWESENHEIT_AKTIV": vorher})


def test_filterfelder_nur_ueber_das_makro():
    """Executable statt Prosa: Ein von Hand gebautes Filterfeld hätte kein ×."""
    import re
    from pathlib import Path
    vorlagen = Path(__file__).resolve().parent.parent / "app" / "webui" / "templates"
    treffer = [f"{p.name}:{i}" for p in vorlagen.glob("*.html") if p.name != "_filterfeld.html"
               for i, z in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
               if re.search(r'<input[^>]*placeholder="Filter', z)]
    assert not treffer, f"Filterfeld am Makro vorbei: {treffer}"


# ── /self: Zeitraum-Prüfung, Hinweise, Meldung nach Status (Testlauf 11.10.2026) ──

def test_self_zeitraum_pruefung_und_meldungen(browser, server):
    """/self bei 393 px mit nachgebildeten APIs:
    * „Bis" vor „Von" → Hinweis sichtbar, Speichern gesperrt; korrigiert → frei.
    * „Aus" → die Vorschau sagt, dass sie zeigt, wie die Antwort AUSSÄHE.
    * Die Erfolgsmeldung nennt den gespeicherten Zustand (vorher hieß es auch
      beim Ausschalten „Abwesenheit … gesetzt").
    * Keine waagerechte Rollleiste.
    """
    import json
    kontext = {"email": "a@x.de", "freigeschaltet": True, "abwesenheit_aktiv": True,
               "darf_vorlagen": False, "waehlbar": {"sig": False, "min": False, "oof": False},
               "oof_template": "K", "sig_template": "S", "standard": {}, "wahl": {}, "listen": {},
               "ooo": {"status": "scheduled", "start": "2026-10-11", "ende": "2026-10-12",
                       "start_zeit": "09:00", "ende_zeit": "17:00", "ganztaegig": True},
               "announce_supported": False,
               "announce": {"an": False, "mode": "anzahl", "x": 3, "privat": True, "extern": False},
               "zugriff": True}
    gesendet = []
    ctx = browser.new_context(viewport={"width": 393, "height": 850})
    ctx.route("**/api/self/context", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(kontext)))
    ctx.route("**/api/self/preview*", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"oof_html": "<p>OOF</p>", "sig_html": "", "banner_html": "",
                         "disclaimer_html": ""})))

    def save(r):
        gesendet.append(json.loads(r.request.post_data))
        r.fulfill(status=200, content_type="application/json", body='{"ok": true}')
    ctx.route("**/api/self/save", save)
    pg = ctx.new_page()
    pg.goto(f"{server}/self", wait_until="domcontentloaded")
    pg.wait_for_selector("#inhalt:not([hidden])", timeout=10000)

    pg.fill("#bis", "2026-10-09")
    pg.dispatch_event("#bis", "change")
    pg.wait_for_timeout(100)
    assert pg.is_visible("#zeitraum-hinweis"), "Hinweis „Ende vor Beginn“ fehlt"
    assert pg.is_disabled("#speichern"), "Speichern muss bei Ende vor Beginn gesperrt sein"
    pg.fill("#bis", "2026-10-12")
    pg.dispatch_event("#bis", "change")
    pg.wait_for_timeout(100)
    assert not pg.is_visible("#zeitraum-hinweis") and not pg.is_disabled("#speichern")

    pg.check("input[name=status][value=disabled]")
    pg.dispatch_event("input[name=status][value=disabled]", "change")
    pg.wait_for_timeout(100)
    assert "aus" in pg.text_content("#vorschau-hinweis").lower()
    pg.click("#speichern")
    pg.wait_for_selector("#speicher-hinweis:not([hidden])", timeout=5000)
    assert gesendet[-1]["oof_status"] == "disabled"
    assert pg.text_content("#speicher-hinweis") == "✓ Gespeichert. Deine Abwesenheit ist aus."

    breit = pg.evaluate("document.documentElement.scrollWidth")
    assert breit <= 393, f"Waagerechte Rollleiste bei 393 px: {breit}"
    ctx.close()


# ── /self: Reiter Abwesenheit · Signatur · Antwort-Signatur (13.10.2026) ─────

def _self_kontext(**ueber):
    k = {"email": "a@x.de", "freigeschaltet": True, "abwesenheit_aktiv": True,
         "oof_mit_signatur": True, "darf_vorlagen": True,
         "waehlbar": {"sig": True, "min": True, "oof": False},
         "oof_template": "K", "sig_template": "S",
         "standard": {"sig": "S", "min": "Kurz", "oof": "K"},
         "wahl": {"sig": "__standard__", "min": "__standard__", "oof": "__standard__"},
         "listen": {"sig": ["S", "Lang"], "min": ["Kurz"], "oof": []},
         "ooo": {"status": "disabled", "start": "", "ende": "", "start_zeit": "09:00",
                 "ende_zeit": "17:00", "ganztaegig": True},
         "announce_supported": False,
         "announce": {"an": False, "mode": "anzahl", "x": 3, "privat": True, "extern": False},
         "zugriff": True}
    k.update(ueber)
    return k


def _self_seite(browser, server, kontext, breite, gesendet):
    import json
    from urllib.parse import urlparse, parse_qs
    ctx = browser.new_context(viewport={"width": breite, "height": 900})
    ctx.route("**/api/self/context", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(kontext)))

    def vorschau(r):
        # Seit v1.12.104 entscheidet der Server, was wirkt: Die Seite schickt
        # die rohe Auswahl (STANDARD/""/Name) und zeigt `neu`/`antwort` fertig an.
        # keep_blank_values: „min=“ heißt „keine Antwort-Signatur“, nicht „fehlt“.
        q = parse_qs(urlparse(r.request.url).query, keep_blank_values=True)
        mn = (q.get("min") or ["__standard__"])[0]
        mn = "Kurz" if mn == "__standard__" else mn
        sig = (q.get("sig") or [""])[0]
        r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "oof_html": "<p>OOF</p>",
            "sig_html": f'<p class="sig-test">SIG {sig}</p>',
            "banner_html": "<p>BAN</p>", "disclaimer_html": "<p>DISC</p>",
            "neu": {"html": f'<p class="sig-test">SIG {sig}</p><p>BAN</p><p>DISC</p>',
                    "signatur_unterdrueckt": False},
            "antwort": {"html": f'<p class="min-test">MIN {mn}</p>' if mn else "",
                        "signatur_unterdrueckt": not mn}}))
    ctx.route("**/api/self/preview*", vorschau)

    def save(r):
        gesendet.append(json.loads(r.request.post_data))
        r.fulfill(status=200, content_type="application/json", body='{"ok": true}')
    ctx.route("**/api/self/save", save)
    pg = ctx.new_page()
    pg.goto(f"{server}/self", wait_until="domcontentloaded")
    pg.wait_for_selector("#inhalt:not([hidden])", timeout=10000)
    return ctx, pg


@pytest.mark.parametrize("breite", [320, 393])
def test_self_reiter_vorschau_und_speichern_je_reiter(browser, server, breite):
    """Drei Reiter in EINER Zeile (Aufgabenbereich ≈ 320 px), je Reiter eine
    Vorschau mit Beispiel-Mail und MUSTER-Stempel nur über dem Blindtext, und
    ein Speichern-Knopf, der NUR seinen Bereich schickt."""
    gesendet = []
    ctx, pg = _self_seite(browser, server, _self_kontext(), breite, gesendet)
    pg.evaluate("localStorage.removeItem('self_reiter')")

    leiste = pg.locator("#reiter-leiste")
    assert leiste.is_visible()
    hoehen = pg.evaluate("[...document.querySelectorAll('#reiter-leiste button')]"
                         ".map(b => Math.round(b.getBoundingClientRect().top))")
    assert len(hoehen) == 3 and len(set(hoehen)) == 1, f"Reiter brechen um: {hoehen}"
    assert pg.is_visible("#p-abw") and not pg.is_visible("#p-sig")

    # Reiter Signatur
    pg.click("#r-sig")
    assert pg.is_visible("#p-sig") and not pg.is_visible("#p-abw")
    pg.frame_locator("#sig-vorschau iframe").locator(".sig-test").wait_for(timeout=5000)
    stempel = pg.evaluate("getComputedStyle(document.querySelector('#p-sig .muster'), '::after').content")
    assert "MUSTER" in stempel
    assert pg.evaluate("!document.querySelector('#sig-vorschau').closest('.muster')"), \
        "Stempel liegt über der Signatur"
    assert pg.is_visible("#sig-auch-oof"), "Hinweis „auch unter der Abwesenheitsnotiz“ fehlt"
    assert pg.is_disabled("#sig-speichern"), "Knopf ohne Änderung muss grau sein"
    pg.select_option("#sig-sel", "Lang")
    _vorschau_enthaelt(pg, "#sig-vorschau", "Lang")
    assert not pg.is_disabled("#sig-speichern")
    assert "Noch nicht gespeichert" in pg.text_content("#sig-hinweis")
    pg.select_option("#sig-sel", "__standard__")          # zurück zum gespeicherten Stand
    assert pg.is_disabled("#sig-speichern") and not pg.is_visible("#sig-hinweis")
    pg.select_option("#sig-sel", "Lang")
    pg.click("#sig-speichern")
    pg.wait_for_function("document.querySelector('#sig-hinweis').textContent.startsWith('✓')")
    assert gesendet[-1] == {"bereich": "signatur", "wahl": {"sig": "Lang"}}
    assert pg.is_disabled("#sig-speichern")

    # Reiter Antwort-Signatur: „— keine —“
    pg.click("#r-min")
    pg.frame_locator("#min-vorschau iframe").locator(".min-test").wait_for(timeout=5000)
    pg.select_option("#min-sel", "")
    pg.wait_for_selector("#min-vorschau .keine-sig", timeout=5000)
    pg.click("#min-speichern")
    pg.wait_for_function("document.querySelector('#min-hinweis').textContent.startsWith('✓')")
    assert gesendet[-1] == {"bereich": "antwort", "wahl": {"min": ""}}
    assert "keine Signatur" in pg.text_content("#min-hinweis")

    # Abwesenheit schickt ihren Bereich, ohne die Signaturwahl
    pg.click("#r-abw")
    pg.click("#speichern")
    pg.wait_for_selector("#speicher-hinweis:not([hidden])", timeout=5000)
    assert gesendet[-1]["bereich"] == "abwesenheit" and gesendet[-1]["wahl"] == {}

    breit = pg.evaluate("document.documentElement.scrollWidth")
    assert breit <= breite, f"Waagerechte Rollleiste bei {breite} px: {breit}"

    # Gemerkter Reiter nach dem Neuladen
    pg.click("#r-min")
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_selector("#inhalt:not([hidden])", timeout=10000)
    assert pg.is_visible("#p-min")
    ctx.close()


# Vorschauen auf /self stehen seit v1.12.132 in einem abgeschotteten Rahmen
# (sig_preview.js): Text der Box samt Inhalt ihrer iframes.
_VORSCHAU_TEXT = ("(sel) => { const b = document.querySelector(sel); if (!b) return '';"
                  " let t = b.textContent; b.querySelectorAll('iframe').forEach(f => {"
                  " try { t += f.contentDocument.body.textContent; } catch (e) {} }); return t; }")


def _vorschau_enthaelt(pg, sel, text, timeout=5000):
    pg.wait_for_function(f"({_VORSCHAU_TEXT})({sel!r}).includes({text!r})", timeout=timeout)


@pytest.mark.parametrize("mit_sig,mit_ban,mit_disc",
                         [(True, False, True), (False, True, False), (False, False, False)])
def test_self_abwesenheits_vorschau_wie_in_exchange(browser, server, mit_sig, mit_ban, mit_disc):
    """Die Vorschau der Abwesenheitsnotiz trägt Signatur, Banner und Disclaimer
    nur, wenn das Gateway sie anhängt (OOO_APPEND_SIGNATURE/_BANNER/_DISCLAIMER)
    — bis v1.9.161 immer alles, danach bis v1.10.60 nie den Disclaimer."""
    ctx, pg = _self_seite(browser, server, _self_kontext(
        oof_mit_signatur=mit_sig, oof_mit_banner=mit_ban, oof_mit_disclaimer=mit_disc), 393, [])
    _vorschau_enthaelt(pg, "#vorschau", "OOF")
    text = pg.evaluate(f"({_VORSCHAU_TEXT})('#vorschau')")
    assert ("SIG" in text) is mit_sig
    assert ("BAN" in text) is mit_ban
    assert ("DISC" in text) is mit_disc
    assert pg.is_visible("#abw-mit-sig") is mit_sig
    ctx.close()


def test_self_nur_ein_reiter_ohne_leiste(browser, server):
    """Darf der Nutzer nichts wählen und ist die Abwesenheit aus, bleibt nur die
    Signatur zur Ansicht — ohne Reiterleiste, ohne Auswahl, ohne Knopf-Wirkung."""
    gesendet = []
    k = _self_kontext(abwesenheit_aktiv=False, oof_mit_signatur=False,
                      waehlbar={"sig": False, "min": False, "oof": False})
    ctx, pg = _self_seite(browser, server, k, 393, gesendet)
    assert not pg.is_visible("#reiter-leiste")
    assert pg.is_visible("#p-sig") and not pg.is_visible("#sig-block")
    assert pg.is_visible("#sig-fest") and not pg.is_visible("#sig-auch-oof")
    pg.frame_locator("#sig-vorschau iframe").locator(".sig-test").wait_for(timeout=5000)
    ctx.close()


def test_self_antwort_signatur_als_reine_ansicht(browser, server):
    """Weder wählbar noch festgeschrieben, aber eine Antwort-Signatur wirkt:
    Reiter als Ansicht (Entscheidung 19.11.2026) — ohne Auswahl und Knopf, mit
    Hinweis. Bis v1.12.107 blieb er verborgen."""
    gesendet = []
    k = _self_kontext(waehlbar={"sig": True, "min": False, "oof": False}, min_template="Kurz")
    ctx, pg = _self_seite(browser, server, k, 393, gesendet)
    assert pg.is_visible("#r-min")
    pg.click("#r-min")
    assert pg.is_visible("#p-min") and not pg.is_visible("#min-block")
    assert not pg.is_visible("#min-speichern")
    assert "Von deiner Organisation vorgegeben: Kurz" in pg.text_content("#min-org")
    assert not pg.is_visible("#min-keine-hinweis")      # verweist auf eine Auswahl, die fehlt
    ctx.close()


def test_self_ohne_antwort_signatur_kein_ansichts_reiter(browser, server):
    gesendet = []
    k = _self_kontext(waehlbar={"sig": True, "min": False, "oof": False}, min_template="")
    ctx, pg = _self_seite(browser, server, k, 393, gesendet)
    assert not pg.is_visible("#r-min")
    ctx.close()


# ── Self-Service im Aufgabenbereich des Add-ins ──────────────────────────────
#
# Anlass (13.10.2026): Der Lese-Knopf öffnete /self?et= im Aufgabenbereich von
# Outlook im Web; der Bereich zeigte nur das Sperrsymbol. Die Seite startete per
# 1,5-s-Zeitgeber vor Office.onReady, imOffice() war beim 401 noch falsch, und
# anmelden() leitete IM RAHMEN zu /auth/login um — die Seite trägt
# X-Frame-Options: DENY. Hier wird office.js durch einen Nachbau ersetzt, der
# sich erst nach 3 s meldet (langsamer als der alte Zeitgeber).

_OFFICE_LANGSAM = """
window.__dialoge = [];
window.Office = { AsyncResultStatus: { Succeeded: 'succeeded' },
  EventType: { DialogMessageReceived: 'm', DialogEventReceived: 'e' },
  onReady: function (cb) { setTimeout(function () {
    if (%(mit_dialog)s) {
      Office.context = { mailbox: {}, officeTheme: %(thema)s, ui: { displayDialogAsync: function (url, opt, cb) {
        window.__dialoge.push(url);
        // Erster Versuch wie „Ignorieren“ im Office-Hinweis (Fehler 12009).
        if (%(ablehnen)s && window.__dialoge.length === 1 && cb) {
          cb({ status: 'failed', error: { code: 12009, message: 'ignoriert' } });
        }
      } } };
    }
    cb({ host: %(host)s });
  }, %(verzoegerung)d); } };
"""


def _office_nachbau(ctx, mit_dialog=True, host="'Outlook'", verzoegerung=3000, ablehnen=False,
                    thema="undefined"):
    js = _OFFICE_LANGSAM % {"mit_dialog": "true" if mit_dialog else "false", "thema": thema,
                            "host": host, "verzoegerung": verzoegerung,
                            "ablehnen": "true" if ablehnen else "false"}
    ctx.route("https://appsforoffice.microsoft.com/**",
              lambda r: r.fulfill(status=200, content_type="application/javascript", body=js))


def _im_rahmen(browser, server, **nachbau):
    ctx = browser.new_context()
    _office_nachbau(ctx, **nachbau)
    pg = ctx.new_page()
    pg.set_content(f'<iframe id="r" src="{server}/self?et=" width="320" height="440"></iframe>')
    pg.wait_for_function("() => window.frames.length === 1")
    rahmen = next(f for f in pg.frames if "/self" in f.url or "/auth" in f.url)
    return ctx, pg, rahmen


def test_aufgabenbereich_wartet_auf_office_und_oeffnet_den_dialog(browser, server):
    ctx, pg, rahmen = _im_rahmen(browser, server)
    try:
        rahmen.wait_for_function("() => window.__dialoge && window.__dialoge.length > 0", timeout=8000)
        assert "/auth/login/microsoft" in rahmen.evaluate("() => window.__dialoge[0]")
        assert "/auth/login?" not in rahmen.url, rahmen.url
    finally:
        ctx.close()


def test_aufgabenbereich_nach_ignorieren_bietet_erneuten_versuch(browser, server):
    """„Ignorieren“ (12009): Knopf statt „Seite neu laden“, sein Klick öffnet den Dialog erneut."""
    ctx, pg, rahmen = _im_rahmen(browser, server, ablehnen=True)
    try:
        rahmen.wait_for_selector("#fehler:not([hidden])", timeout=8000)
        assert "Allow" in rahmen.inner_text("#fehler")
        assert rahmen.is_visible("#tab-anmelden-btn")
        rahmen.click("#tab-anmelden-btn")
        rahmen.wait_for_function("() => window.__dialoge.length === 2", timeout=3000)
        assert "/auth/login/microsoft" in rahmen.evaluate("() => window.__dialoge[1]")
    finally:
        ctx.close()


def test_aufgabenbereich_ohne_dialog_leitet_nicht_um(browser, server):
    """Fehlt die Dialog-Schnittstelle, Meldung statt Weiterleitung in den Rahmen."""
    ctx, pg, rahmen = _im_rahmen(browser, server, mit_dialog=False)
    try:
        rahmen.wait_for_selector("#fehler:not([hidden])", timeout=8000)
        assert "kein Anmeldefenster" in rahmen.inner_text("#fehler")
        assert rahmen.is_visible("#im-browser")
        assert "/self" in rahmen.url and "/auth/login" not in rahmen.url, rahmen.url
    finally:
        ctx.close()


def test_aufgabenbereich_meldet_sich_wenn_office_nie_antwortet(browser, server):
    ctx, pg, rahmen = _im_rahmen(browser, server, verzoegerung=10 ** 9)
    try:
        rahmen.wait_for_selector("#fehler:not([hidden])", timeout=15000)
        assert "nicht initialisiert" in rahmen.inner_text("#fehler")
        assert "/auth/login" not in rahmen.url
    finally:
        ctx.close()


def test_eigenes_fenster_leitet_weiter_zur_anmeldung(browser, server):
    """Gegenstück: Ohne Rahmen bleibt der gewohnte Weg über /auth/login."""
    ctx = browser.new_context()
    _office_nachbau(ctx, mit_dialog=False, host="null", verzoegerung=10)
    pg = ctx.new_page()
    try:
        pg.goto(f"{server}/self", wait_until="domcontentloaded")
        pg.wait_for_url("**/auth/login?next=/self", timeout=8000)
    finally:
        ctx.close()


# ── Tab: stille Anmeldung per NAA ────────────────────────────────────────────
#
# Anlass (17.10.2026): Im Tab auf dem iPhone bat die Anmeldung um den
# Benutzernamen, statt das Konto des Authenticators zu nehmen —
# `authentication.authenticate` ist eine Webanmeldung ohne Broker. Jetzt
# versucht der Tab zuerst still NAA; das Fenster bleibt der Rückfall.
# TeamsJS und MSAL werden hier nachgebaut.

_TEAMS_NACHBAU = """
window.__fenster = [];
window.microsoftTeams = { app: { initialize: function () { return Promise.resolve(); },
    getContext: function () { return Promise.resolve({ app: { theme: window.__teamsThema } }); },
    registerOnThemeChangeHandler: function (h) { window.__themaWechsel = h; } },
  authentication: { authenticate: function (o) { window.__fenster.push(o.url);
    return new Promise(function () {}); } } };
"""

_MSAL_NACHBAU = """
window.__naa = [];
window.msal = { createNestablePublicClientApplication: function (konf) {
  window.__naa.push(konf.auth.clientId);
  return Promise.resolve({ acquireTokenSilent: function (a) {
    window.__naa.push(a.scopes[0]);
    return %(still)s ? Promise.resolve({ accessToken: 'ZUGRIFF' })
                     : Promise.reject(new Error('InteractionRequired'));
  } });
} };
"""

_TAB_KONTEXT = {"email": "a@x.de", "freigeschaltet": True, "abwesenheit_aktiv": True,
                "darf_vorlagen": False, "waehlbar": {"sig": False, "min": False, "oof": False},
                "oof_template": "K", "sig_template": "S", "standard": {}, "wahl": {}, "listen": {},
                "ooo": {"status": "scheduled", "start": "2026-10-11", "ende": "2026-10-12",
                        "start_zeit": "09:00", "ende_zeit": "17:00", "ganztaegig": False},
                "announce_supported": False,
                "announce": {"an": False, "mode": "anzahl", "x": 3, "privat": True, "extern": False},
                "zugriff": True}


def _tab(browser, server, monkeypatch, still=True, sitzung=200, system="light",
         teams_thema=None, wahl=None):
    import json
    import settings_store
    echt = settings_store.get
    fest = {"BOOTSTRAP_CLIENT_ID": "CID", "TENANT_ID": "TID"}
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: fest[k] if k in fest else echt(k, *a, **kw))
    from webui.routen import addin
    tausch, kontext = [], []
    ctx = browser.new_context(viewport={"width": 393, "height": 850}, color_scheme=system)
    ctx.add_init_script(f"window.__teamsThema = {json.dumps(teams_thema)};"
                        + (f"localStorage.setItem('exo-theme', {json.dumps(wahl)});" if wahl else ""))
    # TeamsJS trägt eine Integritätsprüfung — ein Nachbau unter seiner Adresse
    # würde blockiert. Deshalb vorab einsetzen und das CDN leer antworten lassen.
    ctx.add_init_script(_TEAMS_NACHBAU)
    ctx.route(addin.TEAMSJS_URL, lambda r: r.fulfill(status=404, body=""))
    ctx.route("**/static/vendor/msal-browser-*.js", lambda r: r.fulfill(
        status=200, content_type="application/javascript",
        body=_MSAL_NACHBAU % {"still": "true" if still else "false"}))

    def tauschen(r):
        tausch.append(r.request.headers.get("authorization"))
        r.fulfill(status=sitzung, content_type="application/json",
                  body=json.dumps({"token": "SITZUNG"} if sitzung == 200 else {"detail": "x"}))
    ctx.route("**/addin/tab/sitzung*", tauschen)

    def ctx_abruf(r):
        kopf = r.request.headers.get("x-addin-session")
        kontext.append(kopf)
        if kopf == "SITZUNG":
            r.fulfill(status=200, content_type="application/json", body=json.dumps(_TAB_KONTEXT))
        else:
            r.fulfill(status=401, content_type="application/json", body='{"detail":"x"}')
    ctx.route("**/api/self/context", ctx_abruf)
    ctx.route("**/api/self/preview*", lambda r: r.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"oof_html": "", "sig_html": "", "banner_html": "", "disclaimer_html": ""})))
    pg = ctx.new_page()
    pg.goto(f"{server}/self?host=teams", wait_until="domcontentloaded")
    return ctx, pg, tausch, kontext


def test_tab_meldet_sich_still_per_naa_an(browser, server, monkeypatch):
    ctx, pg, tausch, kontext = _tab(browser, server, monkeypatch)
    try:
        pg.wait_for_selector("#inhalt:not([hidden])", timeout=8000)
        assert pg.evaluate("() => window.__naa") == ["CID", "api://CID/access_as_user"]
        assert tausch == ["Bearer ZUGRIFF"]
        assert kontext == [None, "SITZUNG"]                    # 401, dann mit Sitzung
        assert not pg.is_visible("#tab-anmelden")
        assert pg.evaluate("() => window.__fenster") == []      # kein Fenster
        assert pg.evaluate("() => localStorage.getItem('exo_addin_session')") == "SITZUNG"
    finally:
        ctx.close()


@pytest.mark.parametrize("still,sitzung", [(False, 200), (True, 403), (True, 401)])
def test_tab_ohne_naa_bietet_das_fenster(browser, server, monkeypatch, still, sitzung):
    """Scheitert NAA (kein stilles Token, Gateway lehnt ab): Knopf, Klick → Fenster.
    Und nur EIN Versuch — sonst drehte sich die Seite im Kreis."""
    ctx, pg, tausch, kontext = _tab(browser, server, monkeypatch, still=still, sitzung=sitzung)
    try:
        pg.wait_for_selector("#tab-anmelden:not([hidden])", timeout=8000)
        assert pg.evaluate("() => window.__fenster") == []      # erst auf Klick
        pg.wait_for_timeout(300)
        assert len(tausch) == (1 if still else 0)
        assert kontext == [None]
        pg.click("#tab-anmelden-btn")
        pg.wait_for_function("() => window.__fenster.length === 1", timeout=3000)
        assert "/auth/login/microsoft" in pg.evaluate("() => window.__fenster[0]")
    finally:
        ctx.close()



# ── Self-Service: Hell/Dunkel ────────────────────────────────────────────────
#
# Rangfolge: eigene Wahl (Knopf, „exo-theme“ wie in der Verwaltung) → Design
# von Teams/Outlook → System/Browser. Die Vorschau bleibt hell.

_FLAECHEN_PRUEFUNG = """() => {
  const lum = c => { const m = c.match(/\\d+(\\.\\d+)?/g); if (!m || (m.length > 3 && +m[3] === 0)) return null;
                     return (+m[0] * 299 + +m[1] * 587 + +m[2] * 114) / 1000; };
  const funde = [];
  for (const e of document.querySelectorAll('body *')) {
    if (e.closest('.vorschau') || !e.offsetParent) continue;
    const st = getComputedStyle(e), g = lum(st.backgroundColor);
    if (g !== null && g > 200) funde.push('hell: ' + (e.id || e.className || e.tagName));
    const hatText = [...e.childNodes].some(n => n.nodeType === 3 && n.textContent.trim());
    if (hatText && lum(st.color) !== null && lum(st.color) < 110) funde.push('dunkler Text: ' + (e.id || e.className || e.tagName));
  }
  return funde;
}"""


def _thema(pg):
    return pg.evaluate("() => document.documentElement.getAttribute('data-theme')")


@pytest.mark.parametrize("breite", [393, 320])
def test_self_folgt_dem_system_und_die_vorschau_bleibt_hell(browser, server, monkeypatch, breite):
    ctx, pg, _t, _k = _tab(browser, server, monkeypatch, system="dark")
    try:
        pg.set_viewport_size({"width": breite, "height": 850})
        pg.wait_for_selector("#inhalt:not([hidden])", timeout=8000)
        assert _thema(pg) == "dark"
        assert pg.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(15, 23, 42)"
        assert pg.evaluate("() => getComputedStyle(document.querySelector('.vorschau')).backgroundColor") \
            == "rgb(255, 255, 255)"
        pg.evaluate("() => { document.getElementById('nur-anzeige').hidden = false; }")
        assert pg.evaluate("() => [...document.querySelectorAll('input[type=date], select')]"
                           ".filter(e => e.offsetParent).length") >= 2   # Felder sind im Bild
        assert pg.evaluate(_FLAECHEN_PRUEFUNG) == []
        assert pg.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
        assert pg.inner_text("#thema-knopf") == "☀"
    finally:
        ctx.close()


def test_self_hell_hat_keine_dunklen_reste(browser, server, monkeypatch):
    ctx, pg, _t, _k = _tab(browser, server, monkeypatch, system="light")
    try:
        pg.wait_for_selector("#inhalt:not([hidden])", timeout=8000)
        assert _thema(pg) == "light"
        assert pg.evaluate("() => getComputedStyle(document.body).backgroundColor") == "rgb(248, 250, 252)"
    finally:
        ctx.close()


def test_self_teams_design_geht_vor_dem_system_und_folgt_dem_wechsel(browser, server, monkeypatch):
    ctx, pg, _t, _k = _tab(browser, server, monkeypatch, system="light", teams_thema="dark")
    try:
        pg.wait_for_selector("#inhalt:not([hidden])", timeout=8000)
        pg.wait_for_function("() => document.documentElement.getAttribute('data-theme') === 'dark'", timeout=3000)
        pg.evaluate("() => window.__themaWechsel('default')")
        assert _thema(pg) == "light"
        pg.evaluate("() => window.__themaWechsel('contrast')")
        assert _thema(pg) == "dark"
    finally:
        ctx.close()


def test_self_eigene_wahl_geht_vor_teams_und_knopf_merkt_sie(browser, server, monkeypatch):
    ctx, pg, _t, _k = _tab(browser, server, monkeypatch, system="dark", teams_thema="dark", wahl="light")
    try:
        pg.wait_for_selector("#inhalt:not([hidden])", timeout=8000)
        pg.wait_for_timeout(300)
        assert _thema(pg) == "light"
        pg.evaluate("() => window.__themaWechsel('dark')")
        assert _thema(pg) == "light"
        pg.click("#thema-knopf")
        assert _thema(pg) == "dark"
        assert pg.evaluate("() => localStorage.getItem('exo-theme')") == "dark"
    finally:
        ctx.close()


@pytest.mark.parametrize("farbe,erwartet", [("'#1f1f1f'", "dark"), ("'#ffffff'", "light")])
def test_self_im_aufgabenbereich_folgt_dem_outlook_design(browser, server, farbe, erwartet):
    ctx = browser.new_context(color_scheme="light" if erwartet == "dark" else "dark")
    _office_nachbau(ctx, verzoegerung=10, thema="{ bodyBackgroundColor: %s }" % farbe)
    pg = ctx.new_page()
    try:
        pg.set_content(f'<iframe id="r" src="{server}/self?et=" width="320" height="440"></iframe>')
        pg.wait_for_function("() => window.frames.length === 1")
        rahmen = next(f for f in pg.frames if "/self" in f.url)
        rahmen.wait_for_function(
            f"() => document.documentElement.getAttribute('data-theme') === '{erwartet}'", timeout=5000)
    finally:
        ctx.close()


def test_postfachfilter_signatur_und_smime(seite):
    """Filter „Signatur“ und „S/MIME“ (v1.9.175): nach dem Haken in der Zeile,
    zusammen mit Text- und Typfilter, Zähler „x von y“."""
    pg = seite("/mailboxes", breite=1280)
    # Die Seite lädt beim Öffnen ihre eigene (im Test leere) Postfachliste.
    # Kam die Antwort NACH dem Zeichnen unten, überschrieb sie die Mustertabelle
    # — zeitabhängig rot, auf langsamen Rechnern meistens.
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      document.getElementById('mb-table-wrap').style.display = 'block';
      renderMailboxTable([
        {email: 'a@contoso.example', name: 'A', type: 'user', sig: true,  smime: true},
        {email: 'b@contoso.example', name: 'B', type: 'user', sig: true,  smime: false},
        {email: 'c@contoso.example', name: 'C', type: 'shared', sig: false, smime: false},
      ]); }""")

    def sichtbar():
        return pg.evaluate("""() => Array.from(document.querySelectorAll('#mb-tbody tr'))
            .filter(tr => tr.style.display !== 'none').map(tr => tr.dataset.search.split(' ')[0])""")

    assert sichtbar() == ["a@contoso.example", "b@contoso.example", "c@contoso.example"]
    # Seit v1.12.110 stehen die Filter im aufklappbaren Bereich „Filter“.
    pg.click("#mb-filter-knopf")
    pg.select_option("#mb-sig-filter", "an")
    assert sichtbar() == ["a@contoso.example", "b@contoso.example"]
    assert pg.inner_text("#mb-filter-count") == "2 von 3 Postfächern"
    assert pg.inner_text("#mb-filter-knopf").startswith("Filter (1)")
    pg.select_option("#mb-smime-filter", "an")
    assert sichtbar() == ["a@contoso.example"]
    pg.select_option("#mb-sig-filter", "aus")
    pg.select_option("#mb-smime-filter", "aus")
    assert sichtbar() == ["c@contoso.example"]
    # Ungespeicherter Haken zählt beim nächsten Filtern
    pg.select_option("#mb-smime-filter", "")
    pg.evaluate("() => { document.querySelectorAll('#mb-tbody tr')[2]"
                ".querySelector('.mb-sig').checked = true; filterMailboxes(); }")
    assert sichtbar() == []




# Postfachliste mit einer festgelegten Zeile (Gruppe, Standardrichtlinie, Add-in
# aus zwei Gruppen) — für Höhe und Chips.
_MB_FEST = """[
  {email: 'a@contoso.example', name: 'A', type: 'user', sig: true, smime: false, template: 'x',
   standard: {sig: 'default', min: 'kurz', addin: ['A', 'B']},
   fest: {sig: 'Vertrieb', min: '', addin: 'Vertrieb, Technik'}},
  {email: 'b@contoso.example', name: 'B', type: 'user', sig: true, smime: false}]"""


def _postfaecher_zeichnen(pg):
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      // Schlechtester Fall: Lizenzzähler sichtbar.
      document.getElementById('fairuse-counter').style.display = '';
      document.getElementById('mb-table-wrap').style.display = '';
      renderMailboxTable(%s); }""" % _MB_FEST)


@pytest.mark.parametrize("breite,hoehe", [(393, 800), (320, 700)])
def test_erste_postfachzeile_ohne_rollen_sichtbar(seite, breite, hoehe):
    """v1.12.110: Über der Liste standen Erklärtext, sechs Filter und sechs
    Sammelaktionen offen — bei 393 px lag die erste Postfachzeile unter dem
    Bildschirmrand. Jetzt eine Leiste (Suche, „Filter“, „Aktionen“)."""
    pg = seite("/mailboxes", breite=breite, hoehe=hoehe)
    _postfaecher_zeichnen(pg)
    r = pg.evaluate("""() => {
      const tr = document.querySelector('#mb-tbody tr');
      const leiste = document.querySelector('.mb-werkzeug');
      const kinder = Array.from(leiste.children).filter(e => e.offsetParent !== null);
      return {oben: tr.getBoundingClientRect().top + scrollY,
              breit: document.documentElement.scrollWidth,
              // Eine Zeile = jedes Element beginnt oberhalb der Unterkante des
              // obersten. Gleiche Oberkante zu verlangen wäre falsch: Suchfeld
              // und Knöpfe sind verschieden hoch und mittig ausgerichtet.
              zeilen: (() => { const r = kinder.map(e => e.getBoundingClientRect());
                               const unten = Math.min(...r.map(x => x.bottom));
                               return r.some(x => x.top >= unten) ? 2 : 1; })()}; }""")
    assert r["oben"] < hoehe - 100, f"erste Postfachzeile erst bei {r['oben']:.0f} px"
    assert r["breit"] <= breite, f"waagerechte Rollleiste: {r['breit']} px"
    assert r["zeilen"] == 1, "Suche, Filter und Aktionen brechen um"
    # Aufgeklappt sind alle Funktionen erreichbar; immer nur ein Bereich offen.
    pg.click("#mb-aktionen-knopf")
    assert pg.is_visible("#regel-probe-alle-btn") and pg.is_visible("#mb-massen-vorlage")
    pg.click("#mb-filter-knopf")
    assert pg.is_visible("#mb-ruhend-filter") and not pg.is_visible("#regel-probe-alle-btn")
    assert pg.get_attribute("#mb-filter-knopf", "aria-expanded") == "true"
    assert pg.evaluate("() => document.documentElement.scrollWidth") <= breite


@pytest.mark.parametrize("thema", ["light", "dark"])
def test_festgelegte_auswahl_zeigt_nur_die_quelle_als_farbchip(seite, thema):
    pg = seite("/mailboxes", breite=1280, thema=thema)
    _postfaecher_zeichnen(pg)
    r = pg.evaluate("""() => {
      const tr = document.querySelector('#mb-tbody tr');
      const zelle = sel => tr.querySelector(sel).closest('td');
      const hell = el => { const m = getComputedStyle(el).backgroundColor.match(/\\d+/g).slice(0, 3).map(Number);
                           return m.reduce((a, b) => a + b, 0) / 3; };
      const chips = sel => Array.from(zelle(sel).querySelectorAll('.wahl-fest .gruppen-chip'))
        .map(c => ({text: c.textContent, farbe: c.dataset.gruppenfarbe, hell: hell(c),
                    bg: getComputedStyle(c).backgroundColor}));
      const s = tr.querySelector('select.mb-template');
      return {option: s.options[s.selectedIndex].text, gesperrt: s.disabled, titel: s.title,
              sig: chips('select.mb-template'), min: chips('select.mb-min-sig'),
              addin: chips('select.mb-addin-modus'), soll: gruppenFarbe('Vertrieb'),
              ruht: zelle('select.mb-template').querySelector('.wahl-ruht').textContent}; }""")
    assert r["option"] == "Standard" and r["gesperrt"] is True
    assert r["titel"] == "Festgelegt durch Gruppe „Vertrieb“"
    assert [c["text"] for c in r["sig"]] == ["Vertrieb"]
    assert r["sig"][0]["farbe"] == str(r["soll"])
    assert [(c["text"], c["farbe"]) for c in r["min"]] == [("Standardrichtlinie", "standard")]
    assert [c["text"] for c in r["addin"]] == ["Vertrieb", "Technik"]
    assert r["addin"][0]["farbe"] == r["sig"][0]["farbe"], "gleiche Gruppe, gleiche Farbe"
    assert r["ruht"].startswith("eigene Wahl „x“ ruht")
    for c in r["sig"] + r["min"] + r["addin"]:
        if thema == "dark":
            assert c["hell"] < 90, f"Chip leuchtet im Dunkelmodus: {c}"
        else:
            assert c["hell"] > 200, f"Chip ohne helle Farbe: {c}"
    # Gruppe und Standardrichtlinie sind unterscheidbar.
    assert r["sig"][0]["bg"] != r["min"][0]["bg"]


def test_sammellauf_anzeige_mit_echten_zustaenden(seite):
    """Das Backend liefert 'laeuft'/'fertig' klein. Bis v1.10.29 verglich die
    Anzeige mit 'LAEUFT': Text roh („laeuft“), keine Knöpfe, KEIN Nachladen —
    der Betreiber sah „0 von 1 erledigt“ bis zum Neuladen der Seite."""
    import sammelbestellung as sb
    pg = seite("/smime", breite=1280)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      if (!document.getElementById('sammel-lauf')) {
        const d = document.createElement('div'); d.id = 'sammel-lauf'; d.className = 'sammel-lauf';
        document.body.appendChild(d);
      }
      window.__lauf = null;
      const echt = getJSON;
      window.getJSON = async (url) => url.includes('/api/smime/sammel/lauf')
        ? {ok: true, lauf: window.__lauf} : echt(url);
    }""")

    def zeige(lauf):
        pg.evaluate("l => { window.__lauf = l; return sammelLaufPollen(); }", lauf)
        return (pg.inner_text("#sammel-lauf"),
                pg.evaluate("() => document.getElementById('sammel-lauf').dataset.zustand"),
                pg.evaluate("() => _sammelTimer !== null && _sammelTimer !== undefined"))

    text, zustand, timer = zeige({"status": sb.LAEUFT, "gesamt": 2, "aktuell": "b@x.de", "meldung": "",
                                  "erledigt": [{"email": "a@x.de", "ok": True, "phase": "waiting_challenge"}]})
    assert "Läuft" in text and "laeuft" not in text
    assert "1 von 2 aufgegeben" in text and "Wird bestellt: b@x.de" in text
    assert "Warte auf Challenge" in text
    assert "Abbrechen" in text and zustand == "LAEUFT" and timer

    pg.evaluate("() => { clearTimeout(_sammelTimer); _sammelTimer = null; }")
    text, zustand, timer = zeige({"status": sb.FERTIG, "gesamt": 1, "aktuell": "", "meldung": "1 von 1 bestellt.",
                                  "erledigt": [{"email": "a@x.de", "ok": True, "phase": "validating"}]})
    assert "Abgeschlossen" in text and zustand == "FERTIG"
    assert timer, "Bestellung noch unterwegs — die Anzeige muss weiter nachfragen"

    pg.evaluate("() => { clearTimeout(_sammelTimer); _sammelTimer = null; }")
    # Ausgestellt, ohne dass die Seite den Lauf vorher gesehen hat: kein Neuladen
    text, zustand, timer = zeige({"status": sb.FERTIG, "gesamt": 1, "aktuell": "", "meldung": "",
                                  "erledigt": [{"email": "neu@x.de", "ok": True, "phase": "ausgestellt"}]})
    assert "Zertifikat ausgestellt" in text and not timer
    assert "Seite wird aktualisiert" not in text

    pg.evaluate("() => { clearTimeout(_sammelTimer); _sammelTimer = null; }")
    text, zustand, timer = zeige({"status": sb.PAUSIERT, "gesamt": 2, "aktuell": "", "meldung": "Guthaben erschöpft.",
                                  "erledigt": []})
    assert "Angehalten" in text and "Fortsetzen" in text and zustand == "PAUSIERT"



def test_sammellauf_laedt_nach_ausstellung_neu(seite):
    """Sah die Seite eine Bestellung unterwegs und dann ausgestellt, lädt sie
    neu — sonst zeigen die Zeilen darunter weiter „kein Zertifikat“."""
    import sammelbestellung as sb
    pg = seite("/smime", breite=1280)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      if (!document.getElementById('sammel-lauf')) {
        const d = document.createElement('div'); d.id = 'sammel-lauf'; document.body.appendChild(d);
      }
      const echt = getJSON;
      window.getJSON = async (url) => url.includes('/api/smime/sammel/lauf')
        ? {ok: true, lauf: window.__lauf} : echt(url);
      window.__markiert = true;
    }""")
    unterwegs = {"status": sb.FERTIG, "gesamt": 1, "aktuell": "", "meldung": "",
                 "erledigt": [{"email": "a@x.de", "ok": True, "phase": "validating"}]}
    pg.evaluate("l => { window.__lauf = l; return sammelLaufPollen(); }", unterwegs)
    pg.evaluate("() => { clearTimeout(_sammelTimer); }")
    fertig = {**unterwegs, "erledigt": [{"email": "a@x.de", "ok": True, "phase": "ausgestellt"}]}
    with pg.expect_navigation(timeout=5000):
        pg.evaluate("l => { window.__lauf = l; return sammelLaufPollen(); }", fertig)
    assert pg.evaluate("() => window.__markiert") is None, "Seite wurde neu geladen"


def test_zeile_laedt_nach_erfolgreicher_ausstellung_neu(seite):
    """Nach Erfolg räumt das Gateway die Bestellung ab (active:false). Bis
    v1.10.33 lud die Zeile dann NICHT neu und zeigte weiter den alten Stand."""
    pg = seite("/smime", breite=1280)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      window.__antworten = [{active: true, status: 'validating'}, {active: false}];
      window._fetchAcmeStatus = async () => window.__antworten.shift() || {active: false};
      window.__markiert = true;
    }""")
    with pg.expect_navigation(timeout=9000):
        pg.evaluate("() => _startAcmePolling('zeile@x.de')")
    assert pg.evaluate("() => window.__markiert") is None


def test_zeile_ohne_laufende_bestellung_laedt_nicht_neu(seite):
    pg = seite("/smime", breite=1280)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => { window._fetchAcmeStatus = async () => ({active: false});
                          window.__markiert = true; _startAcmePolling('ruhig@x.de'); }""")
    pg.wait_for_timeout(1500)
    assert pg.evaluate("() => window.__markiert") is True, "keine Ladeschleife"


@pytest.mark.parametrize("modus", ["graph", "graph_plus"])
def test_assistent_relay_auch_ausserhalb_von_smtp_waehlbar(seite, modus):
    """Die Seite „SMTP-Relay“ trägt auch die Einlieferung mit Login auf 587
    (Sende-Identitäten), die in JEDEM Modus läuft. Ihr Menüpunkt hängt an
    SMTP_RELAY_ENABLED. Bis v1.10.66 sperrte der Assistent den Haken ausser im
    Modus smtp und schrieb ihn dort als false — in Azure gab es damit keinen
    Weg für Geräte. Jetzt: wählbar, mit Hinweis, und er wird gespeichert."""
    import json
    pg = seite("/setup", breite=1100)
    gesendet = []

    def abfangen(r):
        if r.request.method == "POST":
            gesendet.append(json.loads(r.request.post_data))
            r.fulfill(status=200, content_type="application/json", body='{"ok": false, "error": "Test"}')
        else:
            r.continue_()
    pg.route("**/settings", abfangen)
    pg.check(f'input[name="reinject-mode-wizard"][value="{modus}"]', force=True)
    pg.evaluate("updateModeSelection()")
    assert pg.is_enabled("#relay-enabled-wizard")
    assert pg.is_visible("#relay-nur-smtp")
    assert "587" in pg.text_content("#relay-nur-smtp")
    pg.check("#relay-enabled-wizard", force=True)
    pg.evaluate("saveModeSelection()")
    pg.wait_for_timeout(500)
    assert gesendet and gesendet[-1]["SMTP_RELAY_ENABLED"] is True, gesendet
    pg.context.close()


@pytest.mark.parametrize("breite", [393, 320])
def test_waechterzeile_quetscht_den_namen_nicht(browser, server, monkeypatch, breite):
    """Bypass-Wächter auf dem Handy: Drei Knöpfe standen per nowrap neben dem
    Text und liessen dem Namen wenige Zeichen — er stand Wort für Wort
    untereinander (Bildschirmfoto 15.11.2026). Jetzt rutschen die Knöpfe
    darunter; der Textblock behält eine brauchbare Breite."""
    import json
    import sso
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-oberflaeche")
    monkeypatch.setattr(sso, "_noch_gueltig", lambda p: True)
    keks = sso.create_session_cookie("chefin@example.org", role=sso.ROLE_ADMIN)
    daten = {"ok": True, "enabled": True, "code_stand": "neu", "rule_state": "an",
             "watchers": [{"id": "w1", "kind": "azure", "name": "Azure – rg-waechter/waechter-funktion2",
                           "last_seen": None, "azure": {"app_name": "waechter-funktion2",
                                                        "code_stand": "alt", "resource_group": "rg-waechter",
                                                        "location": "westeurope", "grants_done": True}}]}
    ctx = browser.new_context(viewport={"width": breite, "height": 850})
    ctx.add_cookies([{"name": sso.SESSION_COOKIE, "value": keks, "domain": "127.0.0.1", "path": "/"}])
    ctx.route("**/api/watchdog/status*",
              lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(daten)))
    pg = ctx.new_page()
    pg.goto(f"{server}/advanced", wait_until="domcontentloaded")
    pg.evaluate("() => window.loadWatchdogStatus && window.loadWatchdogStatus()")
    pg.wait_for_function("() => document.querySelector('#wd-liste strong')")
    masse = pg.evaluate("""() => {
        const s = document.querySelector('#wd-liste strong');
        const block = s.parentElement;
        const knopf = [...document.querySelectorAll('#wd-liste button')].find(b => b.textContent.includes('Code aktualisieren'));
        return {block: block.getBoundingClientRect().width,
                liste: document.getElementById('wd-liste').getBoundingClientRect().width,
                knopf: !!knopf};
    }""")
    ctx.close()
    assert masse["knopf"], "Testaufbau: der Knopf „Code aktualisieren“ fehlt"
    assert masse["block"] >= 0.8 * masse["liste"], (
        f"Textblock nur {masse['block']:.0f} px von {masse['liste']:.0f} px — die Knöpfe quetschen den Namen")


def test_code_aktualisieren_ohne_azure_login_bietet_die_anmeldung_an(browser, server, monkeypatch):
    """Fehlt die Azure-Anmeldung, verwies bis v1.12.43 eine Meldung auf
    „Wächter hinzufügen → Azure-Zugriff holen“ — den Knopf gab es dort erst nach
    Wahl von „Azure Function“. Jetzt erscheint am Wächter ein Knopf, und nach
    der Anmeldung läuft „Code aktualisieren“ von selbst weiter."""
    import json
    import sso
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-oberflaeche")
    monkeypatch.setattr(sso, "_noch_gueltig", lambda p: True)
    keks = sso.create_session_cookie("chefin@example.org", role=sso.ROLE_ADMIN)
    daten = {"ok": True, "enabled": True, "code_stand": "neu", "rule_state": "an",
             "watchers": [{"id": "w1", "kind": "azure", "name": "Azure – rg/wd",
                           "last_seen": None, "azure": {"app_name": "wd", "code_stand": "alt",
                                                        "resource_group": "rg", "grants_done": True}}]}
    aufrufe = []

    def _code(route):
        aufrufe.append(1)
        if len(aufrufe) == 1:
            route.fulfill(status=400, content_type="application/json",
                          body=json.dumps({"ok": False, "detail": "Kein Azure-Zugriff — erst per Azure-Login."}))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))
    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.add_cookies([{"name": sso.SESSION_COOKIE, "value": keks, "domain": "127.0.0.1", "path": "/"}])
    ctx.route("**/api/watchdog/status*",
              lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(daten)))
    ctx.route("**/api/watchdog/code-aktualisieren", _code)
    pg = ctx.new_page()
    meldungen = []
    pg.on("dialog", lambda d: (meldungen.append(d.message), d.accept()))
    pg.goto(f"{server}/advanced", wait_until="domcontentloaded")
    pg.evaluate("() => window.loadWatchdogStatus && window.loadWatchdogStatus()")
    pg.wait_for_function("() => [...document.querySelectorAll('#wd-liste button')].some(b => b.textContent.includes('Code aktualisieren'))")
    pg.evaluate("() => [...document.querySelectorAll('#wd-liste button')].find(b => b.textContent.includes('Code aktualisieren')).click()")
    pg.wait_for_function("() => !document.getElementById('wd-arm-nachholen').hidden")
    knopf_sichtbar = pg.is_visible("#wd-arm-nachholen button")
    # Rückmeldung des Anmeldefensters nachspielen → Aktion läuft weiter.
    pg.evaluate("() => window.postMessage({type: 'arm-auth-done'}, location.origin)")
    pg.wait_for_function("() => document.getElementById('wd-arm-nachholen').hidden")
    pg.wait_for_timeout(300)
    ctx.close()
    assert knopf_sichtbar
    assert not any("Wächter hinzufügen" in m for m in meldungen), meldungen
    assert len(aufrufe) == 2, "nach der Anmeldung lief „Code aktualisieren“ nicht erneut"


# ── Mail-Routing: Port 25 wird vor „Selbst per MX“ gemessen ──────────────────

@pytest.mark.parametrize("modus,port25_offen,antwort,gespeichert,getestet", [
    ("mx", False, "ablehnen", False, True),    # gesperrt + Nein → nichts gespeichert
    ("mx", False, "annehmen", True, True),     # gesperrt + Ja   → bewusst gespeichert
    ("mx", True, None, True, True),            # offen           → ohne Rückfrage
    ("smarthost", False, None, True, False),   # Smarthost braucht Port 25 nicht
])
def test_selbst_zustellen_prueft_port25_vor_dem_speichern(browser, server, monkeypatch, modus,
                                                          port25_offen, antwort, gespeichert,
                                                          getestet):
    """In Azure, AWS und vielen Netzen ist Port 25 ausgehend gesperrt. Wer
    „Selbst per MX zustellen“ speichert, bekommt vorher den echten Test; ist der
    Port zu, fragt die Seite nach, und ein Nein speichert nichts."""
    import json
    import sso
    monkeypatch.setattr(sso, "_get_secret", lambda: "testgeheimnis-fuer-die-oberflaeche")
    monkeypatch.setattr(sso, "_noch_gueltig", lambda p: True)
    keks = sso.create_session_cookie("chefin@example.org", role=sso.ROLE_ADMIN)
    aufrufe = {"port25": 0, "speichern": 0}
    dialoge = []

    def _port25(route):
        aufrufe["port25"] += 1
        body = ({"ok": True, "host": "mx.example.net", "antwort": "220 bereit"} if port25_offen
                else {"ok": False, "error": "Zeitüberschreitung"})
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def _speichern(route):
        aufrufe["speichern"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))

    ctx = browser.new_context(viewport={"width": 1280, "height": 900})
    ctx.add_cookies([{"name": sso.SESSION_COOKIE, "value": keks, "domain": "127.0.0.1", "path": "/"}])
    ctx.route("**/api/direkt/port25-test*", _port25)
    ctx.route("**/api/direkt/einstellungen*", _speichern)
    pg = ctx.new_page()

    def _dialog(d):
        dialoge.append(d.message)
        d.accept() if antwort == "annehmen" else d.dismiss()
    pg.on("dialog", _dialog)
    try:
        pg.goto(f"{server}/settings/routing", wait_until="domcontentloaded")
        pg.wait_for_timeout(600)
        pg.evaluate("""m => { document.getElementById('direkt-aktiv').checked = true;
                              document.getElementById('direkt-modus').value = m;
                              direktModusZeigen(); }""", modus)
        pg.evaluate("() => direktSpeichern(document.querySelector('[onclick^=\"direktSpeichern\"]'))")
        pg.wait_for_timeout(800)
    finally:
        ctx.close()
    assert aufrufe["port25"] == (1 if getestet else 0), aufrufe
    assert aufrufe["speichern"] == (1 if gespeichert else 0), (aufrufe, dialoge)
    if getestet and not port25_offen:
        assert dialoge and "Port 25 ausgehend ist von hier gesperrt" in dialoge[0], dialoge
    else:
        assert not dialoge, dialoge


# ── Webmail im Notbetrieb: verschiebbare Spalten, einzeilige breite Liste ────

def _notfall_seite(browser, server, monkeypatch, breite, speicher=None, zusatz=None):
    """Webmail mit nachgespielter API: angemeldet, ein Postfach, zwei Mails."""
    import json as _json
    import notbetrieb

    monkeypatch.setattr(notbetrieb, "funktion_aktiv", lambda: True)
    antworten = {
        "ich": {"ok": True, "benutzer": "max@firma.at", "senden": False,
                "zugriff": [{"postfach": "max@firma.at", "eigen": True}]},
        "ordner": {"ok": True, "ordner": [{"id": "e", "wk": "inbox", "name": "Posteingang",
                                           "anzahl": 2, "ungelesen": 1}]},
        "liste": {"ok": True, "gesamt": 2, "je_seite": 50, "mails": [
            {"graph_id": "m1", "von": "Erika Musterfrau", "betreff": "Angebot", "empfangen":
             "2026-11-16T09:00:00Z", "gelesen": False, "vorschau": "Hallo Max, anbei das Angebot"},
            {"graph_id": "m2", "von": "Hans Beispiel", "betreff": "Termin", "empfangen":
             "2026-11-16T08:00:00Z", "gelesen": True, "vorschau": "Passt Donnerstag?",
             "an": "Vertrieb <vertrieb@firma.at>"}]},
        "mail": {"ok": True, "betreff": "Termin", "von": [{"name": "Hans Beispiel", "adresse": "hans@firma.at"}],
                 "an": [{"name": "Vertrieb", "adresse": "vertrieb@firma.at"}], "cc": [], "anhaenge": [],
                 "datum": "2026-11-16T08:00:00Z", "html": False, "text": "Passt Donnerstag?"},
    } | (zusatz or {})
    gesendet = []
    ctx = browser.new_context(viewport={"width": breite, "height": 900})
    if speicher:
        ctx.add_init_script("localStorage.setItem('nf-spalten', %s)" % _json.dumps(_json.dumps(speicher)))

    def api(route):
        teil = route.request.url.split("/notfall/api/")[1].split("?")[0]
        if route.request.method == "POST":
            gesendet.append((teil, route.request.post_data_json))
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps(antworten.get(teil, {"ok": True})))

    ctx.route("**/notfall/api/**", api)
    pg = ctx.new_page()
    pg.goto(f"{server}/notfall", wait_until="domcontentloaded", timeout=30000)
    pg.wait_for_selector("#liste .eintrag", timeout=10000)
    pg.gesendet = gesendet
    return pg


def _hoehe(pg, sel):
    return pg.eval_on_selector(sel, "e => e.getBoundingClientRect().height")


def test_notfall_liste_wird_einzeilig_wenn_breit_gezogen(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1600)
    zweizeilig = _hoehe(pg, "#liste .eintrag")
    assert pg.eval_on_selector("#listenspalte", "e => e.offsetWidth") == 360

    # Trenner zwischen Liste und Lesebereich mit der Maus nach rechts ziehen.
    box = pg.locator('.trenner[data-spalte="liste"]').bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + 200
    pg.mouse.move(x, y); pg.mouse.down(); pg.mouse.move(x + 500, y, steps=5); pg.mouse.up()

    assert pg.eval_on_selector("#listenspalte", "e => e.offsetWidth") > 800
    einzeilig = _hoehe(pg, "#liste .eintrag")
    assert einzeilig < zweizeilig * 0.6, (zweizeilig, einzeilig)
    # Von, Betreff und Datum stehen auf einer Höhe.
    oben = [pg.eval_on_selector(f"#liste .eintrag {s}", "e => Math.round(e.getBoundingClientRect().top)")
            for s in (".von", ".betreff", ".datum")]
    assert max(oben) - min(oben) <= 4, oben
    # Gemerkt: neu laden behält die Breite.
    gemerkt = pg.evaluate("JSON.parse(localStorage.getItem('nf-spalten')).liste")
    assert gemerkt > 800
    pg.context.close()


def test_notfall_spalten_lassen_dem_lesebereich_platz(browser, server, monkeypatch):
    # Gemerkte Breite aus einem großen Bildschirm, jetzt am kleinen Notebook.
    pg = _notfall_seite(browser, server, monkeypatch, 1100, speicher={"ordner": 240, "liste": 2000})
    assert pg.eval_on_selector("#lesen", "e => e.offsetWidth") >= 320
    # Pfeiltaste verkleinert, Doppelklick setzt zurück.
    t = pg.locator('.trenner[data-spalte="liste"]')
    vorher = pg.eval_on_selector("#listenspalte", "e => e.offsetWidth")
    t.focus(); pg.keyboard.press("ArrowLeft")
    assert pg.eval_on_selector("#listenspalte", "e => e.offsetWidth") == vorher - 20
    t.dblclick()
    assert pg.eval_on_selector("#listenspalte", "e => e.offsetWidth") == 360
    pg.context.close()


def test_notfall_mobil_ohne_trenner_und_zweizeilig(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 393, speicher={"liste": 900})
    assert pg.eval_on_selector('.trenner[data-spalte="liste"]', "e => getComputedStyle(e).display") == "none"
    assert pg.evaluate("document.documentElement.scrollWidth") <= 393
    kopf = pg.eval_on_selector("#liste .eintrag .von", "e => Math.round(e.getBoundingClientRect().top)")
    betreff = pg.eval_on_selector("#liste .eintrag .betreff", "e => Math.round(e.getBoundingClientRect().top)")
    assert betreff > kopf   # Betreff unter dem Absender, nicht daneben
    pg.context.close()


def test_notfall_suche_beim_tippen_und_markiert(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1300)
    anfragen = []
    pg.on("request", lambda r: anfragen.append(r.url) if "/notfall/api/liste" in r.url and "suche=" in r.url
          and "suche=&" not in r.url else None)
    pg.fill("#suche", "")
    pg.type("#suche", "an", delay=30)           # zwei Zeichen: noch keine Suche
    pg.wait_for_timeout(700)
    assert anfragen == []
    pg.type("#suche", "gebot", delay=30)        # zügig weitergetippt: EINE Suche nach der Pause
    pg.wait_for_timeout(900)
    assert len(anfragen) == 1 and "suche=angebot" in anfragen[0], anfragen
    # Markiert, in der Schreibung der Mail, und nur das Wort.
    assert pg.eval_on_selector("#liste .eintrag .betreff mark", "e => e.textContent") == "Angebot"
    assert pg.eval_on_selector("#liste .eintrag .vorschau mark", "e => e.textContent") == "Angebot"
    farbe = pg.eval_on_selector("#liste mark", "e => getComputedStyle(e).backgroundColor")
    assert farbe == "rgb(253, 230, 138)", farbe
    pg.context.close()


def test_notfall_suche_markiert_absender_und_empfaenger(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1300)
    # Treffer nur beim Empfänger: Die Liste zeigt dafür eine An-Zeile, markiert.
    pg.fill("#suche", "vertrieb"); pg.press("#suche", "Enter")
    pg.wait_for_timeout(500)
    assert pg.eval_on_selector('#liste .eintrag[data-id="m2"] .an mark', "e => e.textContent") == "Vertrieb"
    assert pg.locator('#liste .eintrag[data-id="m1"] .an').count() == 0     # kein Fund im An: keine Zeile
    # Geöffnete Mail: An-Feld markiert.
    pg.click('#liste .eintrag[data-id="m2"]')
    pg.wait_for_selector("#lesen-betreff")
    assert "vertrieb" in pg.inner_html("#lesen .kopfbereich").lower().split("<mark>")[1].split("</mark>")[0]
    # Absender: in Liste und Kopf der Mail.
    pg.fill("#suche", "hans"); pg.press("#suche", "Enter")
    pg.wait_for_timeout(500)
    assert pg.eval_on_selector('#liste .eintrag[data-id="m2"] .von mark', "e => e.textContent") == "Hans"
    pg.click('#liste .eintrag[data-id="m2"]')
    pg.wait_for_timeout(300)
    marken = pg.eval_on_selector_all("#lesen .kopfbereich mark", "es => es.map(e => e.textContent)")
    assert "Hans" in marken and "hans" in marken, marken      # Name und Adresse
    pg.context.close()


def test_notfall_antwort_zeigt_zitat_statt_textzitat(browser, server, monkeypatch):
    """Das Original steht nicht mehr als „> “-Text im Feld; es ist eingeklappt
    darunter zu sehen, und beim Senden geht der Bezug samt Postfach mit."""
    zusatz = {
        "ich": {"ok": True, "benutzer": "max@firma.at", "senden": True, "sendeweg": "notfall",
                "zugriff": [{"postfach": "max@firma.at", "eigen": True}],
                "absender": [{"postfach": "max@firma.at", "recht": "eigen"}]},
        "vorlage": {"ok": True, "an": "hans@firma.at", "cc": "", "betreff": "AW: Termin", "text": "",
                    "zitat": {"kopf": [{"feld": "Von", "wert": "Hans Beispiel <hans@firma.at>"},
                                       {"feld": "Betreff", "wert": "Termin"}],
                              "html": False, "text": "Passt Donnerstag?"}},
        "senden": {"ok": True, "ueber": "notfall"},
    }
    pg = _notfall_seite(browser, server, monkeypatch, 393, zusatz=zusatz)
    pg.click('#liste .eintrag[data-id="m2"]')
    pg.wait_for_selector("#lesen-betreff")
    pg.evaluate("verfassen('antwort')")
    pg.wait_for_selector("#verfassen .zitat")
    assert pg.input_value("#vf-text") == ""
    assert "unten angefügt" in pg.inner_text("#verfassen .zitat summary")
    pg.click("#verfassen .zitat summary")
    assert pg.inner_text("#verfassen .zitat pre") == "Passt Donnerstag?"
    assert "Hans Beispiel <hans@firma.at>" in pg.inner_text("#verfassen .zitat .kopf")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 393
    pg.fill("#vf-text", "Ja, passt.")
    pg.click("#vf-senden")
    pg.wait_for_selector("#lesen .ok")
    teil, rumpf = next(x for x in pg.gesendet if x[0] == "senden")
    assert (rumpf["bezug"], rumpf["art"], rumpf["bezug_postfach"]) == ("m2", "antwort", "max@firma.at")
    pg.context.close()


# ── Webmail-Layout: Befehlsleiste, Gruppen, Initialen ───────────────────────

_SENDER = {"ich": {"ok": True, "benutzer": "max@firma.at", "senden": True, "sendeweg": "notfall",
                   "zugriff": [{"postfach": "max@firma.at", "eigen": True}],
                   "absender": [{"postfach": "max@firma.at", "recht": "eigen"}]}}


def test_notfall_befehlsleiste_loescht_nur_die_geoeffnete_mail(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1300, zusatz=_SENDER)
    assert _sichtbar(pg, "#befehle")
    # Ohne geöffnete Mail gibt es nichts zu löschen oder zu verschieben.
    assert pg.is_disabled("#b-loeschen") and pg.is_disabled("#b-verschieben")
    pg.click('#liste .eintrag[data-id="m2"]')
    pg.wait_for_selector("#lesen .mailansicht")
    assert not pg.is_disabled("#b-loeschen") and not pg.is_disabled("#b-verschieben")
    # Die Antwort-Symbole sitzen am Absender der Mail.
    assert pg.locator('#lesen .symbole button[title="Antworten"]').count() == 1
    pg.click("#b-loeschen")
    pg.wait_for_function("() => !document.querySelector('#lesen .mailansicht')")
    teil, rumpf = next(x for x in pg.gesendet if x[0] == "verschieben")
    assert (rumpf["id"], rumpf["ziel"]) == ("m2", "deleteditems")
    # Mail weg: wieder gesperrt.
    pg.wait_for_function("() => document.getElementById('b-loeschen').disabled")
    pg.context.close()


def test_notfall_ohne_senderecht_keine_befehlsleiste(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1300)
    assert not _sichtbar(pg, "#befehle")
    pg.context.close()


def test_notfall_liste_gruppen_initialen_und_ordnerzahl(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1300)
    # Vor der ersten Mail steht ihre Datumsgruppe.
    assert pg.evaluate("document.querySelector('#liste').firstElementChild.className") == "gruppe"
    assert pg.inner_text('#liste .eintrag[data-id="m1"] .avatar') == "EM"
    assert pg.inner_text('#liste .eintrag[data-id="m2"] .avatar') == "HB"
    gruppen = pg.evaluate("""() => { const t = n => new Date(Date.now() - n * 864e5).toISOString();
        return [datumGruppe(t(0)), datumGruppe(t(1)), datumGruppe(t(60))]; }""")
    assert gruppen[:2] == ["Heute", "Gestern"] and gruppen[2] not in ("Heute", "Gestern", "Diese Woche", "Letzte Woche")
    initialen = pg.evaluate("""() => ['Mustermann, Max', 'max.mustermann@firma.at', '"Erika Musterfrau" <e@x.at>']
        .map(n => avatar(n).match(/>([^<]*)</)[1])""")
    assert initialen == ["MM", "MM", "EM"], initialen
    # Gleicher Name, gleiche Farbe — die Farbe kommt als Attribut, nie als Stil.
    farben = pg.evaluate("() => [avatar('Hans Beispiel'), avatar('Hans Beispiel')].map(h => h.match(/data-farbe=\"(\\d)\"/)[1])")
    assert farben[0] == farben[1]
    assert "style" not in pg.evaluate("avatar('Hans Beispiel')")
    # Ordner zeigen sichtbar nur die ungelesenen (1 von 2), die Gesamtzahl im Tooltip.
    assert pg.inner_text('#ordner .eintrag[data-id="e"] .zahl') == "1"
    assert "1 ungelesen von 2" in pg.get_attribute('#ordner .eintrag[data-id="e"]', "title")
    pg.context.close()


def test_notfall_termine_ohne_leere_kreisspalte(browser, server, monkeypatch):
    """Kalender und Entwürfe haben keinen Absender — ihre Zeilen dürfen die
    Spalte für die Initialen nicht freilassen."""
    zusatz = {"kalender": {"ok": True, "termine": [
        {"betreff": "Jour fixe", "beginn": "2026-11-16T09:00:00Z", "ende": "2026-11-16T10:00:00Z",
         "ort": "Raum 1", "teilnehmer": [], "ganztag": False}]},
        "kontakte": {"ok": True, "kontakte": [{"name": "Erika Musterfrau", "mails": ["erika@firma.at"],
                                               "telefon": [], "firma": ""}]}}
    pg = _notfall_seite(browser, server, monkeypatch, 1300, zusatz=zusatz)
    links = pg.eval_on_selector("#liste", "e => e.getBoundingClientRect().left")
    pg.evaluate("ordnerWaehlen('__kalender')")
    pg.wait_for_selector("#liste .eintrag .betreff")
    abstand = pg.eval_on_selector("#liste .eintrag .betreff", "e => e.getBoundingClientRect().left") - links
    assert abstand < 30, abstand
    assert pg.inner_text("#listentitel") == "Kalender"
    pg.evaluate("ordnerWaehlen('__kontakte')")
    pg.wait_for_selector("#liste .eintrag .avatar")
    assert pg.inner_text("#liste .eintrag .avatar") == "EM"
    pg.context.close()


_VERFASSEN = _SENDER | {
    "vorlage": {"ok": True, "an": "hans@firma.at", "cc": "", "bcc": "", "betreff": "AW: Termin", "text": "",
                "zitat": None},
    "vorschau": {"ok": True, "zustand": "angehaengt", "smime": True, "ohne_empfaenger": False, "meldung": "",
                 "url": "/notfall/vorschau?k=probe"}}


def test_notfall_befehlsleiste_antworten_und_ungelesen(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1300, zusatz=_VERFASSEN)
    for k in ("#b-antworten", "#b-allen", "#b-weiterleiten", "#b-gelesen"):
        assert pg.is_disabled(k), k
    pg.click('#liste .eintrag[data-id="m1"]')
    pg.wait_for_selector("#lesen .mailansicht")
    assert not pg.is_disabled("#b-antworten") and pg.inner_text("#b-gelesen") == "Als ungelesen markieren"
    pg.click("#b-gelesen")
    pg.wait_for_function("() => document.querySelector('#b-gelesen').innerText === 'Als gelesen markieren'")
    assert pg.get_attribute('#liste .eintrag[data-id="m1"]', "data-ungelesen") == "1"
    pg.click("#b-gelesen")
    pg.wait_for_function("() => document.querySelector('#b-gelesen').innerText === 'Als ungelesen markieren'")
    assert [(r["id"], r["gelesen"]) for t, r in pg.gesendet if t == "gelesen"] == [("m1", False), ("m1", True)]
    # Antworten aus der Leiste: Formular mit Vorschau dessen, was angehängt wird.
    pg.click("#b-antworten")
    pg.wait_for_function("() => (document.getElementById('vf-sig-titel') || {textContent: ''}).textContent.startsWith('Diese')")
    assert pg.inner_text("#vf-sig-titel") == "Diese Signatur, Banner und Disclaimer werden angehängt · S/MIME-signiert"
    assert pg.get_attribute("#vf-sig iframe", "src") == "/notfall/vorschau?k=probe"
    assert pg.get_attribute("#vf-sig iframe", "sandbox") == ""
    erste = [r for t, r in pg.gesendet if t == "vorschau"]
    assert erste[0]["art"] == "antwort" and erste[0]["bezug"] == "m1"
    # Text tippen rechnet nicht neu, Empfänger ändern schon.
    pg.fill("#vf-text", "Danke!")
    pg.wait_for_timeout(1400)
    assert len([1 for t, _ in pg.gesendet if t == "vorschau"]) == 1
    pg.fill("#vf-cc", "erika@firma.at")
    pg.wait_for_timeout(1400)
    zweite = [r for t, r in pg.gesendet if t == "vorschau"]
    assert len(zweite) == 2 and zweite[1]["cc"] == "erika@firma.at"
    pg.context.close()


def test_notfall_befehlsleiste_passt_aufs_handy(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 393, zusatz=_VERFASSEN)
    pg.click('#liste .eintrag[data-id="m2"]')
    pg.wait_for_selector("#lesen .mailansicht")
    assert _sichtbar(pg, "#b-gelesen") and _sichtbar(pg, "#b-antworten")
    assert pg.evaluate("document.documentElement.scrollWidth") <= 393
    pg.context.close()


def test_notfall_listenspalten_ziehbar_und_vorschautext(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1600, speicher={"liste": 1000},
                        zusatz=_SENDER | {"kalender": {"ok": True, "termine": []}})
    assert _sichtbar(pg, "#spaltenkopf")
    links = lambda sel: pg.eval_on_selector(sel, "e => Math.round(e.getBoundingClientRect().left)")
    breite = lambda sel: pg.eval_on_selector(sel, "e => Math.round(e.getBoundingClientRect().width)")
    # Kopfzeile und Mails teilen das Gitter: Spalten stehen bündig.
    assert abs(links("#spaltenkopf .sk:nth-child(3)") - links('#liste .eintrag[data-id="m1"] .betreff')) <= 1
    assert breite("#spaltenkopf .sk:nth-child(2)") == 170
    griff = pg.locator('.griff[data-s="von"]').bounding_box()
    x, y = griff["x"] + griff["width"] / 2, griff["y"] + griff["height"] / 2
    pg.mouse.move(x, y); pg.mouse.down(); pg.mouse.move(x + 80, y, steps=4); pg.mouse.up()
    assert 245 <= breite("#spaltenkopf .sk:nth-child(2)") <= 262     # Griff sitzt mitten im Spaltenabstand
    assert abs(breite('#liste .eintrag[data-id="m1"] .von') - breite("#spaltenkopf .sk:nth-child(2)")) <= 1
    assert abs(links("#spaltenkopf .sk:nth-child(3)") - links('#liste .eintrag[data-id="m1"] .betreff')) <= 1
    assert "von" in pg.evaluate("localStorage.getItem('nf-listenspalten')")
    pg.dblclick('.griff[data-s="von"]')
    assert breite("#spaltenkopf .sk:nth-child(2)") == 170
    # Vorschautext aus: weg, und gemerkt.
    assert _sichtbar(pg, '#liste .eintrag[data-id="m1"] .vorschau')
    pg.click("label:has(#f-vorschautext)")
    assert not _sichtbar(pg, '#liste .eintrag[data-id="m1"] .vorschau')
    assert pg.evaluate("localStorage.getItem('nf-vorschautext')") == "0"
    # Kalender & Co. haben keine Von/Betreff-Spalten.
    pg.evaluate("ordnerWaehlen('__kalender')")
    assert not _sichtbar(pg, "#spaltenkopf")
    pg.context.close()


def test_notfall_schmale_liste_ohne_spaltenkopf(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1100)
    assert not _sichtbar(pg, "#spaltenkopf")
    pg.context.close()


# ── Notbetrieb-Seite: Lage, Abläufe, Geräte ─────────────────────────────────

def test_notbetrieb_lage_knopf_und_vollstaendige_ablaeufe(seite, monkeypatch):
    """Der Notfall ist ein Knopf mit Rückfrage, kein Kästchen wie jedes andere;
    die Abläufe stehen nummeriert und vollständig da (nicht nach zwei Zeilen
    gekürzt); „Auch senden“ ist ohne „für alle lesbar“ gesperrt; die Geräte
    sind zugeklappt, ein Anker öffnet seinen Abschnitt."""
    import notbetrieb
    monkeypatch.setattr(notbetrieb, "funktion_aktiv", lambda: True)
    pg = seite("/notbetrieb", breite=1280)
    assert pg.get_attribute("#nb-lage", "data-lage") == "normal"
    assert pg.inner_text("#nb-ausrufen-btn").strip() == "Notfall ausrufen …"
    gespeichert = []
    pg.route("**/settings", lambda r: (gespeichert.append(r.request.post_data_json),
                                       r.fulfill(status=200, content_type="application/json", body='{"ok": true}')))
    pg.evaluate("window.location.reload = () => {}")
    pg.click("#nb-ausrufen-btn")                               # Rückfrage: die Fixture bestätigt
    pg.wait_for_function("() => document.getElementById('nb-ausrufen-btn').disabled === false")
    assert gespeichert and gespeichert[-1].get("NOTBETRIEB_AUSGERUFEN") is True, gespeichert
    schritte = pg.locator(".nb-schritte li")
    assert schritte.count() == 8
    assert pg.locator(".nb-ablauf .hint-toggle").count() == 0
    assert "Zurückspielen" in schritte.nth(7).inner_text()
    assert pg.is_disabled("#nb-senden-alle") != pg.is_checked("#nb-lesen-alle")
    for k in ("nb-geraete-windows", "nb-teams-app", "nb-geraete-handys"):
        assert pg.get_attribute(f"#{k}", "open") is None, k
    pg.evaluate("location.hash = '#nb-teams-app'")
    pg.wait_for_function("() => document.getElementById('nb-teams-app').open")
    pg.context.close()


def test_notbetrieb_stand_als_uebersicht_bleibt_nach_dem_abgleich(seite, monkeypatch):
    """Der Stand der Kopie war eine lange Zeile; jetzt Bezeichnung │ Wert,
    Fehler über 0 gelb. Und: Nach „Jetzt abgleichen“ verschwand die Zeile, weil
    sie als Fortschrittsanzeige diente (versteckt und geleert am Ende)."""
    import json as _json
    import notbetrieb
    monkeypatch.setattr(notbetrieb, "funktion_aktiv", lambda: True)
    status = {"ok": True, "mails": 143, "postfaecher_im_schatten": 4, "lokal_offen": 0, "profile": 36,
              "profile_stand": "2026-11-17T20:35:44Z",
              "letzter_lauf": {"ts": "2026-11-17T21:12:37Z", "neu": 0, "entfernt": 0, "abgelaufen": 0,
                               "fehler": 2, "postfaecher_fehler": 0, "postfaecher": 21},
              "eingang": {"angenommen": 5, "unbekannt": 1}}

    def api(route):
        teil = route.request.url.split("/api/notbetrieb/")[1].split("?")[0]
        antwort = {"status": status, "abgleich": {"ok": True, "neu": 0, "entfernt": 0}}.get(teil, {"ok": True, "items": []})
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(antwort))
    pg = seite("/notbetrieb", breite=1280)
    pg.context.route("**/api/notbetrieb/**", api)
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_function("() => document.querySelectorAll('#nb-stand dt').length === 3")
    zeilen = pg.eval_on_selector_all("#nb-stand dt", "e => e.map(x => x.innerText)")
    assert zeilen == ["Kopie", "Profile für Signaturen", "Letzter Abgleich"], zeilen
    assert pg.inner_text("#nb-stand dd:nth-of-type(1)") == "143 Mails aus 4 Postfächern"
    fehler = pg.locator('#nb-stand [data-zustand="warnung"]')
    assert fehler.count() == 1 and fehler.inner_text() == "2 Fehler"
    assert "21 Postfächer" in pg.inner_text("#nb-stand dd:nth-of-type(3)")
    assert pg.eval_on_selector_all("#nb-eingang-stand dt", "e => e.length") == 5
    pg.click("#nb-abgleich-btn")
    pg.wait_for_function("() => !document.getElementById('nb-abgleich-btn').disabled")
    pg.wait_for_timeout(300)
    assert _sichtbar(pg, "#nb-stand") and pg.eval_on_selector_all("#nb-stand dt", "e => e.length") == 3
    pg.context.close()


def test_notbetrieb_platz_was_waere_wenn_und_verwerfen(seite, monkeypatch):
    """Platzbedarf steht bei den Feldern: ändern → sofort gerechnet (ohne
    Speichern), als „noch nicht gespeichert“ markiert; „Änderungen verwerfen“
    stellt den gespeicherten Stand her und sperrt Speichern wieder."""
    import json as _json
    import notbetrieb
    monkeypatch.setattr(notbetrieb, "funktion_aktiv", lambda: True)
    gerechnet = []

    def api(route):
        teil = route.request.url.split("/api/notbetrieb/")[1].split("?")[0]
        if teil == "platz-rechnen":
            gerechnet.append(route.request.post_data_json)
            antwort = {"ok": True, "gemessen": True, "bedarf": 1_000_000, "verfuegbar": 10_000_000, "anteil": 0.1,
                       "frei": 9_000_000, "belegt": 1_000_000, "mess_ts": "2026-11-17T20:00:00Z", "mess_mails": 10,
                       "mess_tage": 30, "mess_dauer_s": 1, "nach_herkunft": {}, "geeicht": False}
        else:
            antwort = {"ok": True, "items": []}
        route.fulfill(status=200, content_type="application/json", body=_json.dumps(antwort))
    pg = seite("/notbetrieb", breite=1280)
    pg.context.route("**/api/notbetrieb/**", api)
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_function("() => !document.getElementById('nb-platz-urteil').hidden")
    alt = pg.input_value("#nb-std-mail")
    assert not _sichtbar(pg, "#nb-fenster-verwerfen") and pg.is_disabled("#nb-fenster-btn")
    pg.fill("#nb-std-mail", str(int(alt) + 7))
    pg.wait_for_function("() => document.getElementById('nb-platz-urteil').innerText.includes('noch nicht gespeichert')")
    assert gerechnet[-1]["fenster"]["mail"] == int(alt) + 7                  # gerechnet, nicht gespeichert
    assert _sichtbar(pg, "#nb-fenster-verwerfen") and not pg.is_disabled("#nb-fenster-btn")
    pg.click("#nb-fenster-verwerfen")
    pg.wait_for_function("() => document.getElementById('nb-fenster-verwerfen').hidden")
    assert pg.input_value("#nb-std-mail") == alt
    pg.wait_for_function("() => document.getElementById('nb-fenster-btn').disabled")
    assert "noch nicht gespeichert" not in pg.inner_text("#nb-platz-urteil")
    pg.context.close()


# ── Roter Faden: „Dein Notfall-Zugang“ statt Anmeldemaske ────────────────────

_FAKTOR_NUR_APP = {"ok": True, "postfach": "max@firma.at", "aktiv": True, "totp": True, "seit": "2026-11-01",
                   "passkeys": [], "codes_uebrig": 8, "gesperrt": False, "webmail": False}


def _notfall_start(browser, server, monkeypatch, faktor, ausgerufen=False, pfad="/notfall", breite=1100):
    """Nicht angemeldet (/api/ich 401); `faktor` = Antwort von /api/faktor
    (None = keine frische Microsoft-Anmeldung, 401)."""
    import json as _json
    import notbetrieb

    monkeypatch.setattr(notbetrieb, "funktion_aktiv", lambda: True)
    monkeypatch.setattr(notbetrieb, "ausgerufen", lambda: ausgerufen)
    ctx = browser.new_context(viewport={"width": breite, "height": 900})

    def api(route):
        teil = route.request.url.split("/notfall/api/")[1].split("?")[0]
        if teil == "faktor" and faktor is not None:
            route.fulfill(status=200, content_type="application/json", body=_json.dumps(faktor))
            return
        route.fulfill(status=401, content_type="application/json", body='{"detail": "Nicht angemeldet."}')

    ctx.route("**/notfall/api/**", api)
    pg = ctx.new_page()
    pg.goto(f"{server}{pfad}", wait_until="domcontentloaded", timeout=30000)
    _zugang_geladen(pg)
    return pg


# Fertig ist die Seite, wenn die Anmeldemaske steht oder der Zugang seinen
# Stand zeigt (beides nach einer Antwort von /api/ich bzw. /api/faktor).
_GELADEN = """() => { const z = id => !document.getElementById(id).hidden;
  return z('anmeldung') || (z('einrichten') && (z('ein-stand') || z('ein-anmelden'))); }"""


def _zugang_geladen(pg):
    pg.wait_for_function(_GELADEN, timeout=10000)


def _sichtbar(pg, sel):
    return pg.eval_on_selector(sel, "e => !e.closest('[hidden]') && e.offsetParent !== null")


def test_notfall_normalbetrieb_startet_beim_zugang(browser, server, monkeypatch):
    pg = _notfall_start(browser, server, monkeypatch, faktor=None)
    assert _sichtbar(pg, "#einrichten") and not _sichtbar(pg, "#anmeldung")
    assert pg.inner_text("#einrichten h1") == "Deinen Notfall-Zugang einrichten"
    assert "/auth/login/microsoft?next=/notfall/einrichten" in pg.get_attribute("#ein-anmelden a", "href")
    # Anmeldung ausprobieren: ohne Sitzung zur Anmeldemaske, mit Weg zurück.
    pg.click("#ein-weiter")
    pg.wait_for_selector("#anmeldung:not([hidden])", timeout=5000)     # weiter() fragt erst /api/ich
    assert _sichtbar(pg, "#anmeldung") and not _sichtbar(pg, "#einrichten")
    assert pg.inner_text("#anmeldung h1") == "Notfall-Anmeldung"
    assert _sichtbar(pg, "#zum-zugang")
    pg.context.close()


def test_notfall_ausprobieren_setzt_die_adresse_des_webmails(browser, server, monkeypatch):
    """Ein Favorit nach „ausprobieren“ muss auf /notfall zeigen — die
    Einrichtung braucht die Microsoft-Anmeldung, die im Notfall fehlt."""
    pg = _notfall_start(browser, server, monkeypatch, faktor=None, pfad="/notfall/einrichten?host=teams")
    pg.click("#ein-weiter")
    pg.wait_for_selector("#anmeldung:not([hidden])", timeout=5000)
    assert pg.url.endswith("/notfall?host=teams"), pg.url
    pg.context.close()


def test_notfall_passkey_nur_wo_der_rahmen_ihn_erlaubt(browser, server, monkeypatch):
    """Ein Rahmen ohne publickey-credentials-get lässt navigator.credentials
    hängen (Teams-Desktop-Reiter) — dann bietet die Seite keinen Passkey an."""
    pg = _notfall_start(browser, server, monkeypatch, faktor=None)
    assert pg.evaluate("pkMoeglich()") is True
    for erlaubt, erwartet in (("publickey-credentials-get 'none'; publickey-credentials-create 'none'", False),
                              ("publickey-credentials-get 'self'; publickey-credentials-create 'self'", True)):
        pg.evaluate("""([url, allow]) => new Promise(fertig => { const f = document.createElement('iframe');
            f.setAttribute('allow', allow); f.onload = fertig; f.src = url; document.body.appendChild(f); })""",
                    [pg.url, erlaubt])
        rahmen = pg.frames[-1]
        rahmen.wait_for_function("typeof pkMoeglich === 'function'")
        assert rahmen.evaluate("[pkMoeglich('get'), pkMoeglich('create')]") == [erwartet, erwartet], erlaubt
    pg.context.close()


def test_notfall_zugang_nur_app_schlaegt_passkey_vor(browser, server, monkeypatch):
    pg = _notfall_start(browser, server, monkeypatch, faktor=_FAKTOR_NUR_APP)
    stand = pg.locator("#ein-zustand")
    assert stand.inner_text().startswith("Bereit ✓") and stand.get_attribute("data-zustand") == "bereit"
    assert "Authenticator-App" in stand.inner_text() and "8 Wiederherstellungscodes" in stand.inner_text()
    assert _sichtbar(pg, "#ein-pk-hinweis") and pg.inner_text("#ein-pk-hinweis").startswith("Tipp")
    assert pg.inner_text("#ein-weiter") == "Notfall-Anmeldung ausprobieren"
    pg.context.close()


def test_notfall_zugang_offen_und_weiter_zur_post(browser, server, monkeypatch):
    offen = _FAKTOR_NUR_APP | {"aktiv": False, "totp": False, "codes_uebrig": 0, "webmail": True}
    pg = _notfall_start(browser, server, monkeypatch, faktor=offen, breite=393)
    assert pg.get_attribute("#ein-zustand", "data-zustand") == "offen"
    assert pg.inner_text("#ein-zustand").startswith("Noch nicht eingerichtet")
    assert not _sichtbar(pg, "#ein-pk-hinweis")            # kein „Tipp“ ohne Faktor
    assert pg.inner_text("#ein-weiter") == "Weiter zur Post"
    assert pg.evaluate("document.documentElement.scrollWidth") <= 393
    pg.context.close()


def test_notfall_ausgerufen_startet_bei_der_anmeldung(browser, server, monkeypatch):
    pg = _notfall_start(browser, server, monkeypatch, faktor=None, ausgerufen=True)
    assert _sichtbar(pg, "#anmeldung") and not _sichtbar(pg, "#einrichten")
    assert pg.inner_text("#anmeldung h1") == "Anmelden"
    # /notfall/einrichten zeigt auch im Notfall den Zugang (alte Links, Konto-Menü).
    pg.goto(pg.url.split("/notfall")[0] + "/notfall/einrichten", wait_until="domcontentloaded")
    _zugang_geladen(pg)
    assert _sichtbar(pg, "#einrichten")
    pg.context.close()


def test_notfall_im_teams_reiter_richtet_im_browser_ein(browser, server, monkeypatch):
    pg = _notfall_start(browser, server, monkeypatch, faktor=None, pfad="/notfall?host=teams")
    link = pg.locator("#ein-anmelden a")
    assert link.inner_text() == "Im Browser einrichten"
    assert link.get_attribute("target") == "_blank" and link.get_attribute("data-extern") is not None
    assert "login.microsoftonline" not in (link.get_attribute("href") or "")
    pg.context.close()


def test_notfall_teams_konto_geht_als_anmeldehinweis_mit(browser, server, monkeypatch):
    """Teams-Reiter → „Im Browser einrichten“ trägt das Teams-Konto mit; die
    Einrichtung im Browser gibt es an die Microsoft-Anmeldung weiter (dort
    war sonst ein anderes Konto angemeldet, Klicktest 17.11.2026)."""
    import json as _json
    import notbetrieb
    monkeypatch.setattr(notbetrieb, "funktion_aktiv", lambda: True)
    monkeypatch.setattr(notbetrieb, "ausgerufen", lambda: False)
    ctx = browser.new_context(viewport={"width": 1100, "height": 900})
    ctx.add_init_script("""window.microsoftTeams = {app: {
        initialize: async () => {}, getContext: async () => ({user: {loginHint: 'nb.cloud@firma.at'}}),
        openLink: async u => { window._geoeffnet = u; }}};""")
    ctx.route("**/*teams-js*", lambda r: r.abort())
    ctx.route("**/*MicrosoftTeams*", lambda r: r.abort())
    ctx.route("**/notfall/api/**", lambda r: r.fulfill(status=401, content_type="application/json",
                                                        body=_json.dumps({"detail": "x"})))
    pg = ctx.new_page()
    pg.goto(f"{server}/notfall?host=teams", wait_until="domcontentloaded", timeout=30000)
    _zugang_geladen(pg)
    pg.wait_for_function("() => nb.teamsOk && nb.teamsKonto")
    pg.click("#ein-anmelden a")
    pg.wait_for_function("() => window._geoeffnet")
    ziel = pg.evaluate("window._geoeffnet")
    assert "/notfall/einrichten" in ziel and "login_hint=nb.cloud%40firma.at" in ziel, ziel
    # Im Browser angekommen: der Hinweis geht an die Microsoft-Anmeldung.
    pg.goto(f"{server}/notfall/einrichten?login_hint=nb.cloud%40firma.at", wait_until="domcontentloaded")
    _zugang_geladen(pg)
    href = pg.get_attribute("#ein-anmelden a", "href")
    assert href.startswith("/auth/login/microsoft?") and "login_hint=nb.cloud%40firma.at" in href, href
    ctx.close()


def test_webmail_hat_konto_menue(browser, server, monkeypatch):
    pg = _notfall_seite(browser, server, monkeypatch, 1300)
    assert _sichtbar(pg, "#konto-link")
    assert pg.get_attribute("#konto-link", "href") == "/notfall/einrichten"
    assert pg.inner_text("#konto-link") == "Anmeldung & Sicherheit"
    pg.context.close()


# ── Baukasten: lange Eingaben (Freitext, HTML-Code) ──────────────────────────
# Anlass: Beide Felder waren 60px hoch und standen neben einer 110px-Beschriftung
# in einer rund 400px breiten Spalte — längere Texte waren kaum zu bearbeiten.

@pytest.mark.parametrize("typ,feld", [("text", "text"), ("freetext", "html")])
@pytest.mark.parametrize("breite", [1280, 393])
def test_lange_eingabe_breit_hoch_und_gross_bearbeitbar(seite, typ, feld, breite):
    pg = seite("/template", breite=breite)
    pg.evaluate("t => addBlock(t)", typ)
    pg.locator(".block-card").last.locator(".block-type-badge").click()
    karte = pg.locator(".block-card").last
    ta = karte.locator(f"textarea[name={feld}]")
    mass = ta.evaluate("""el => {
        const k = el.closest('.block-card-body').getBoundingClientRect();
        const r = el.getBoundingClientRect();
        return {anteil: r.width / k.width, hoehe: r.height};
    }""")
    assert mass["anteil"] > 0.85, f"Feld nutzt nur {mass['anteil']:.0%} der Kartenbreite"
    assert mass["hoehe"] >= 100, f"Feld nur {mass['hoehe']:.0f}px hoch"

    # Höhe wächst mit dem Inhalt
    ta.fill("\n".join(f"Zeile {i}" for i in range(14)))
    assert ta.evaluate("el => el.getBoundingClientRect().height") > mass["hoehe"] + 40

    # Groß bearbeiten: deckt fast das Fenster, jede Eingabe landet sofort im Baustein
    karte.get_by_role("button", name="Groß bearbeiten").click()
    gross = pg.locator("#gross-feld")
    assert gross.is_visible()
    flaeche = gross.evaluate("el => el.getBoundingClientRect().width * el.getBoundingClientRect().height"
                             " / (innerWidth * innerHeight)")
    assert flaeche > 0.5, f"großes Feld deckt nur {flaeche:.0%} des Fensters"
    gross.fill("Neu\nZweite Zeile")
    # Die Kopfzeile der Karte wird aus dem Baustein-Objekt gebildet
    # (blockSummary) — steht der neue Text dort, ist er im Baustein angekommen.
    assert "Neu" in karte.locator(".block-card-header").inner_text()
    pg.keyboard.press("Escape")
    assert not gross.is_visible()
    assert ta.input_value() == "Neu\nZweite Zeile"
    pg.context.close()


# ── Kampagnen: „Übernahme erzwingen für“ kommt beim Speichern an ─────────────
# Fehlt `fuer` in der Anfrage, greift still die Vorgabe (Postfächer mit
# Banner) — ein gewähltes „ohne Banner“ ginge ohne Meldung verloren.

def test_kampagne_speichert_die_gewaehlten_postfacharten(seite):
    import settings_store
    vorher = settings_store.get("BANNER_CAMPAIGNS")
    settings_store.update({"BANNER_CAMPAIGNS": [
        {"id": "kmp_alt001", "name": "Alt", "banner": "x", "start": "", "end": "",
         "group": "", "enabled": True}]})
    try:
        pg = seite("/kampagnen", breite=1280)
        pg.wait_for_selector("#kampagnen-body button")
        # Altkampagne ohne `fuer`: zeigt die Lesart des Mailwegs
        assert "Richtlinien-Banner, individueller Banner" in pg.inner_text("#kampagnen-body")
        pg.get_by_role("button", name="Bearbeiten").click()
        gesetzt = pg.eval_on_selector_all(".k-fuer", "l => l.filter(c => c.checked).map(c => c.value)")
        assert gesetzt == ["richtlinie", "eigen"]
        pg.locator(".k-fuer[value=richtlinie]").uncheck()
        pg.locator(".k-fuer[value=eigen]").uncheck()
        pg.locator(".k-fuer[value=ohne]").check()
        if not pg.locator("#k-banner option[value=x]").count():
            pg.evaluate("""() => { const o = document.createElement('option');
                o.value = o.textContent = 'x'; document.getElementById('k-banner').appendChild(o);
                document.getElementById('k-banner').value = 'x'; }""")
        pg.get_by_role("button", name="Speichern").click()
        pg.wait_for_function("() => /Gespeichert/.test(document.getElementById('k-result').innerText)")
        gespeichert = settings_store.get("BANNER_CAMPAIGNS")
        assert [c.get("fuer") for c in gespeichert] == [["ohne"]]
        # keine Auswahl: abgelehnt, nichts geschrieben
        pg.get_by_role("button", name="Bearbeiten").click()
        pg.locator(".k-fuer[value=ohne]").uncheck()
        pg.get_by_role("button", name="Speichern").click()
        pg.wait_for_function("() => /mindestens eine/i.test(document.getElementById('k-result').innerText)")
        assert [c.get("fuer") for c in settings_store.get("BANNER_CAMPAIGNS")] == [["ohne"]]
        pg.context.close()
    finally:
        settings_store.update({"BANNER_CAMPAIGNS": vorher or []})


@pytest.mark.parametrize("thema", ["light", "dark"])
def test_gruppen_punkt_in_der_textfarbe_des_chips(seite, thema):
    """Lab 19.11. (Klickliste E, Befund 1): Der Farbpunkt vor Gruppennamen war
    mit dem blassen Chip-Hintergrund gefüllt — hell kaum, dunkel fast nicht zu
    sehen. Er trägt jetzt die Textfarbe des Chips."""
    pg = seite("/mailboxes", breite=1280, thema=thema)
    pg.wait_for_load_state("networkidle")
    r = pg.evaluate("""() => {
      const d = document.createElement('div');
      d.innerHTML = _gruppenPunktHtml('Vertrieb') + _gruppenPunktHtml('Technik');
      document.body.appendChild(d);
      return Array.from(d.children).map(p => {
        const cs = getComputedStyle(p);
        return {bg: cs.backgroundColor, fg: cs.color}; }); }""")
    for p in r:
        assert p["bg"] == p["fg"], f"Punkt nicht in der Textfarbe gefüllt: {p}"


def test_farbfeld_bekommt_nie_base(seite, monkeypatch, tmp_path):
    """Klickliste E, Hinweis 4: Ein Textbaustein mit Farbe „base“ gab den Wert
    an <input type=color> weiter — Konsolenwarnung „does not conform“."""
    import json
    import config
    import signature_engine
    (tmp_path / "farbe.html").write_text("<p>x</p>", encoding="utf-8")
    (tmp_path / "farbe.meta.json").write_text(json.dumps({
        "version": 1, "kind": "signatur", "global": {},
        "blocks": [{"id": "1", "type": "text", "text": "eins", "color": "base"}]}), encoding="utf-8")
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    signature_engine._reload_env()
    pg = seite("/template?name=farbe", breite=1280)
    warnungen = []
    pg.on("console", lambda m: warnungen.append(m.text) if "does not conform" in m.text else None)
    pg.wait_for_selector(".block-card")
    pg.locator(".block-card").first.locator(".block-type-badge").click()
    pg.wait_for_selector(".block-card input[type=color]")
    pg.wait_for_timeout(200)
    # Rohes Attribut, nicht .value: Der Browser bereinigt .value still zu
    # #000000 — gerade daran sah man den Fehler nicht.
    werte = pg.evaluate("() => Array.from(document.querySelectorAll('.block-card input[type=color]'))"
                        ".map(i => i.getAttribute('value'))")
    import re as _re
    assert werte and all(_re.fullmatch(r"#[0-9a-fA-F]{6}", v or "") for v in werte), werte
    assert not warnungen, warnungen
    # Kurzhilfe zu den Variablen steht auch im Baukasten
    assert pg.is_visible("#baukasten-variablen-hilfe")
    assert "custom_b" in pg.text_content("#baukasten-variablen-hilfe")


# ── Baukasten: Schriftart-Auswahl, bearbeiter.-Vorschläge, Vorschau „Schreibt“ ──

def test_schriftart_auswahl_mit_freitext(seite, monkeypatch, tmp_path):
    """Gespeicherte Werte erscheinen richtig (Ladeweg), die Auswahl landet beim
    Speichern im Baustein und in den globalen Einstellungen (Speicherweg)."""
    import json
    import config
    import signature_engine
    (tmp_path / "schrift.html").write_text("<p>x</p>", encoding="utf-8")
    (tmp_path / "schrift.meta.json").write_text(json.dumps({
        "version": 1, "kind": "signatur",
        "global": {"font_family": "Lato, sans-serif"},
        "blocks": [{"id": "1", "type": "text", "text": "eins", "font": "verdana,geneva , sans-serif"},
                   {"id": "2", "type": "text", "text": "zwei", "font": ""}],
    }), encoding="utf-8")
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    signature_engine._reload_env()

    pg = seite("/template?name=schrift", breite=1280)
    gesendet = []

    def speichern(route):
        if route.request.method == "POST":
            gesendet.append(json.loads(route.request.post_data))
            route.fulfill(json={"ok": True})
        else:
            route.continue_()
    pg.route("**/api/templates/schrift/meta", speichern)
    pg.wait_for_selector(".block-card")

    # global: gespeicherter Wert ohne Vorgabe → „Eigene …“ mit dem Wert
    pg.evaluate("() => document.getElementById('g-font-wahl').closest('details').open = true")
    wahl, feld = pg.locator("#g-font-wahl"), pg.locator("#g-font")
    assert wahl.input_value() == "__eigen" and feld.is_visible()
    assert feld.input_value() == "Lato, sans-serif"
    wahl.select_option("Georgia, serif")
    assert not feld.is_visible() and feld.input_value() == "Georgia, serif"

    # Baustein 1: Schreibweise egal, trifft die Vorgabe; Baustein 2: leer = wie global
    karten = pg.locator(".block-card")
    karten.nth(0).locator(".block-type-badge").click()
    karten.nth(1).locator(".block-type-badge").click()
    w1, f1 = karten.nth(0).locator(".schrift-wahl select"), karten.nth(0).locator("input[name=font]")
    w2, f2 = karten.nth(1).locator(".schrift-wahl select"), karten.nth(1).locator("input[name=font]")
    assert w1.input_value() == "Verdana, Geneva, sans-serif" and not f1.is_visible()
    assert w2.input_value() == "" and not f2.is_visible()
    w1.select_option("Tahoma, Geneva, sans-serif")
    w2.select_option("__eigen")
    assert f2.is_visible()
    f2.fill("Lato, Arial, sans-serif")
    f2.dispatch_event("change")

    pg.locator("#save-builder-btn").click()
    for _ in range(50):
        if gesendet:
            break
        pg.wait_for_timeout(100)
    assert gesendet, "nichts gespeichert"
    meta = gesendet[-1]
    assert meta["global"]["font_family"] == "Georgia, serif"
    assert [b.get("font") for b in meta["blocks"]] == ["Tahoma, Geneva, sans-serif",
                                                       "Lato, Arial, sans-serif"]
    pg.context.close()
    signature_engine._reload_env()


def test_vorschlag_kennt_bearbeiter(seite):
    pg = seite("/template", breite=1280)
    pg.evaluate("() => addBlock('text')")
    pg.locator(".block-card").last.locator(".block-type-badge").click()
    ta = pg.locator(".block-card").last.locator("textarea[name=text]")
    ta.click()
    ta.press_sequentially("{{ bearbeiter.disp")
    box = pg.locator("#var-ac")
    box.wait_for(state="visible")
    assert box.inner_text().strip() == "bearbeiter.displayName"
    pg.keyboard.press("Enter")
    assert ta.input_value() == "{{ bearbeiter.displayName"
    pg.context.close()


def test_vorschlag_setzt_klammern(seite):
    """Außerhalb eines Ausdrucks setzt der Vorschlag `{{ … }}` selbst (v1.12.106)."""
    pg = seite("/template", breite=1280)
    pg.evaluate("() => addBlock('text')")
    pg.locator(".block-card").last.locator(".block-type-badge").click()
    ta = pg.locator(".block-card").last.locator("textarea[name=text]")
    ta.click()
    ta.press_sequentially("Von: bearbeiter.disp")
    pg.locator("#var-ac").wait_for(state="visible")
    pg.keyboard.press("Enter")
    assert ta.input_value() == "Von: {{ bearbeiter.displayName }}"
    pg.context.close()


def test_vorschau_schickt_wer_schreibt(seite):
    pg = seite("/template", breite=1280)
    def abfangen(route):
        route.fulfill(json={"html": "<p>x</p>", "banner_html": "", "disclaimer_html": ""})
    pg.route("**/api/preview-data*", abfangen)
    pg.evaluate("""() => {
        for (const id of ['preview-email-sel', 'preview-bearbeiter-sel']) {
            const s = document.getElementById(id);
            for (const a of ['info@example.org', 'max@example.org']) {
                const o = document.createElement('option'); o.value = o.textContent = a; s.appendChild(o);
            }
        }
        document.getElementById('preview-email-sel').value = 'info@example.org';
    }""")
    # Die Vorschau steht in einem iframe (sigVorschau) — auf die Anfrage warten
    with pg.expect_request("**/api/preview-data*") as anfrage:
        pg.locator("#preview-bearbeiter-sel").select_option("max@example.org")
    assert "bearbeiter=max%40example.org" in anfrage.value.url
    with pg.expect_request("**/api/preview-data*") as anfrage:
        pg.locator("#preview-bearbeiter-sel").select_option("")
    assert "email=info%40example.org" in anfrage.value.url
    assert "bearbeiter=" not in anfrage.value.url
    # Seit v1.12.110 nur die bearbeitete Vorlage: kein Fall, keine Herkunft.
    assert "fall=" not in anfrage.value.url
    assert pg.locator("#vorschau-fall, #vorschau-herkunft, #preview-override-box").count() == 0
    link = pg.locator("#richtlinie-link").get_attribute("href")
    assert link.startswith("/richtlinienergebnis?email=info%40example.org")
    pg.context.close()


# ── Richtlinienergebnis: eigene Seite (v1.12.110) ────────────────────────────

@pytest.mark.parametrize("breite,thema", [(1280, "light"), (393, "light"), (393, "dark")])
def test_richtlinienergebnis_zeigt_teile_mit_herkunft(seite, breite, thema):
    """Je Fall die Teile mit Herkunft (festgeschrieben / Kampagne über
    data-art), Gründe als Hinweis, gerenderte Vorschau; bei 393 px ohne
    Querrollen, im Dunkelmodus ohne leuchtende Chips."""
    pg = seite("/richtlinienergebnis", breite=breite, thema=thema)
    teil = lambda slot, name, vorlage, quelle, fest=False, kampagne="": {
        "slot": slot, "name": name, "vorlage": vorlage, "quelle": quelle,
        "fest": fest, "kampagne": kampagne}
    antwort = {"faelle": [
        {"schluessel": "neu", "titel": "Neue Mail", "html": "<p>SIG</p><p>BAN</p>",
         "teile": [teil("sig", "Signatur", "GrpSig", "Gruppe Vertrieb", fest=True),
                   teil("banner", "Banner", "KmpBan", "Kampagne Sommer", kampagne="Sommer")],
         "grund_text": "", "zusatz_text": "", "override": None},
        {"schluessel": "intern", "titel": "Rein interne Mail", "html": "", "teile": [],
         "grund_text": "Rein interne Post leitet das Gateway unverändert weiter.",
         "zusatz_text": "", "override": None}],
        "error": None, "vorlage": ""}
    pg.route("**/api/richtlinienergebnis*", lambda r: r.fulfill(json=antwort))
    pg.evaluate("""() => {
        const s = document.getElementById('rl-email-sel');
        const o = document.createElement('option'); o.value = o.textContent = 'info@example.org';
        s.appendChild(o);
    }""")
    with pg.expect_request("**/api/richtlinienergebnis*") as anfrage:
        pg.locator("#rl-email-sel").select_option("info@example.org")
    assert "email=info%40example.org" in anfrage.value.url
    pg.locator('.rl-fall[data-fall="intern"]').wait_for()
    chips = pg.locator('.rl-fall[data-fall="neu"] .herkunft-chip')
    assert chips.count() == 2
    assert chips.nth(0).inner_text() == "Signatur „GrpSig“ · Gruppe Vertrieb · festgeschrieben"
    assert chips.nth(0).get_attribute("data-art") == "fest"
    assert chips.nth(1).get_attribute("data-art") == "kampagne"
    assert "unverändert" in pg.locator('.rl-fall[data-fall="intern"] .rl-hinweis').inner_text()
    assert "nichts an" in pg.locator('.rl-fall[data-fall="intern"] .rl-leer').inner_text()
    assert pg.locator('.rl-fall[data-fall="neu"] iframe').count() == 1
    assert pg.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth")
    hell = pg.evaluate("""() => {
        const m = getComputedStyle(document.querySelector('.herkunft-chip[data-art=fest]'))
          .backgroundColor.match(/\\d+/g).map(Number);
        return 0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2];
    }""")
    assert (hell < 90) if thema == "dark" else (hell > 200), hell
    pg.context.close()


# ── Dynamische Gruppen: Regel-Editor auf dem Telefon (v1.12.96) ──────────────

def test_regel_editor_vorschau_und_keine_querrolle(seite, monkeypatch, tmp_path):
    """Regel-Editor bei 393 px: Bedingung anlegen → die Vorschau zählt und nennt
    je Mitglied den Grund; die Seite rollt dabei nicht quer."""
    import dynamische_gruppen as dg
    import settings_store
    mc = {"g-1": {"sig": True, "primary": "anna@firma.at", "known_addresses": ["anna@firma.at"]},
          "g-2": {"sig": True, "primary": "bert@firma.at", "known_addresses": ["bert@firma.at"]}}
    werte = {"MAILBOX_CONFIG": mc, "INTERNAL_GROUPS": {"Vertrieb": []},
             "DYNAMIC_GROUPS": {"Vertrieb": {"regel": {"verknuepfung": "alle", "bedingungen": []},
                                             "immer": [], "nie": []}}}
    echt = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: werte[k] if k in werte else echt(k, *a, **kw))
    monkeypatch.setattr(dg, "SCHNAPPSCHUSS", tmp_path / "snap.json")
    monkeypatch.setattr(dg, "STAND", tmp_path / "stand.json")
    nutzer = [{"id": "1", "mail": "anna@firma.at", "department": "Vertrieb"},
              {"id": "2", "mail": "bert@firma.at", "department": "Technik"}]
    dg.secure_io.write_secret_json(dg.SCHNAPPSCHUSS,
                                   {"stand": dg._jetzt_iso(), **dg.zuordnen(nutzer, mc, [])})
    dg.zuruecksetzen()
    try:
        pg = seite("/mailboxes", breite=393)
        knopf = pg.locator('button[data-group="Vertrieb"]', has_text="Regel")
        knopf.wait_for(state="attached", timeout=10000)
        knopf.click()
        pg.locator("#regel-modal-overlay").wait_for(state="visible")
        pg.locator("#regel-modal-overlay button", has_text="+ Bedingung").first.click()
        zeile = pg.locator("#regel-baum .regel-zeile").first
        zeile.locator("select.regel-feld").select_option("department")
        zeile.locator("input.regel-wert").fill("vertrieb")
        pg.wait_for_function(
            "() => /^1 Mitglied /.test(document.getElementById('regel-vorschau-zahl').textContent)",
            timeout=10000)
        assert "anna@firma.at" in pg.locator("#regel-vorschau-liste").inner_text()
        pg.locator("#regel-vorschau-liste summary").first.click()
        assert "Vertrieb" in pg.locator("#regel-vorschau-liste li").first.inner_text()
        breite = pg.evaluate("() => document.documentElement.scrollWidth")
        assert breite <= 393, f"Seite rollt quer: {breite} px"
        pg.context.close()
    finally:
        dg.zuruecksetzen()


# ── Dynamische Gruppen: Personenauswahl für „Immer/Nie dabei“ ───────────────
# Vorher eine <datalist>: auf iOS/Teams-Webview kaum bedienbar, Name und Adresse
# nicht zusammen sichtbar. Jetzt personenAuswahl() in mailboxes.html.

_PA_POSTFAECHER = [
    {"email": "erika.mustermann@firma.at", "name": "Erika Mustermann", "config_key": "g-1", "sig": True},
    {"email": "max.mueller@firma.at", "name": "Max Müller", "config_key": "g-2", "sig": True},
    {"email": "e.beispiel@firma.at", "name": "Eva Beispiel", "config_key": "g-3", "smime": True},
    {"email": "aus@firma.at", "name": "Nicht Aktiviert", "config_key": "g-4"},
] + [{"email": f"sehr.lange.adresse.nummer.{i}@abteilung-mit-langem-namen.firma.at",
      "name": f"Testperson Mit Langem Namen {i}", "config_key": f"l-{i}", "sig": True}
     for i in range(12)]


def _pa_oeffnen(seite, breite=393, thema="light"):
    pg = seite("/mailboxes", breite=breite, thema=thema)
    pg.route("**/api/gruppen/dynamisch/vorschau", lambda r: r.fulfill(
        json={"ok": True, "anzahl": 0, "neu": 0, "weg": 0, "mitglieder": [], "schnappschuss": {}}))
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""p => {
      _mbData = p;
      _dynGroups = {Vertrieb: {regel: {verknuepfung: 'alle', bedingungen: []}, immer: [], nie: []}};
      openRegelModal('Vertrieb'); }""", _PA_POSTFAECHER)
    pg.locator("#regel-modal-overlay").wait_for(state="visible")
    return pg


def _pa_treffer(pg, art):
    return pg.evaluate("""a => Array.from(document.querySelectorAll('#regel-' + a + '-neu-liste .pa-eintrag'))
        .map(e => ({name: (e.querySelector('.pa-name') || {}).textContent,
                    gesperrt: e.getAttribute('aria-disabled') === 'true'}))""", art)


def _chips(pg, art):
    return pg.evaluate("""a => Array.from(document.querySelectorAll('#regel-' + a + ' .regel-chip'))
        .map(c => c.firstChild.textContent)""", art)


def test_personenauswahl_filtert_nach_name_und_adresse(seite):
    pg = _pa_oeffnen(seite)
    feld = pg.locator("#regel-immer-neu")
    feld.fill("MULLER")                    # Name, ohne Umlaut, groß geschrieben
    assert [t["name"] for t in _pa_treffer(pg, "immer")] == ["Max Müller"]
    assert pg.inner_text("#regel-immer-neu-liste .pa-adresse") == "max.mueller@firma.at"
    feld.fill("müll")                      # mit Umlaut
    assert [t["name"] for t in _pa_treffer(pg, "immer")] == ["Max Müller"]
    feld.fill("x mue")                     # Umlaut als ue, über die Leerstelle (Adresse hat dort einen Punkt)
    assert [t["name"] for t in _pa_treffer(pg, "immer")] == ["Max Müller"]
    feld.fill("e.beisp")                   # Teil der Adresse, nicht des Namens
    assert [t["name"] for t in _pa_treffer(pg, "immer")] == ["Eva Beispiel"]
    feld.fill("nicht aktiviert")           # weder Signatur noch S/MIME → nicht wählbar
    assert _pa_treffer(pg, "immer") == []
    pg.context.close()


def test_personenauswahl_enter_waehlt_markierten_eintrag(seite):
    pg = _pa_oeffnen(seite)
    feld = pg.locator("#regel-immer-neu")
    feld.fill("firma.at")                  # viele Treffer: kein Einzeltreffer-Kurzweg
    feld.press("ArrowDown")
    feld.press("ArrowDown")
    feld.press("Enter")
    assert _chips(pg, "immer") == ["max.mueller@firma.at"]
    assert feld.input_value() == ""
    assert pg.is_hidden("#regel-immer-neu-liste")
    # Esc und Klick daneben schließen die Liste, der Dialog bleibt offen
    feld.fill("eva")
    assert pg.is_visible("#regel-immer-neu-liste")
    feld.press("Escape")
    assert pg.is_hidden("#regel-immer-neu-liste")
    assert pg.is_visible("#regel-modal-overlay")
    feld.fill("eva")
    pg.click("#regel-modal-title")
    assert pg.is_hidden("#regel-immer-neu-liste")
    # Tipp auf einen Eintrag
    feld.fill("eva")
    pg.locator("#regel-immer-neu-liste .pa-eintrag").first.click()
    assert _chips(pg, "immer") == ["max.mueller@firma.at", "e.beispiel@firma.at"]
    pg.context.close()


def test_personenauswahl_bereits_gewaehlte_nicht_doppelt(seite):
    pg = _pa_oeffnen(seite)
    immer = pg.locator("#regel-immer-neu")
    immer.fill("erika")
    immer.press("Enter")                   # einziger Treffer → gewählt
    assert _chips(pg, "immer") == ["erika.mustermann@firma.at"]
    # Dieselbe Liste: gesperrt, kein Doppel — weder per Pfeil+Enter, Klick noch Adresse.
    feld = pg.locator("#regel-immer-neu")
    feld.fill("erika")
    assert _pa_treffer(pg, "immer") == [{"name": "Erika Mustermann", "gesperrt": True}]
    feld.press("ArrowDown")
    feld.press("Enter")
    feld.fill("erika")
    pg.locator("#regel-immer-neu-liste .pa-eintrag").first.click(force=True)   # aria-disabled
    feld.fill("erika.mustermann@firma.at")
    feld.press("Enter")
    assert _chips(pg, "immer") == ["erika.mustermann@firma.at"] and _chips(pg, "nie") == []
    # Andere Liste: wählbar, mit Hinweis — und VERSCHIEBT (UI-Review 20.11.2026;
    # bis v1.12.131 gesperrt, während „Hinzufügen“ verschob).
    nie = pg.locator("#regel-nie-neu")
    nie.fill("erika")
    treffer = _pa_treffer(pg, "nie")
    assert len(treffer) == 1 and treffer[0]["gesperrt"] is False and "wechselt von" in treffer[0]["name"]
    nie.press("Enter")
    assert _chips(pg, "immer") == [] and _chips(pg, "nie") == ["erika.mustermann@firma.at"]
    pg.context.close()


@pytest.mark.parametrize("thema", ["light", "dark"])
def test_personenauswahl_bei_393_ohne_querrollen(seite, thema):
    pg = _pa_oeffnen(seite, breite=393, thema=thema)
    feld = pg.locator("#regel-nie-neu")
    feld.fill("langem")
    r = pg.evaluate("""() => {
      const l = document.getElementById('regel-nie-neu-liste');
      const h = l.parentNode.getBoundingClientRect(), b = l.getBoundingClientRect();
      const box = document.querySelector('#regel-modal-overlay > div');
      const eintrag = l.querySelector('.pa-eintrag').getBoundingClientRect().height;
      const m = getComputedStyle(l).backgroundColor.match(/\\d+/g).map(Number);
      return {links: b.left, rechts: b.right, hl: h.left, hr: h.right,
              seite: document.documentElement.scrollWidth,
              box: box.scrollWidth - box.clientWidth, liste: l.scrollWidth - l.clientWidth,
              sichtbar: l.clientHeight / eintrag, rollt: l.scrollHeight > l.clientHeight,
              hell: 0.299 * m[0] + 0.587 * m[1] + 0.114 * m[2]}; }""")
    assert r["links"] >= r["hl"] - 1 and r["rechts"] <= r["hr"] + 1, r
    assert r["rechts"] <= 393, r
    assert r["seite"] <= 393 and r["box"] <= 0 and r["liste"] <= 0, r
    assert 7 <= r["sichtbar"] <= 9 and r["rollt"], r
    assert (r["hell"] < 90) if thema == "dark" else (r["hell"] > 200), r
    pg.context.close()


# ── Personenauswahl als gemeinsamer Baustein (static/personen_auswahl.js) ────
# Seit v1.12.114 auch für statische Gruppen und die zwei Suchfelder in den
# Einstellungen. Deren alte Listen fanden bei Enter keinen Eintrag mehr
# (`div[style*="cursor:pointer"]`, die Einträge hatten längst Klassen).

def test_statische_gruppe_mitglieder_ueber_personenauswahl(seite):
    pg = seite("/mailboxes", breite=393)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""p => {
      _mbData = p;
      _groups = {Vertrieb: ['g-1', 'alt-weg']};      // 'alt-weg': nicht mehr aktiviert
      window._gespeichert = null;
      saveGroups = async () => { window._gespeichert = JSON.parse(JSON.stringify(_groups)); return true; };
      const b = document.createElement('button'); b.dataset.group = 'Vertrieb';
      openMembersModal(b); }""", _PA_POSTFAECHER)
    pg.locator("#members-modal-overlay").wait_for(state="visible")
    chips = lambda: pg.evaluate("""() => Array.from(document.querySelectorAll('#members-modal-list .regel-chip'))
        .map(c => c.firstChild.textContent)""")
    assert chips() == ["erika.mustermann@firma.at", "alt-weg"]   # Bestand bleibt sichtbar
    feld = pg.locator("#members-modal-neu")
    feld.fill("erika")
    assert pg.evaluate("""() => Array.from(document.querySelectorAll('#members-modal-neu-liste .pa-eintrag'))
        .map(e => e.getAttribute('aria-disabled'))""") == ["true"]       # schon Mitglied
    feld.fill("x mue")
    feld.press("Enter")
    assert chips() == ["erika.mustermann@firma.at", "alt-weg", "max.mueller@firma.at"]
    pg.locator("#members-modal-list .regel-chip button").first.click()   # Erika entfernen
    pg.evaluate("saveMembersModal()")
    pg.wait_for_function("window._gespeichert !== null")
    assert pg.evaluate("window._gespeichert.Vertrieb") == ["alt-weg", "g-2"]
    breite = pg.evaluate("() => document.documentElement.scrollWidth")
    assert breite <= 393
    pg.context.close()


def test_einstellungen_empfaenger_ueber_personenauswahl(seite):
    pg = seite("/settings", breite=393)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      _notifAllMailboxes = [
        {email: 'info@firma.at', name: 'Info', type: 'shared'},
        {email: 'max.mueller@firma.at', name: 'Max Müller', type: 'user'},
        {email: 'erika.mustermann@firma.at', name: 'Erika Mustermann', type: 'user'}];
      _notifRecipients = ['info@firma.at']; _renderNotifPicker(); }""")
    feld = pg.locator("#notif-search-input")
    feld.click()                                    # beimFokus: Liste ohne Tippen
    liste = "#notif-search-input-liste"
    assert pg.is_visible(liste)
    assert pg.evaluate(f"""() => Array.from(document.querySelectorAll('{liste} .pa-kopf')).map(e => e.textContent)""") \
        == ["Freigegebene Postfächer", "Benutzer-Postfächer"]
    feld.press("ArrowDown")                         # Info ist schon Empfänger → übersprungen
    feld.press("Enter")
    assert pg.evaluate("_notifRecipients") == ["info@firma.at", "max.mueller@firma.at"]
    feld.fill("erika")
    feld.press("Enter")                             # einziger Treffer
    assert pg.evaluate("_notifRecipients")[-1] == "erika.mustermann@firma.at"
    # Jede Zeile genau einmal: Das Neuzeichnen entfernte die alten Zeilen über
    # eine Klasse, die eine zweite className-Zuweisung überschrieb.
    zeilen = pg.evaluate("""() => Array.from(document.querySelectorAll('#notif-recipients-list .empfaenger-zeile span'))
        .map(e => e.textContent)""")
    assert zeilen == ["info@firma.at", "max.mueller@firma.at", "erika.mustermann@firma.at"]
    pg.context.close()


def test_einstellungen_verwaltung_sucht_auch_in_entra(seite):
    pg = seite("/settings", breite=393)
    pg.route("**/api/entra/users?q=*", lambda r: r.fulfill(
        json={"ok": True, "users": [{"upn": "neu.person@firma.at", "name": "Neue Person"}]}))
    pg.wait_for_load_state("networkidle")
    pg.evaluate("() => { _adminAllUsers = [{upn: 'erika.mustermann@firma.at', name: 'Erika Mustermann'}]; }")
    feld = pg.locator("#admin-search-input")
    feld.click()
    feld.type("neu")
    pg.wait_for_selector("#admin-search-input-liste .pa-eintrag", timeout=3000)
    assert pg.inner_text("#admin-search-input-liste .pa-name") == "Neue Person"
    feld.press("ArrowDown")
    feld.press("Enter")
    assert pg.is_visible("#admin-selected")
    assert pg.inner_text("#admin-selected-name") == "Neue Person"
    assert feld.input_value() == "neu.person@firma.at"
    pg.context.close()


@pytest.mark.parametrize("breite", [1280, 393])
def test_personenauswahl_huelle_so_hoch_wie_das_feld(seite, breite):
    """Die Hülle darf nicht höher sein als ihr Feld: sonst hängt ▼ in einer Lücke
    und die Liste (top:100%) öffnet darunter. Anlass v1.12.114: `flex:1 1 150px`
    in der Spalte `.settings-control` wirkte als HÖHE (150 px statt 34 px)."""
    pg = seite("/settings", breite=breite)
    pg.wait_for_load_state("networkidle")
    masse = pg.evaluate("""() => Array.from(document.querySelectorAll('.pa-huelle')).map(h => {
        const sichtbar = !!(h.offsetParent);
        if (!sichtbar) { let e = h; while (e && e !== document.body) { e.hidden = false; e.style.display = e.style.display === 'none' ? '' : e.style.display; e = e.parentElement; } }
        const f = h.querySelector('input');
        return {id: f && f.id, huelle: Math.round(h.getBoundingClientRect().height),
                feld: Math.round(f.getBoundingClientRect().height)}; })""")
    ids = {m["id"] for m in masse}
    assert {"notif-search-input", "admin-search-input"} <= ids, masse
    for m in masse:
        assert m["feld"] > 0 and m["huelle"] == m["feld"], m
    pg.context.close()


def test_postfachzeile_auswahlen_auf_einer_linie(seite):
    """Eine Auswahl mit Marke „Ausnahme“ darunter macht die Zelle höher. Bis
    v1.12.119 saßen die Auswahlen dadurch je Zelle mittig und standen in einer
    Zeile auf verschiedenen Höhen. Jetzt: alle Auswahlen einer Zeile oben
    bündig, Kästchen auf ihrer Mitte."""
    pg = seite("/mailboxes", breite=1600)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      document.getElementById('mb-table-wrap').style.display = '';
      renderMailboxTable([
        {email: 'a@contoso.example', name: 'A', type: 'user', sig: true, smime: true,
         template: 'Privat', min_template: '', standard: {sig: 'default', min: 'kurz'}},
        {email: 'b@contoso.example', name: 'B', type: 'user', sig: true, smime: false}]); }""")
    m = pg.evaluate("""() => { const tr = document.querySelector('#mb-tbody tr');
        const sels = Array.from(tr.querySelectorAll('select')).filter(s => s.offsetParent);
        const mitte = r => r.top + r.height / 2;
        return {ausnahmen: sels.filter(s => s.dataset.ausnahme === '1').length,
                anzahl: sels.length,
                tops: sels.map(s => Math.round(s.getBoundingClientRect().top)),
                selMitte: mitte(sels[0].getBoundingClientRect()),
                innen: sels.map(s => { const c = getComputedStyle(s);
                    return [s.getBoundingClientRect().height - parseFloat(c.paddingTop) - parseFloat(c.paddingBottom)
                            - parseFloat(c.borderTopWidth) - parseFloat(c.borderBottomWidth), parseFloat(c.fontSize)]; }),
                kaestchen: Array.from(tr.querySelectorAll('input.mb-sig, input.mb-smime'))
                    .map(c => mitte(c.getBoundingClientRect()))}; }""")
    assert m["ausnahmen"] >= 1 and m["anzahl"] - m["ausnahmen"] >= 1, m   # beide Fälle in der Zeile
    assert max(m["tops"]) - min(m["tops"]) <= 1, m
    # Unterstrich und Unterlängen (g, p, y) dürfen nicht abgeschnitten sein: Die
    # feste Höhe mit dem globalen Innenabstand (7 px) liess v1.12.120 nur 14 px
    # Platz für 12-px-Schrift — „Mit_Logo“ erschien als „Mit Logo“.
    for innen, schrift in m["innen"]:
        assert innen >= schrift * 1.4, m["innen"]
    for k in m["kaestchen"]:
        assert abs(k - m["selMitte"]) <= 2, m
    pg.context.close()


@pytest.mark.parametrize("breite", [1280, 393])
def test_gruppenknoepfe_kleben_nicht_aneinander(seite, breite):
    """Die Knöpfe einer Gruppenzeile (Regel, Variablen, Umwandeln) brachen bis
    v1.12.119 in der auf 520 px begrenzten Tabelle untereinander um und
    berührten sich. Jetzt: breit auf einer Linie, schmal mit Abstand."""
    pg = seite("/mailboxes", breite=breite)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      _groups = {'UserMBX': ['a', 'b'], 'SharedMBX': ['c']};
      _dynGroups = {'UserMBX': {regeln: []}};
      _renderGroupsTable(); }""")
    zeilen = pg.evaluate("""() => Array.from(document.querySelectorAll('#groups-tbody tr')).map(tr =>
        Array.from(tr.querySelectorAll('td:nth-child(2) button')).map(b => {
          const r = b.getBoundingClientRect(); return [Math.round(r.top), Math.round(r.bottom), Math.round(r.left), Math.round(r.right)]; }))
        .filter(k => k.length > 1)""")
    assert zeilen, "keine Gruppenzeile mit mehreren Knöpfen"
    for knoepfe in zeilen:
        if breite >= 1280:
            assert len({k[0] for k in knoepfe}) == 1, knoepfe            # eine Linie
        for a in knoepfe:
            for b in knoepfe:
                if a is b:
                    continue
                ueberlappt_waagrecht = a[2] < b[3] and b[2] < a[3]
                if ueberlappt_waagrecht and a[1] <= b[0]:              # a über b
                    assert b[0] - a[1] >= 4, knoepfe
                if a[0] == b[0] and a[3] <= b[2]:                      # a links von b
                    assert b[2] - a[3] >= 4, knoepfe
    pg.context.close()


def test_gruppenregel_addin_in_derselben_zeile(seite):
    """Breite Regelkarte: Die Add-in-Auswahl stand bis v1.12.119 allein in einer
    zweiten Zeile, obwohl Platz war. Jetzt neben Disclaimer & Co."""
    pg = seite("/mailboxes", breite=1600)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      _groups = {'UserMBX': ['a']};
      _customPolicies = [{condition_type: 'group', group_name: 'UserMBX', vorlagen: {}}];
      _renderCustomPoliciesTable(); }""")
    tops = pg.evaluate("""() => Array.from(document.querySelectorAll('.cpol-karte .cpol-slots > .cpol-zeile'))
        .filter(z => z.offsetParent).map(z => [z.dataset.slot, Math.round(z.getBoundingClientRect().top)])""")
    assert any(s == "addin" for s, _ in tops), tops
    assert len({t for _, t in tops}) == 1, tops
    pg.context.close()


def test_postfaecher_hinweistexte_einheitlich_breit(seite):
    """Erklärtexte auf /mailboxes liefen bis v1.12.119 mal über die ganze Breite,
    mal fest auf 640 px, mit zwei Zeilenhöhen. Jetzt eine Regel für alle."""
    pg = seite("/mailboxes", breite=1600)
    pg.wait_for_load_state("networkidle")
    werte = pg.evaluate("""() => Array.from(document.querySelectorAll('.settings-card p.hint'))
        .filter(p => !p.closest('[style*="position:fixed"]'))
        .map(p => { const c = getComputedStyle(p);
                    return [c.maxWidth, c.lineHeight, (p.textContent || '').trim().slice(0, 30)]; })""")
    assert len(werte) >= 8, werte
    assert len({w[0] for w in werte}) == 1, werte
    assert len({w[1] for w in werte}) == 1, werte
    pg.context.close()


def test_postfachfilter_bleiben_nach_neuladen(seite):
    """Gesetzte Filter (Bereich „Filter“) überstehen das Neuladen — je Browser
    gemerkt. Der Suchtext bewusst nicht."""
    pg = seite("/mailboxes", breite=1280)
    pg.wait_for_load_state("networkidle")
    pg.click("#mb-filter-knopf")
    pg.select_option("#mb-sig-filter", "an")
    pg.check("#mb-ausnahme-filter")
    pg.reload()
    pg.wait_for_load_state("networkidle")
    assert pg.input_value("#mb-sig-filter") == "an"
    assert pg.is_checked("#mb-ausnahme-filter")
    assert pg.input_value("#mb-smime-filter") == ""
    assert pg.inner_text("#mb-filter-knopf").startswith("Filter (2)")
    # Aktive gemerkte Filter klappen den Bereich auf — sonst wirkten sie unbemerkt
    # (UI-Review 20.11.2026).
    assert pg.is_visible("#mb-sig-filter")
    # Zurücksetzen wirkt ebenso über das Neuladen hinaus.
    pg.select_option("#mb-sig-filter", "")
    pg.uncheck("#mb-ausnahme-filter")
    pg.reload()
    pg.wait_for_load_state("networkidle")
    assert pg.input_value("#mb-sig-filter") == ""
    assert not pg.is_checked("#mb-ausnahme-filter")
    pg.context.close()


def test_gruppenregel_beschriftung_auf_hoehe_der_auswahl_schmal(seite):
    """393 px: Ist die Add-in-Liste („▶ Alle“) aufgeklappt, stand die Beschriftung
    „Add-in-Auswahl“ mittig neben dem ganzen Block statt neben ihrer Auswahl."""
    pg = seite("/mailboxes", breite=393)
    pg.wait_for_load_state("networkidle")
    pg.evaluate("""() => {
      _groups = {'UserMBX': ['a']};
      _customPolicies = [{condition_type: 'group', group_name: 'UserMBX', vorlagen: {addin: '*'}}];
      _renderCustomPoliciesTable();
      document.querySelectorAll('.cpol-karte details').forEach(d => d.open = true); }""")
    m = pg.evaluate("""() => Array.from(document.querySelectorAll('.cpol-karte .cpol-zeile'))
        .filter(z => z.offsetParent && z.querySelector('select')).map(z => {
          const l = z.querySelector(':scope > label').getBoundingClientRect();
          const s = z.querySelector('select').getBoundingClientRect();
          return [z.dataset.slot, Math.round((l.top + l.bottom) / 2 - (s.top + s.bottom) / 2)]; })""")
    assert any(slot == "addin" for slot, _ in m), m
    for slot, abstand in m:
        assert abs(abstand) <= 4, m
    pg.context.close()


@pytest.mark.parametrize("breite", [1280, 393])
def test_vorlagen_importieren_mit_auswahl(seite, monkeypatch, tmp_path, breite):
    """Import (v1.12.123): Paket prüfen, vorhandene Vorlage wird per Vorgabe
    umbenannt statt überschrieben, fehlerhafte ist nicht wählbar, Ergebnis am Ort."""
    import io
    import json
    import zipfile
    import config
    import signature_engine
    (tmp_path / "signature.html").write_text("<p>Standard</p>", encoding="utf-8")
    (tmp_path / "Kompakt.html").write_text("ALT", encoding="utf-8")
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    signature_engine._reload_env()
    paket = io.BytesIO()
    with zipfile.ZipFile(paket, "w") as z:
        z.writestr("templates/Kompakt.html", "<p>NEU</p>")
        z.writestr("templates/Neu.html", "<p>{{ user.displayName }}</p>")
        z.writestr("templates/Neu.meta.json", json.dumps({"version": 1, "kind": "banner", "blocks": []}))
        z.writestr("templates/Kaputt.html", "{% if %}")
        z.writestr("templates/sub/x.html", "x")
    datei = tmp_path.parent / f"paket-{breite}.zip"
    datei.write_bytes(paket.getvalue())
    pg = seite("/template?name=default", breite=breite)
    pg.click("#import-knopf")
    pg.set_input_files("#import-dateien", str(datei))
    pg.wait_for_selector("#import-tbody tr")
    zeilen = pg.evaluate("""() => Array.from(document.querySelectorAll('#import-tbody tr')).map(tr => ({
        name: tr.dataset.name, an: tr.querySelector('.import-an').checked,
        aus: tr.querySelector('.import-an').disabled,
        aktion: (tr.querySelector('.import-aktion') || {}).value || null,
        ziel: (tr.querySelector('.import-ziel') || {}).value || null}))""")
    z = {r["name"]: r for r in zeilen}
    assert z["Kaputt"]["aus"] and not z["Kaputt"]["an"]
    assert z["Kompakt"]["aktion"] == "umbenennen" and z["Kompakt"]["ziel"] == "Kompakt-2"
    assert z["Neu"]["an"] and z["Neu"]["aktion"] is None
    assert "Unterordner" in pg.inner_text("#import-uebersprungen")
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    pg.click("#import-los")
    pg.wait_for_function("document.getElementById('import-ergebnis').textContent.includes('Importiert')")
    assert (tmp_path / "Kompakt.html").read_text() == "ALT"
    assert (tmp_path / "Kompakt-2.html").read_text() == "<p>NEU</p>"
    assert json.loads((tmp_path / "Neu.meta.json").read_text())["kind"] == "banner"
    assert not (tmp_path / "Kaputt.html").exists()
    pg.context.close()


def test_vorlagen_exportieren_als_zip(seite, monkeypatch, tmp_path):
    import io
    import zipfile
    import config
    import signature_engine
    (tmp_path / "signature.html").write_text("<p>Standard</p>", encoding="utf-8")
    (tmp_path / "Kompakt.html").write_text("<p>K</p>", encoding="utf-8")
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    signature_engine._reload_env()
    pg = seite("/template?name=default", breite=1280)
    pg.click("#export-knopf")
    pg.click("#export-modal button:has-text('Keine')")
    pg.check("#export-liste .export-an[value='Kompakt']")
    with pg.expect_download() as dl:
        pg.click("#export-los")
    pfad = dl.value.path()
    with zipfile.ZipFile(io.BytesIO(open(pfad, "rb").read())) as zf:
        assert sorted(zf.namelist()) == ["LIESMICH.txt", "templates/Kompakt.html"]
    pg.wait_for_function("document.getElementById('export-stand').textContent.includes('1 Vorlage')")
    pg.context.close()


# Basis gegen Erweitert (Neuordnung 20.11.2026): was im einfachen Fall zu
# entscheiden ist, steht ohne den Schalter da; Technik erst mit ihm.
_BASIS_ERWEITERT = {
    "/settings/signature": (["nosig-trigger", "nosig-behaelt", "kette-behaelt", "intern-behaelt"],
                            ["fallback", "loop-header", "sent-items", "skip-duplicate-sig",
                             "skip-sig-in-thread", "sign-internal-only", "antwort-letztes-wort"], "sig"),
    "/settings/smime": (["smime-signing-enabled", "enc-trigger"], ["crl-check", "kv-url"], "smime"),
    "/settings": (["notify-smime-expiry", "notify-cert-renewal"], ["notify-startup"], "benachrichtigungen"),
}


@pytest.mark.parametrize("url", list(_BASIS_ERWEITERT))
def test_basis_und_erweitert(seite, url):
    basis, erweitert, adv = _BASIS_ERWEITERT[url]
    pg = seite(url, breite=1280)
    pg.wait_for_load_state("domcontentloaded")
    pg.evaluate(f"() => {{ try {{ localStorage.removeItem('advsec_{adv}'); }} catch (e) {{}} _initAdv('{adv}'); }}")
    sichtbar = lambda i: pg.evaluate(f"() => {{ const e = document.getElementById('{i}'); return !!(e && e.getClientRects().length); }}")
    assert [i for i in basis if not sichtbar(i)] == [], "fehlt in der Basis"
    assert [i for i in erweitert if sichtbar(i)] == [], "gehört unter Erweitert"
    pg.check(f"#adv-cb-{adv}")
    assert [i for i in erweitert if not sichtbar(i)] == [], "mit Schalter nicht zu sehen"
    pg.context.close()


def test_ews_zugriffsbereich_zugeklappt_bis_eingerichtet(seite):
    """Zustand ausdrücklich setzen: Andere Tests (test_ews_bereich) tragen einen
    Domänencontroller in dieselben Einstellungen ein."""
    import settings_store
    vorher = {k: settings_store.get(k) for k in ("EWS_LDAP_SERVER", "EWS_BEREICH_AUTO")}
    offen = "() => { const d = document.getElementById('ews-bereich'); return d && d.tagName === 'DETAILS' ? d.open : null; }"
    try:
        settings_store.update({"EWS_LDAP_SERVER": "", "EWS_BEREICH_AUTO": False})
        pg = seite("/settings", breite=1280)
        pg.wait_for_load_state("domcontentloaded")
        assert pg.evaluate(offen) is False
        pg.context.close()
        settings_store.update({"EWS_LDAP_SERVER": "dc.firma.at"})
        pg = seite("/settings", breite=1280)
        pg.wait_for_load_state("domcontentloaded")
        assert pg.evaluate(offen) is True               # eingerichtet: offen
        pg.context.close()
    finally:
        settings_store.update(vorher)


def test_uebersicht_zeigt_nur_zeilen_die_hier_gelten(seite):
    """Übersicht-Prüfung 20.11.2026: Zeilen für Funktionen, die nicht an sind,
    standen überall auf 0; die Spalte „Vorjahr“ auch. Schutzzeilen bleiben."""
    import settings_store
    import stats
    schluessel = ("SETUP_COMPLETE", "ADDIN_ENABLED", "MAILBOX_CONFIG", "REINJECT_MODE",
                  "ANTWORT_LETZTES_WORT")
    vorher = {k: settings_store.get(k) for k in schluessel}
    alt_period = stats.get_period
    zeilen = """() => Array.from(document.querySelectorAll('table tbody tr td:first-child')).map(t => t.textContent.trim())"""
    try:
        settings_store.update({"SETUP_COMPLETE": True, "ADDIN_ENABLED": False,
                               "MAILBOX_CONFIG": {}, "REINJECT_MODE": "smtp",
                               "ANTWORT_LETZTES_WORT": "add-in"})
        stats.get_period = lambda *a: {}
        pg = seite("/", breite=1280)
        pg.wait_for_load_state("domcontentloaded")
        z = pg.evaluate(zeilen)
        assert "davon Stapel verhindert" in z and "Verarbeitet" in z and "Fehler (Gateway)" in z
        # v1.12.149: Graph sendMail nur in Graph-Modi, Key Vault nur mit Vault
        for weg in ("S/MIME signiert", "Add-in-Signatur ersetzt", "Abwesenheitsantworten (Gateway)",
                    "Graph sendMail (zugestellt)", "davon über Key Vault"):
            assert weg not in z, weg
        assert pg.evaluate("() => !document.querySelector('th.col-py').getClientRects().length")
        assert pg.evaluate("groesseMb(15151.6)") == "14,8 GB" and pg.evaluate("groesseMb(512)") == "512 MB"
        assert pg.evaluate("groesseMb(376 / 1024)") == "376 KB"
        pg.context.close()
        # Funktion an → Zeilen da; Vorjahr mit Daten → Spalte da
        settings_store.update({"ADDIN_ENABLED": True, "REINJECT_MODE": "graph_plus",
                               "MAILBOX_CONFIG": {"x": {"smime": True}},
                               "ANTWORT_LETZTES_WORT": "gateway"})
        from datetime import datetime as _dt
        stats.get_period = lambda jahr, monat=None: {"processed": 3} if jahr < _dt.now().year or monat else {}
        pg = seite("/", breite=1280)
        pg.wait_for_load_state("domcontentloaded")
        z = pg.evaluate(zeilen)
        for da in ("S/MIME signiert", "Add-in-Signatur ersetzt", "Abwesenheitsantworten (Gateway)",
                   "Graph sendMail (zugestellt)"):
            assert da in z, da
        assert pg.evaluate("() => !!document.querySelector('th.col-py').getClientRects().length")
        pg.context.close()
    finally:
        stats.get_period = alt_period
        settings_store.update(vorher)


def test_self_vorschau_fuehrt_kein_skript_aus(browser, server):
    """Review 20.11.2026: /self setzte die gerenderte Signatur per innerHTML in die
    Seite. Seit Vorlagen aus Paketen importiert werden (v1.12.123), liefe ein
    <img onerror=…> darin im Ursprung der Oberfläche. Jetzt im abgeschotteten
    Rahmen (sig_preview.js, sandbox ohne allow-scripts)."""
    ctx, pg = _self_seite(browser, server, _self_kontext(), 393, [])
    boese = '<p class="sig-test">SIG</p><img src="x" onerror="window.top.__xss=(window.top.__xss||0)+1">'
    pg.unroute("**/api/self/preview*")
    pg.route("**/api/self/preview*", lambda r: r.fulfill(json={
        "ok": True, "oof_html": "<p>OOF</p>" + boese, "sig_html": boese, "banner_html": "", "disclaimer_html": "",
        "neu": {"html": boese}, "antwort": {"html": boese}}))
    pg.reload(wait_until="domcontentloaded")
    pg.wait_for_selector("#inhalt:not([hidden])", timeout=10000)
    pg.click("#r-sig")
    _vorschau_enthaelt(pg, "#sig-vorschau", "SIG")          # gleich, ob im Rahmen oder in der Seite
    pg.wait_for_timeout(800)
    assert pg.evaluate("window.__xss || 0") == 0
    ctx.close()



def test_ohne_gemerkten_filter_bleibt_der_bereich_zu(seite):
    pg = seite("/mailboxes", breite=1280)
    pg.wait_for_load_state("networkidle")
    assert not pg.is_visible("#mb-sig-filter")
    pg.context.close()


def _png(breite, hoehe, rgba=False, rauschen=True):
    """Kleiner PNG-Kodierer (kein Pillow in der Testumgebung)."""
    import os
    import struct
    import zlib
    kanaele = 4 if rgba else 3
    zeilen = bytearray()
    for y in range(hoehe):
        zeilen.append(0)
        if rauschen:
            zeilen += os.urandom(breite * kanaele)
        else:
            px = bytes([30, 80, 160, 0 if (y // 20) % 2 else 255][:kanaele])
            zeilen += px * breite

    def chunk(typ, daten):
        return struct.pack(">I", len(daten)) + typ + daten + struct.pack(">I", zlib.crc32(typ + daten) & 0xffffffff)
    kopf = struct.pack(">IIBBBBB", breite, hoehe, 8, 6 if rgba else 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", kopf)
            + chunk(b"IDAT", zlib.compress(bytes(zeilen), 6)) + chunk(b"IEND", b""))


@pytest.mark.parametrize("art", ["foto", "transparent"])
def test_baukasten_verkleinert_hochgeladene_bilder(seite, monkeypatch, tmp_path, art):
    """Ideenliste 20.11.2026: Bis v1.12.134 lehnte der Baukasten Bilder über 512 KB
    ab und nahm kleinere unverändert — Logos mussten von Hand, oft zu stark,
    verkleinert werden. Jetzt: doppelte Anzeigebreite, Foto → JPG, Transparenz → PNG."""
    import json
    import config
    import signature_engine
    (tmp_path / "bild.html").write_text("<p>x</p>", encoding="utf-8")
    (tmp_path / "bild.meta.json").write_text(json.dumps({
        "version": 1, "kind": "signatur", "global": {},
        "blocks": [{"id": "1", "type": "logo", "url": "", "width": 150}]}), encoding="utf-8")
    monkeypatch.setattr(config, "TEMPLATE_DIR", str(tmp_path))
    signature_engine._reload_env()
    if art == "foto":
        daten, name = _png(1600, 800), "logo.png"            # ~3,8 MB Rauschen
    else:
        daten, name = _png(900, 300, rgba=True, rauschen=False), "logo.png"
    pg = seite("/template?name=bild", breite=1280)
    pg.wait_for_selector(".block-card")
    pg.locator(".block-card").first.locator(".block-type-badge").click()
    pg.set_input_files(".block-card input[type=file][data-bid]",
                       files=[{"name": name, "mimeType": "image/png", "buffer": daten}])
    pg.wait_for_function("(document.querySelector('[data-bild-hinweis]') || {}).textContent"
                         " && document.querySelector('[data-bild-hinweis]').textContent.includes('Verkleinert')",
                         timeout=15000)
    m = pg.evaluate("""async () => { const u = document.querySelector('.block-card img[src^="data:"]').src;
        const i = new Image(); await new Promise(r => { i.onload = r; i.src = u; });
        return {typ: u.slice(5, u.indexOf(';')), breite: i.naturalWidth, hoehe: i.naturalHeight, laenge: u.length}; }""")
    assert m["breite"] == 300, m                          # 2 × Anzeigebreite 150
    assert m["typ"] == ("image/jpeg" if art == "foto" else "image/png"), m
    assert m["laenge"] < 512 * 1024 * 4 / 3, m
    pg.context.close()
