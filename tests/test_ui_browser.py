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
        gestaucht: tabs.filter(t => t.scrollWidth > t.clientWidth + 1)
                       .map(t => t.textContent.trim()),
      };
    }""")
    assert d["zeilen"] == 1, (
        f"Bei {breite}px stehen die Unterreiter in {d['zeilen']} Zeilen.")
    assert d["wischbar"], (
        f"Bei {breite}px lässt sich nicht wischen — die Reiter sind auf die "
        "Breite gestaucht statt überzulaufen (`flex-shrink` fehlt).")
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
        q = parse_qs(urlparse(r.request.url).query)
        mn = (q.get("min") or [""])[0]
        r.fulfill(status=200, content_type="application/json", body=json.dumps({
            "oof_html": "<p>OOF</p>",
            "sig_html": f'<p class="sig-test">SIG {(q.get("sig") or [""])[0]}</p>',
            "min_html": f'<p class="min-test">MIN {mn}</p>' if mn else "",
            "banner_html": "", "disclaimer_html": ""}))
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
    pg.wait_for_selector("#sig-vorschau .sig-test", timeout=5000)
    stempel = pg.evaluate("getComputedStyle(document.querySelector('#p-sig .muster'), '::after').content")
    assert "MUSTER" in stempel
    assert pg.evaluate("!document.querySelector('#sig-vorschau').closest('.muster')"), \
        "Stempel liegt über der Signatur"
    assert pg.is_visible("#sig-auch-oof"), "Hinweis „auch unter der Abwesenheitsnotiz“ fehlt"
    assert pg.is_disabled("#sig-speichern"), "Knopf ohne Änderung muss grau sein"
    pg.select_option("#sig-sel", "Lang")
    pg.wait_for_function("document.querySelector('#sig-vorschau').textContent.includes('Lang')")
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
    pg.wait_for_selector("#min-vorschau .min-test", timeout=5000)
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


@pytest.mark.parametrize("mit_sig", [True, False])
def test_self_abwesenheits_vorschau_wie_in_exchange(browser, server, mit_sig):
    """Die Vorschau der Abwesenheitsnotiz trägt die Signatur nur, wenn das
    Gateway sie anhängt (OOO_APPEND_SIGNATURE) — bis v1.9.161 immer."""
    ctx, pg = _self_seite(browser, server, _self_kontext(oof_mit_signatur=mit_sig), 393, [])
    pg.wait_for_function("document.querySelector('#vorschau').textContent.includes('OOF')")
    assert ("SIG" in pg.text_content("#vorschau")) is mit_sig
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
    pg.wait_for_selector("#sig-vorschau .sig-test", timeout=5000)
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
    ctx.route("**/addin/tab/sitzung", tauschen)

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
