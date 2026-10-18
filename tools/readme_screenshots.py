"""Screenshots für die README — echte Seiten, Beispieldaten (Contoso / Erika Mustermann).

Aufruf:  .venv/bin/python tools/readme_screenshots.py [Zielordner]   (Vorgabe docs/screenshots)
Braucht Playwright mit Chromium. Läuft gegen ein Wegwerf-DATA_DIR; keine echten
Tenant-Daten, das Repository ist öffentlich.
"""
import json, os, shutil, socket, sys, tempfile, threading, time
from pathlib import Path
REPO = Path(__file__).resolve().parents[1]
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else REPO / "docs" / "screenshots")
OUT.mkdir(exist_ok=True)
tmp = Path(tempfile.mkdtemp(prefix="shots-"))
(tmp / "data").mkdir(); shutil.copytree(REPO / "app/seed_templates", tmp / "templates")
os.environ["DATA_DIR"] = str(tmp / "data"); os.environ["TEMPLATE_DIR"] = str(tmp / "templates")
sys.path.insert(0, str(REPO / "app"))
import settings_store, sso, graph_client, signature_engine
settings_store.init() if hasattr(settings_store, "init") else None
settings_store.update({"GATEWAY_NAME": "Contoso Mail-Gateway", "ABWESENHEIT_AKTIV": True,
                       "SELF_SERVICE_ENABLED": True, "ADDIN_ENABLED": True})
sso._get_secret = lambda: "screenshot-geheimnis"
keks = sso.create_session_cookie("admin@contoso.example", role=sso.ROLE_ADMIN)
# ── Beispieldaten: Postfächer, Zertifikate, Relay-Geräte (echte Speicherwege, Wegwerf-Verzeichnis)
import exo_mailboxes, relay_hosts, smime_store, datetime as _dt
from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
LEUTE = [("erika.mustermann", "Erika Mustermann", "UserMailbox"), ("max.muster", "Max Muster", "UserMailbox"),
         ("anna.schmidt", "Anna Schmidt", "UserMailbox"), ("jonas.weber", "Jonas Weber", "UserMailbox"),
         ("lena.fischer", "Lena Fischer", "UserMailbox"), ("tom.becker", "Tom Becker", "UserMailbox"),
         ("support", "Support", "SharedMailbox"), ("buchhaltung", "Buchhaltung", "SharedMailbox"),
         ("besprechung-1og", "Besprechungsraum 1. OG", "RoomMailbox")]
exo_mailboxes.list_mailboxes = lambda refresh=False: [
    {"primary": f"{u}@contoso.example", "display_name": n, "type": t} for u, n, t in LEUTE]
cfg, health = {}, {}
for i, (u, n, t) in enumerate(LEUTE[:8]):
    a = f"{u}@contoso.example"
    cfg[a] = {"sig": True, "smime": i in (0, 1, 2, 6), "use_policy": True}
    health[a] = {"overall": "ok", "last_checked": "2026-10-18T09:00:00Z", "checks": {}}
cfg["erika.mustermann@contoso.example"]["template"] = "Mit_Logo"
settings_store.update({"MAILBOX_CONFIG": cfg, "MAILBOX_HEALTH": health,
                       "SMTP_RELAY_ENABLED": True, "REINJECT_MODE": "smtp"})
jetzt = _dt.datetime.now(_dt.timezone.utc)
ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Beispiel S/MIME CA"),
                     x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Beispiel-CA")])
def zert(adresse, name, tage, alt=0):
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    c = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name),
            x509.NameAttribute(NameOID.EMAIL_ADDRESS, adresse)])).issuer_name(ca_name)
         .public_key(k.public_key()).serial_number(x509.random_serial_number())
         .not_valid_before(jetzt - _dt.timedelta(days=alt)).not_valid_after(jetzt + _dt.timedelta(days=tage))
         .add_extension(x509.SubjectAlternativeName([x509.RFC822Name(adresse)]), False)
         .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), False)
         .sign(ca_key, hashes.SHA256()))
    return c.public_bytes(serialization.Encoding.PEM), k.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
for u, n, tage in (("erika.mustermann", "Erika Mustermann", 340), ("max.muster", "Max Muster", 21),
                   ("anna.schmidt", "Anna Schmidt", 200), ("support", "Support", 300)):
    c, k = zert(f"{u}@contoso.example", n, tage, alt=365 - tage)
    smime_store.store_pem_slot(f"{u}@contoso.example", c, k)
for a, n in (("einkauf@lieferant.example", "Einkauf Lieferant AG"), ("p.meier@kanzlei.example", "Petra Meier")):
    smime_store.store_recipient_cert(a, zert(a, n, 500, alt=30)[0])
for ip, dns, kom in (("192.168.10.21", "drucker-eg.contoso.local", "Multifunktionsdrucker EG (Scan to Mail)"),
                     ("192.168.10.22", "drucker-1og.contoso.local", "Multifunktionsdrucker 1. OG"),
                     ("192.168.20.5", "erp.contoso.local", "ERP-System: Rechnungsversand"),
                     ("192.168.30.40", "nas.contoso.local", "NAS: Sicherungsberichte")):
    relay_hosts.speichern(ip, dns=dns, kommentar=kom)
import uvicorn
from webui.app import app
with socket.socket() as s:
    s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", lifespan="off"))
threading.Thread(target=srv.run, daemon=True).start()
while not srv.started: time.sleep(0.1)
BASE = f"http://127.0.0.1:{port}"

erika = graph_client.UserData(displayName="Erika Mustermann", jobTitle="Leitung Vertrieb",
    companyName="Contoso GmbH", mail="erika.mustermann@contoso.example", phone="+49 30 1234567-0",
    mobilePhone="+49 170 1234567", website="https://contoso.example")
sig, _ = signature_engine.render(erika, os.environ.get("SIG", "Kompakt"))
banner, _ = signature_engine.render(erika, "Banner")
sig_logo, _ = signature_engine.render(erika, "Mit_Logo")
disc, _ = signature_engine.render(erika, "Disclaimer")
oof = ("<p>Vielen Dank für Ihre Nachricht.</p><p>Ich bin <b>vom 20.10.2026 bis 24.10.2026</b> nicht im Büro "
       "und lese meine E-Mails in dieser Zeit nicht. In dringenden Fällen wenden Sie sich bitte an "
       "<a href='mailto:vertrieb@contoso.example' style='color:#0369a1'>vertrieb@contoso.example</a>.</p>"
       "<p style='color:#475569'>Außerdem bin ich vom 15.12.2026 bis 02.01.2027 abwesend.</p>")
min_html = "<p style='font-family:Calibri,Arial;font-size:11pt;margin:0'>Viele Grüße<br><b>Erika Mustermann</b> · Contoso GmbH</p>"
KONTEXT = {"email": "erika.mustermann@contoso.example", "freigeschaltet": True, "abwesenheit_aktiv": True,
  "darf_vorlagen": True, "waehlbar": {"sig": True, "min": True, "oof": True},
  "oof_template": "Abwesenheit", "sig_template": "Mit_Logo", "standard": {"oof": "Abwesenheit", "sig": "Kompakt", "min": "Minimal"}, "wahl": {},
  "listen": {"oof": ["Abwesenheit", "Abwesenheit kurz"], "sig": ["Mit_Logo", "Kompakt"], "min": ["Minimal"]},
  "oof_mit_signatur": True, "oof_mit_banner": False,
  "ooo": {"status": "scheduled", "start": "2026-10-20", "ende": "2026-10-24", "start_zeit": "08:00",
          "ende_zeit": "18:00", "ganztaegig": True},
  "announce_supported": True, "announce": {"an": True, "mode": "anzahl", "x": 3, "privat": True, "extern": False},
  "zugriff": True}
VORSCHAU = {"oof_html": oof, "sig_html": sig, "banner_html": banner, "disclaimer_html": disc, "min_html": min_html}
UEBERSICHT = {"ts": None, "gesamt": 7, "abwesend": 2, "kein_token": False, "ooo_enabled": True, "items": [
  {"upn": "erika.mustermann@contoso.example", "name": "Erika Mustermann", "status": "scheduled", "abwesend": False,
   "zeitraum": "20.10.2026 – 24.10.2026", "oof_template": "Abwesenheit", "kalender": True, "ankuendigung": True, "zugriff": True},
  {"upn": "max.muster@contoso.example", "name": "Max Muster", "status": "alwaysEnabled", "abwesend": True,
   "zeitraum": "", "oof_template": "Abwesenheit", "kalender": None, "ankuendigung": False, "zugriff": True},
  {"upn": "anna.schmidt@contoso.example", "name": "Anna Schmidt", "status": "scheduled", "abwesend": True,
   "zeitraum": "13.10.2026 – 31.10.2026", "oof_template": "Elternzeit", "kalender": True, "ankuendigung": False, "zugriff": True},
  {"upn": "jonas.weber@contoso.example", "name": "Jonas Weber", "status": "disabled", "abwesend": False,
   "zeitraum": "", "oof_template": "Abwesenheit", "kalender": True, "ankuendigung": True, "zugriff": True},
  {"upn": "lena.fischer@contoso.example", "name": "Lena Fischer", "status": "disabled", "abwesend": False,
   "zeitraum": "", "oof_template": "Abwesenheit kurz", "kalender": False, "ankuendigung": False, "zugriff": True},
  {"upn": "support@contoso.example", "name": "Support", "status": "disabled", "abwesend": False,
   "zeitraum": "", "oof_template": "", "kalender": None, "ankuendigung": False, "zugriff": True},
  {"upn": "tom.becker@contoso.example", "name": "Tom Becker", "status": "scheduled", "abwesend": False,
   "zeitraum": "03.11.2026 – 07.11.2026", "oof_template": "Abwesenheit", "kalender": True, "ankuendigung": True, "zugriff": True},
]}

def json_route(daten):
    return lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(daten))

from playwright.sync_api import sync_playwright
with sync_playwright() as pw:
    b = pw.chromium.launch()
    def ctx(w, h, thema="light", admin=False):
        c = b.new_context(viewport={"width": w, "height": h}, color_scheme=thema, device_scale_factor=2, locale="de-DE")
        if admin:
            c.add_cookies([{"name": sso.SESSION_COOKIE, "value": keks, "domain": "127.0.0.1", "path": "/"}])
        c.add_init_script(f"try{{localStorage.setItem('exo-theme','{thema}')}}catch(e){{}}")
        c.route("**/api/self/context", json_route(KONTEXT))
        c.route("**/api/self/preview*", json_route(VORSCHAU))
        c.route("**/api/abwesenheit/uebersicht*", json_route(UEBERSICHT))
        def vd(r):
            html = sig if "template=Kompakt" in r.request.url else sig_logo
            r.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"html": html, "banner_html": banner, "disclaimer_html": disc}))
        c.route("**/api/preview-data*", vd)
        c.route("https://appsforoffice.microsoft.com/**", lambda r: r.fulfill(status=200, content_type="application/javascript", body="window.Office={onReady:function(cb){cb({host:null})}};"))
        return c
    KARTE = "xpath=ancestor::*[contains(@class,'karte') or contains(@class,'card')][1]"
    def element(pg, sel, name):
        pg.locator(sel).first.locator(KARTE).screenshot(path=str(OUT / f"{name}.png")); print("ok", name)
    def bildschirm(pg, name, w, h, y=0):
        pg.screenshot(path=str(OUT / f"{name}.png"), clip={"x": 0, "y": y, "width": w, "height": h}); print("ok", name)
    def seite(w, h, thema, admin, pfad, klick=None):
        c = ctx(w, h, thema, admin); pg = c.new_page()
        pg.goto(BASE + pfad, wait_until="domcontentloaded"); pg.wait_for_timeout(1500)
        if klick: pg.click(klick); pg.wait_for_timeout(800)
        return c, pg
    for f in OUT.glob("*.png"): f.unlink()

    c, pg = seite(1280, 900, "light", True, "/abwesenheit"); element(pg, "#ab-tbody", "abwesenheit-uebersicht"); c.close()
    c, pg = seite(393, 850, "light", False, "/self")
    bildschirm(pg, "self-abwesenheit", 393, 850)
    element(pg, "#vorschau", "self-abwesenheit-vorschau"); c.close()
    c, pg = seite(393, 850, "dark", False, "/self", "#r-sig"); element(pg, "#sig-vorschau", "self-signatur-dunkel"); c.close()
    c, pg = seite(393, 850, "light", False, "/self", "#r-min"); bildschirm(pg, "self-antwort", 393, 850); c.close()
    c, pg = seite(1280, 900, "light", True, "/template?name=Mit_Logo")
    pg.evaluate("""() => { const s = document.getElementById('preview-email-sel');
        const o = new Option('Erika Mustermann', 'erika.mustermann@contoso.example'); s.add(o);
        s.value = o.value; refreshPreview(); }"""); pg.wait_for_timeout(1200)
    bildschirm(pg, "vorlagen-editor", 1280, 820, y=70); c.close()
    def baukasten(vorlage, aufklappen, name):
        c, pg = seite(1280, 1000, "light", True, "/template?name=" + vorlage)
        pg.evaluate("""() => { const s = document.getElementById('preview-email-sel');
            const o = new Option('Erika Mustermann', 'erika.mustermann@contoso.example'); s.add(o);
            s.value = o.value; refreshPreview(); }""")
        karten = pg.locator("#block-list > .block-card > .block-card-header")
        for i in aufklappen: karten.nth(i).click(position={"x": 30, "y": 10})
        pg.wait_for_timeout(1200)
        top = pg.locator("#tab-builder").bounding_box()["y"] - 8
        bildschirm(pg, name, 1280, 820, y=top); c.close()
    baukasten("Mit_Logo", [2], "baukasten-mit-logo")
    baukasten("Kompakt", [2, 6], "baukasten-kompakt")
    c, pg = seite(1440, 900, "light", True, "/mailboxes"); element(pg, "#mb-tbody", "postfaecher"); c.close()
    c, pg = seite(1280, 900, "light", True, "/smime")
    pg.click("text=Alle Zertifikate ausklappen"); pg.wait_for_timeout(600)
    element(pg, "text=Signaturzertifikate für S/MIME-aktivierte Benutzer", "smime-zertifikate")
    pg.locator("table:has-text('einkauf@lieferant.example')").last.screenshot(path=str(OUT / "smime-empfaenger.png")); print("ok smime-empfaenger")
    c.close()
    c, pg = seite(1440, 900, "light", True, "/relay"); element(pg, "#geraete-anzahl", "relay"); c.close()
    b.close()
srv.should_exit = True
