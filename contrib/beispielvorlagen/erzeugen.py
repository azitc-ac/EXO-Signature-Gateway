#!/usr/bin/env python3
"""Beispielvorlagen für die Webseite erzeugen — Baukasten-Daten, HTML, Text, Vorschaubilder.

Die Quelle ist allein die Bausteinliste hier (VORLAGEN). HTML und Text erzeugt
der Renderer des Gateways (`template_builder`), damit die Dateien genau so
aussehen, wie der Baukasten sie beim Speichern schriebe. Vorschaubilder rendert
die Signatur-Engine mit erfundenen Testdaten; fotografiert wird mit Playwright.

Aufruf (aus dem Repo, mit der venv):
    .venv/bin/python contrib/beispielvorlagen/erzeugen.py            # Vorlagen + Bilder
    .venv/bin/python contrib/beispielvorlagen/erzeugen.py --ohne-bilder

Ergebnis:
    contrib/beispielvorlagen/templates/<Name>.html|.txt|.meta.json
    contrib/beispielvorlagen/vorschau/<Name>.png
    contrib/beispielvorlagen/beispielvorlagen.zip   (Importformat, ohne Bilder)

Neutral: kein Firmen- oder Produktname, Logo ist ein grauer Platzhalter,
Adressen unter example.com (für Dokumentation reserviert, RFC 2606).
"""
from __future__ import annotations

import base64
import io
import json
import sys
import tempfile
import zipfile
from pathlib import Path

HIER = Path(__file__).resolve().parent
sys.path.insert(0, str(HIER.parent.parent / "app"))

import template_builder as tb  # noqa: E402

GLOBAL = {"font_family": "Calibri, Arial, sans-serif", "font_size": "10.5pt",
          "base_color": "#1f2937", "muted_color": "#6b7280", "link_color": "#0369a1"}

LOGO_HTML = """<html><body style="margin:0;background:transparent">
<div style="width:150px;height:50px;border-radius:8px;background:#e2e8f0;display:flex;
align-items:center;justify-content:center;font:600 17px Arial,sans-serif;color:#64748b;
letter-spacing:.06em">IHR LOGO</div></body></html>"""


def _id():
    _id.n += 1
    return str(_id.n)


_id.n = 0


def B(typ, **k):
    return {"id": _id(), "type": typ, **k}


def vorlagen(logo: str) -> dict[str, dict]:
    kontakt = [
        B("phone", field="phone", prefix="Tel."),
        B("mobile", field="mobilePhone", prefix="Mobil"),
        B("email_link"),
        B("web_link"),
    ]
    return {
        # 1) Klassisch mit Logo
        "Klassisch_Logo": {"kind": "signatur", "blocks": [
            B("greeting", text="Mit freundlichen Grüßen"),
            B("spacer", height=8),
            B("name_field", bold=True, size="12pt"),
            B("field", field="jobTitle", color="muted"),
            B("field", field="department", color="muted"),
            B("spacer", height=6),
            *kontakt,
            B("address"),
            B("spacer", height=10),
            B("logo", url=logo, width=150, alt="Logo"),
        ]},
        # 2) Zwei Spalten: Logo links, Angaben rechts
        "Zwei_Spalten": {"kind": "signatur", "blocks": [
            B("greeting", text="Viele Grüße"),
            B("spacer", height=8),
            B("two_col", left=[B("logo", url=logo, width=120, alt="Logo")],
              right=[B("name_field", bold=True),
                     B("field", field="jobTitle", color="muted"),
                     B("divider", color="#e2e8f0", margin=6),
                     B("phone", field="phone", prefix="T"),
                     B("mobile", field="mobilePhone", prefix="M"),
                     B("email_link", prefix="E"),
                     B("web_link", prefix="W")],
              divider=True, divider_color="#e2e8f0", gap=16, left_width="auto",
              left_valign="middle", right_valign="top"),
        ]},
        # 3) Kasten mit runden Ecken
        "Kasten_modern": {"kind": "signatur", "blocks": [
            B("greeting", text="Beste Grüße"),
            B("spacer", height=8),
            B("box", width=420, radius=10, border_width=1, border_color="#bfdbfe",
              filled=True, fill_color="#f0f9ff", padding=14, padding_x=18, children=[
                  B("name_field", bold=True, size="12pt", color="#1e40af"),
                  B("field", field="jobTitle", color="muted"),
                  B("spacer", height=6),
                  B("phone", field="phone", prefix="Tel."),
                  B("email_link"),
                  B("web_link"),
              ]),
        ]},
        # 4) Geteiltes Postfach: Person, die schreibt, und das Postfach
        "Geteiltes_Postfach": {"kind": "signatur", "blocks": [
            B("greeting", text="Mit freundlichen Grüßen"),
            B("spacer", height=8),
            B("field", field="bearbeiter.displayName", bold=True),
            B("field", field="bearbeiter.jobTitle", color="muted", bedingung="fuer_postfach"),
            B("field", field="displayName", prefix="für", color="muted", bedingung="fuer_postfach"),
            B("spacer", height=6),
            B("phone", field="phone", prefix="Tel."),
            B("email_link"),
            B("web_link"),
        ]},
        # 5) Kurze interne Signatur
        "Intern_kurz": {"kind": "signatur", "blocks": [
            B("greeting", text="Viele Grüße"),
            B("name_field", bold=False),
            B("field", field="department", color="muted"),
            B("phone", field="phone", prefix="Durchwahl"),
        ]},
        # 6) Antwort-Signatur (ab der 2. eigenen Mail im Verlauf)
        "Antwort_kurz": {"kind": "signatur", "blocks": [
            B("name_field", bold=True),
            B("field", field="jobTitle", color="muted", size="9pt"),
            B("phone", field="phone", prefix="Tel.", size="9pt"),
        ]},
        # 7) Abwesenheit zweisprachig
        "Abwesenheit_de_en": {"kind": "oof", "blocks": [
            B("freetext", html=(
                "<p style=\"margin:0 0 8px\">Vielen Dank für Ihre Nachricht. "
                "{% if oof.zeitraum %}Ich bin {{ oof.zeitraum }} nicht im Büro"
                "{% else %}Ich bin derzeit nicht im Büro{% endif %} und lese meine E-Mails "
                "in dieser Zeit nicht. In dringenden Fällen wenden Sie sich bitte an "
                "<a href=\"mailto:info@example.com\">info@example.com</a>.</p>"
                "<p style=\"margin:0 0 8px\">Thank you for your message. "
                "{% if oof.period %}I am out of the office {{ oof.period }}"
                "{% else %}I am currently out of the office{% endif %} and will not be reading "
                "e-mail during this time. For urgent matters, please contact "
                "<a href=\"mailto:info@example.com\">info@example.com</a>.</p>")),
        ]},
        # 8a) Kampagnen-Banner
        "Kampagne_Banner": {"kind": "banner", "blocks": [
            B("box", width=520, radius=6, border_width=0, filled=True, fill_color="#eff6ff",
              padding=8, padding_x=14, children=[
                  B("badge", label="TERMIN", badge_color="#1e40af", label_color="#ffffff",
                    html="Besuchen Sie uns auf der Fachmesse, 12.–14. März, Halle 3, Stand B12 — "
                         "<a href=\"https://www.example.com/messe\">jetzt Termin vereinbaren</a>"),
              ]),
        ]},
        # 8b) Disclaimer
        "Disclaimer_kurz": {"kind": "disclaimer", "blocks": [
            B("divider", color="#e5e7eb", margin=8),
            B("text", size="8pt", color="muted", text=(
                "Diese Nachricht kann vertrauliche Informationen enthalten. Wenn Sie nicht der "
                "vorgesehene Empfänger sind, informieren Sie bitte den Absender und löschen Sie "
                "die Nachricht. Eine Weitergabe ist nicht gestattet.")),
            B("text", size="8pt", color="muted", text=(
                "This message may contain confidential information. If you are not the intended "
                "recipient, please notify the sender and delete this message.")),
        ]},
    }


def _logo_png(browser) -> str:
    seite = browser.new_page(viewport={"width": 150, "height": 50}, device_scale_factor=2)
    seite.set_content(LOGO_HTML)
    png = seite.screenshot(omit_background=True)
    seite.close()
    return "data:image/png;base64," + base64.b64encode(png).decode()


def _testperson():
    from graph_client import UserData
    return UserData(displayName="Adele Vance", givenName="Adele", surname="Vance",
                    jobTitle="Vertriebsleiterin", department="Vertrieb",
                    companyName="Muster GmbH", mail="adele.vance@example.com",
                    phone="+49 30 1234567-0", mobilePhone="+49 170 1234567",
                    streetAddress="Musterstraße 1", postalCode="10115", city="Berlin",
                    country="Deutschland", website="https://www.example.com")


def _vorschau_html(name: str, art: str, verz: Path) -> str:
    import config
    import signature_engine
    config.TEMPLATE_DIR = str(verz)
    signature_engine._reload_env()
    adele = _testperson()
    extra = None
    if name == "Geteiltes_Postfach":
        from graph_client import UserData
        postfach = UserData(displayName="Kundenservice", mail="service@example.com",
                            phone="+49 30 1234567-100", website="https://www.example.com")
        extra = {"bearbeiter": adele}
        html, _ = signature_engine.render(postfach, name, extra=extra)
    elif art == "oof":
        oof = {k: "" for k in signature_engine.OOF_PLATZHALTER}
        oof.update(zeitraum="vom 22.12.2026 bis 06.01.2027", period="from 22.12.2026 to 06.01.2027")
        html, _ = signature_engine.render(adele, name, extra={"oof": oof, **oof})
    else:
        html, _ = signature_engine.render(adele, name)
    return ("<html><body style=\"margin:0;background:#fff;font-family:Calibri,Arial,sans-serif\">"
            "<div style=\"padding:20px 24px;display:inline-block;max-width:640px\">" + html + "</div></body></html>")


def main() -> None:
    ohne_bilder = "--ohne-bilder" in sys.argv
    ziel = HIER / "templates"
    ziel.mkdir(exist_ok=True)
    browser = pw = None
    if not ohne_bilder:
        from playwright.sync_api import sync_playwright
        pw = sync_playwright().start()
        browser = pw.chromium.launch()
        logo = _logo_png(browser)
    else:
        bestehend = ziel / "Klassisch_Logo.meta.json"
        logo = (json.loads(bestehend.read_text())["blocks"][-1]["url"] if bestehend.exists()
                else "data:image/png;base64,")
    for name, v in vorlagen(logo).items():
        meta = {"version": 1, "kind": v["kind"], "text_eigen": False,
                "global": dict(GLOBAL), "blocks": v["blocks"]}
        (ziel / f"{name}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                                                encoding="utf-8")
        (ziel / f"{name}.html").write_text(tb.render_html(meta), encoding="utf-8")
        (ziel / f"{name}.txt").write_text(tb.render_txt(meta), encoding="utf-8")
        print("Vorlage", name)
    if browser:
        vorschau = HIER / "vorschau"
        vorschau.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            for f in ziel.iterdir():
                (Path(tmp) / f.name).write_bytes(f.read_bytes())
            for name, v in vorlagen(logo).items():
                seite = browser.new_page(viewport={"width": 720, "height": 200}, device_scale_factor=2)
                seite.set_content(_vorschau_html(name, v["kind"], Path(tmp)))
                kasten = seite.locator("body > div")
                kasten.screenshot(path=str(vorschau / f"{name}.png"))
                seite.close()
                print("Vorschau", name)
        browser.close()
        pw.stop()
    puffer = io.BytesIO()
    with zipfile.ZipFile(puffer, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(ziel.iterdir()):
            z.write(f, f"templates/{f.name}")
    (HIER / "beispielvorlagen.zip").write_bytes(puffer.getvalue())
    print("ZIP", HIER / "beispielvorlagen.zip")


if __name__ == "__main__":
    main()
