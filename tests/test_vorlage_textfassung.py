"""Die Nur-Text-Fassung einer Vorlage entsteht automatisch — außer sie ist
ausdrücklich selbst angepasst (v1.10.89).

ANLASS
------
24.08.2026: `Blog-Banner-Orange.html` lag auf der Produktions-VM ohne `.txt`;
`render()` fiel auf `signature.txt` zurück. Damals wurde der Rückfall nur
sichtbar gemacht. 09.11.2026 zeigte sich, was er bei einem BANNER anrichtet:
Im Nur-Text-Teil stand unter der Signatur die Standardsignatur ein zweites
Mal, vom Blog-Hinweis kein Wort. Und eine handgeschriebene Textfassung
veraltet bei jeder Änderung am HTML, ohne dass es jemand merkt.

Jetzt: Ohne den Haken „Selbst anpassen“ (`text_eigen`) entsteht die
Textfassung beim Versand aus derselben Vorlage — bei Baukasten-Vorlagen aus
den Bausteinen, sonst aus dem HTML. Geprüft wird hier:

1. der Umwandler `html_zu_text` (Zeilen, Links, Bilder, Jinja bleibt stehen),
2. `render()` nimmt nie mehr die Standardsignatur für eine andere Vorlage,
3. die Einordnung des Bestands (`textfassung_eigen` ohne Eintrag),
4. Editor und Baukasten überschreiben keine eigene Fassung, und ein Ab- und
   wieder Anwählen beginnt mit dem frischen Ergebnis,
5. die Änderungszeit der Meta bleibt beim Umschalten unangetastet.
"""
import json
import os
import sys
from pathlib import Path

import pytest

WURZEL = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WURZEL / "app"))

import signature_engine as se  # noqa: E402


@pytest.fixture
def ordner(tmp_path, monkeypatch):
    """Vorlagenverzeichnis umlenken — und die Jinja-Umgebung mitnehmen.

    ⚠️ `_env` hält seinen `FileSystemLoader` samt Pfad fest; ohne Neubau
    rendert ein späterer Test gegen das Verzeichnis eines früheren."""
    monkeypatch.setattr(se.config, "TEMPLATE_DIR", str(tmp_path))
    monkeypatch.setattr(se, "_env", None)
    return tmp_path


class _User:
    custom = {}
    displayName = "Erika Muster"
    phone = "+49 1"
    website = "https://example.org"

    def __getattr__(self, _):
        return ""


def _baukasten_meta(text="Hallo aus dem Baukasten"):
    return {"version": 1, "kind": "banner",
            "blocks": [{"id": "a1", "type": "greeting", "text": text}]}


# ── 1. Umwandler ─────────────────────────────────────────────────────────────

def test_zeilen_zellen_und_quelltext_umbrueche():
    t = se.html_zu_text('<table><tr><td>Eins</td><td>Zwei</td></tr>'
                        '<tr><td>Drei\n   über zwei Quelltextzeilen</td></tr></table>'
                        '<p>Absatz</p><div>Block</div>Text<br>Neue Zeile')
    assert t.splitlines() == ["Eins Zwei", "Drei über zwei Quelltextzeilen",
                              "Absatz", "Block", "Text", "Neue Zeile"]


@pytest.mark.parametrize("html,erwartet", [
    ('<a href="https://blog.example">Zum Blog</a>', "Zum Blog (https://blog.example)"),
    ('<a href="https://blog.example">blog.example</a>', "blog.example"),
    ('<a href="mailto:a@b.de">a@b.de</a>', "a@b.de"),
    ('<a href="tel:{{ user.phone }}">{{ user.phone }}</a>', "{{ user.phone }}"),
    ('<a href="{{ user.website }}">Webseite</a>', "Webseite ({{ user.website }})"),
    ('<a href="#oben">Nach oben</a>', "Nach oben"),
])
def test_links(html, erwartet):
    assert se.html_zu_text(html).strip() == erwartet


def test_bilder_nur_verlinkt_und_unsichtbares_entfaellt():
    t = se.html_zu_text('<img alt="Firmenlogo" src="x.png">'
                        '<a href="https://shop.example"><img alt="Zum Shop" src="b.png"></a>'
                        '<style>p{color:red}</style><script>x()</script><!-- Kommentar -->Rest')
    assert "Firmenlogo" not in t and "color" not in t and "x()" not in t
    assert "Kommentar" not in t
    assert "Zum Shop (https://shop.example)" in t and "Rest" in t


def test_jinja_und_entities_bleiben_text():
    t = se.html_zu_text("{% if user.phone %}<p>Tel &amp; Fax:&nbsp;{{ user.phone }}</p>{% endif %}")
    assert "Tel & Fax: {{ user.phone }}" in t and "{% if user.phone %}" in t


# ── 2. render() ──────────────────────────────────────────────────────────────

def test_banner_ohne_txt_bekommt_seinen_eigenen_text(ordner):
    """Der Fehler vom 09.11.: hier stand vorher die Standardsignatur."""
    (ordner / "signature.html").write_text("<p>Standard</p>", encoding="utf-8")
    (ordner / "signature.txt").write_text("Standard-Text", encoding="utf-8")
    (ordner / "Banner.html").write_text(
        '<p>Tipps &amp; Tools — <a href="https://blog.example">Blog</a></p>', encoding="utf-8")
    html, txt = se.render(_User(), "Banner")
    assert "Standard" not in txt
    assert txt == "Tipps & Tools — Blog (https://blog.example)"      # & nicht maskiert


def test_eingesetzte_werte_werden_nicht_maskiert(ordner):
    """Die Textfassung ist kein HTML: „Müller & Söhne“ darf nicht als
    „Müller &amp; Söhne“ ankommen. Trifft nur EINGESETZTE Werte — fester Text
    der Vorlage wird ohnehin nie maskiert."""
    (ordner / "Sig.html").write_text("<p>{{ user.companyName }}</p>", encoding="utf-8")

    class _Firma(_User):
        companyName = "Müller & Söhne <GmbH>"
    assert se.render(_Firma(), "Sig")[1] == "Müller & Söhne <GmbH>"


def test_auto_text_ohne_leerzeilen_nach_dem_befuellen(ordner):
    (ordner / "Sig.html").write_text(
        "<p>{{ user.displayName }}</p>{% if user.mobilePhone %}<p>Mobil {{ user.mobilePhone }}</p>"
        "{% endif %}<p>Tel {{ user.phone }}</p>", encoding="utf-8")
    assert se.render(_User(), "Sig")[1] == "Erika Muster\nTel +49 1"


def test_eigene_fassung_gilt_unveraendert(ordner):
    (ordner / "Sig.html").write_text("<p>HTML</p>", encoding="utf-8")
    (ordner / "Sig.txt").write_text("Eigen\n\n{{ user.displayName }}", encoding="utf-8")
    (ordner / "Sig.meta.json").write_text(json.dumps({"text_eigen": True}), encoding="utf-8")
    assert se.render(_User(), "Sig")[1] == "Eigen\n\nErika Muster"    # Leerzeile bleibt


def test_fehlende_vorlage_faellt_ganz_auf_die_standardsignatur(ordner):
    (ordner / "signature.html").write_text("<p>Standard</p>", encoding="utf-8")
    html, txt = se.render(_User(), "GibtEsNicht")
    assert "Standard" in html and txt == "Standard"


def test_sandbox_gilt_auch_fuer_die_textfassung(ordner):
    (ordner / "Boese.html").write_text(
        "<p>{{ user.__class__.__mro__[1].__subclasses__() }}</p>", encoding="utf-8")
    assert "subclasses" not in se.render(_User(), "Boese")[1]
    assert "class" not in se.render(_User(), "Boese")[1]


# ── 3. Baukasten und Bestand ─────────────────────────────────────────────────

def test_baukasten_text_aus_den_bausteinen(ordner):
    import template_builder
    meta = _baukasten_meta()
    (ordner / "B.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (ordner / "B.html").write_text(template_builder.render_html(meta), encoding="utf-8")
    os.utime(ordner / "B.html", (1000, 1000))
    assert se.textfassung_auto("B") == template_builder.render_txt(meta)


def test_von_hand_geaendertes_html_gewinnt(ordner):
    (ordner / "B.meta.json").write_text(json.dumps(_baukasten_meta()), encoding="utf-8")
    (ordner / "B.html").write_text("<p>Von Hand neu</p>", encoding="utf-8")
    os.utime(ordner / "B.meta.json", (1000, 1000))
    assert se.quelltext_neuer("B")
    assert se.textfassung_auto("B").strip() == "Von Hand neu"


@pytest.mark.parametrize("txt,eigen", [
    (None, False),                  # keine Datei
    ("  \n", False),                # leer
    ("AUTO", False),                # gleich der automatischen Fassung
    ("Von Hand geschrieben", True),
    ("Tipps\xa0und\n      Tools", False),   # nur Leerraum anders (ältere Baukasten-Fassung)
])
def test_bestand_ohne_eintrag(ordner, txt, eigen):
    (ordner / "V.html").write_text("<p>Tipps und Tools</p>", encoding="utf-8")
    if txt is not None:
        (ordner / "V.txt").write_text(se.html_zu_text("<p>Tipps und Tools</p>") if txt == "AUTO"
                                      else txt, encoding="utf-8")
    assert se.textfassung_eigen("V") is eigen


def test_ausdruecklicher_eintrag_schlaegt_den_vergleich(ordner):
    (ordner / "V.html").write_text("<p>Inhalt</p>", encoding="utf-8")
    (ordner / "V.txt").write_text("Von Hand", encoding="utf-8")
    (ordner / "V.meta.json").write_text(json.dumps({"text_eigen": False}), encoding="utf-8")
    assert not se.textfassung_eigen("V")


# ── 4./5. Umschalten ─────────────────────────────────────────────────────────

def test_festlegen_eigen_und_zurueck(ordner):
    (ordner / "V.html").write_text("<p>Inhalt</p>", encoding="utf-8")
    se.textfassung_festlegen("V", True, "Mein Text")
    assert (ordner / "V.txt").read_text(encoding="utf-8") == "Mein Text"
    assert se.textfassung_quelle("V") == ("Mein Text", True)
    se.textfassung_festlegen("V", False)
    assert not (ordner / "V.txt").exists(), "alte eigene Fassung bleibt liegen"
    assert se.textfassung_quelle("V") == (se.html_zu_text("<p>Inhalt</p>"), False)


def test_umschalten_laesst_die_aenderungszeit_der_meta_stehen(ordner):
    """Sonst verdeckte das Speichern eine Hand-Änderung am HTML."""
    (ordner / "B.meta.json").write_text(json.dumps(_baukasten_meta()), encoding="utf-8")
    (ordner / "B.html").write_text("<p>Von Hand</p>", encoding="utf-8")
    os.utime(ordner / "B.meta.json", (1000, 1000))
    se.textfassung_festlegen("B", False)
    assert json.loads((ordner / "B.meta.json").read_text())["text_eigen"] is False
    assert se.quelltext_neuer("B")


def test_vorschau_im_editor_wie_beim_versand(ordner):
    import template_builder
    meta = _baukasten_meta()
    (ordner / "B.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    html = template_builder.render_html(meta)
    (ordner / "B.html").write_text(html, encoding="utf-8")
    os.utime(ordner / "B.html", (1000, 1000))
    assert se.textfassung_vorschau("B", html) == template_builder.render_txt(meta)
    assert se.textfassung_vorschau("B", "<p>Getippt</p>").strip() == "Getippt"


# ── Durch die Anwendung: Editor und Baukasten ────────────────────────────────

@pytest.fixture
def klient(ordner):
    from fastapi.testclient import TestClient
    import webui.app as wa
    wa.app.dependency_overrides[wa._check_auth] = lambda: "test"
    with TestClient(wa.app) as c:
        yield c
    wa.app.dependency_overrides.clear()


def test_editor_sperrt_das_feld_bei_automatik(klient, ordner):
    (ordner / "V.html").write_text("<p>Inhalt</p>", encoding="utf-8")
    seite = klient.get("/template", params={"name": "V"}).text
    haken = seite[seite.index('id="text_eigen"'):]
    assert "checked" not in haken[:haken.index(">")]
    feld = seite[seite.index('<textarea id="txt_content"'):]
    feld = feld[:feld.index("</textarea>")]
    assert " disabled" in feld and "Inhalt" in feld


def test_quelltext_speichern_mit_und_ohne_haken(klient, ordner):
    (ordner / "V.html").write_text("<p>Inhalt</p>", encoding="utf-8")
    form = {"template_name": "V", "html_content": "<p>Inhalt</p>", "txt_content": "Eigen"}
    klient.post("/template", data={**form, "text_eigen": "1"}, follow_redirects=False)
    assert (ordner / "V.txt").read_text(encoding="utf-8") == "Eigen"
    klient.post("/template", data=form, follow_redirects=False)        # Haken weg
    assert not (ordner / "V.txt").exists()
    assert not se.textfassung_eigen("V")


def test_quelltext_speichern_schreibt_unveraendertes_html_nicht(klient, ordner):
    (ordner / "V.html").write_text("<p>Inhalt</p>", encoding="utf-8")
    os.utime(ordner / "V.html", (1000, 1000))
    klient.post("/template", data={"template_name": "V", "html_content": "<p>Inhalt</p>"},
                follow_redirects=False)
    assert os.stat(ordner / "V.html").st_mtime == 1000


@pytest.mark.parametrize("eigen", [True, False])
def test_baukasten_speichern_ueberschreibt_keine_eigene_fassung(klient, ordner, eigen):
    (ordner / "B.html").write_text("<p>alt</p>", encoding="utf-8")
    (ordner / "B.txt").write_text("Meine Fassung", encoding="utf-8")
    (ordner / "B.meta.json").write_text(json.dumps({**_baukasten_meta(), "text_eigen": eigen}),
                                        encoding="utf-8")
    d = klient.post("/api/templates/B/meta", json=_baukasten_meta("Neu")).json()
    assert d["ok"] and d["text_eigen"] is eigen
    if eigen:
        assert (ordner / "B.txt").read_text(encoding="utf-8") == "Meine Fassung"
        assert d["txt"] == "Meine Fassung"
    else:
        assert not (ordner / "B.txt").exists()
        assert "Neu" in d["txt"]


def test_baukasten_speichern_haelt_bestand_als_eigen_fest(klient, ordner):
    """Ohne Eintrag VOR dem Schreiben eingeordnet: Danach verglichen, erschiene
    die alte, nur vom Baukasten erzeugte .txt neben den neuen Bausteinen als
    „eigen“ — und die Textfassung bliebe für immer auf dem alten Stand."""
    import template_builder
    alt = _baukasten_meta("Alt")
    (ordner / "B.html").write_text(template_builder.render_html(alt), encoding="utf-8")
    (ordner / "B.txt").write_text(template_builder.render_txt(alt), encoding="utf-8")
    (ordner / "B.meta.json").write_text(json.dumps(alt), encoding="utf-8")
    klient.post("/api/templates/B/meta", json=_baukasten_meta("Neu"))
    assert not se.textfassung_eigen("B")
    assert "Neu" in se.textfassung_quelle("B")[0]


def test_endpunkt_liefert_die_vorschau(klient, ordner):
    d = klient.post("/api/templates/textfassung",
                    json={"name": "V", "html": "<p>Getippt</p>"}).json()
    assert d == {"ok": True, "txt": "Getippt\n"}


def test_baukasten_freitext_ueber_denselben_umwandler():
    """Der Blog-Banner auf dem Raspi (09.11.): Quelltext-Umbruch mit Einrückung
    und &nbsp; standen mitten im Satz der Textfassung."""
    import template_builder
    meta = {"version": 1, "blocks": [{"type": "freetext", "html":
            "💡 <strong>Schon gewusst?</strong>&nbsp; Tipps &amp; Tools zu Exchange&nbsp;Online —\n"
            "      <a href=\"https://blog.zarenko.net\">blog.zarenko.net</a>&lt;b&gt;"}]}
    assert template_builder.render_txt(meta).strip() == (
        "💡 Schon gewusst? Tipps & Tools zu Exchange Online — blog.zarenko.net<b>")


@pytest.mark.parametrize("txt_zeit,eigen", [(1000, False), (5000, True)])
def test_bestand_baukasten_nach_zeitstempel(ordner, txt_zeit, eigen):
    """Der Baukasten schrieb .txt und Meta immer zusammen. Weicht eine
    gleichzeitig geschriebene .txt ab, hat sich nur seine Ausgabe geändert
    (Raspi 09.11.: „Demo-Banner“) — automatisch. Nach der Meta geändert und
    abweichend: von Hand — eigen."""
    import template_builder
    meta = _baukasten_meta("Neu")
    (ordner / "B.meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (ordner / "B.html").write_text(template_builder.render_html(meta), encoding="utf-8")
    (ordner / "B.txt").write_text("Alte Ausgabe des Baukastens", encoding="utf-8")
    for datei, zeit in (("B.meta.json", 1000), ("B.html", 1000), ("B.txt", txt_zeit)):
        os.utime(ordner / datei, (zeit, zeit))
    assert se.textfassung_eigen("B") is eigen
