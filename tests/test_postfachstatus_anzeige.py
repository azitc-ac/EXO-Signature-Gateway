"""Postfach-Status: Anzeige aller Zustände und Fassung der Outlook-Add-ins.

Zwei Befunde vom 18.10.2026:

* Die Postfachliste kannte „warning“, die Prüfung liefert „warn“ (dazu „fixed“
  und „skip“). Eine Warnung erschien grau mit „?“.
* Ob Exchange einem Postfach die aktuelle Fassung eines Add-ins ausgeliefert
  hat, war nirgends sichtbar. Ein Manifest-Update im Admin Center, das nicht
  bestätigt oder noch nicht verteilt ist, fiel erst auf, als ein Gerät nichts
  tat.
"""
import re
from pathlib import Path

import health_check
import settings_store

WURZEL = Path(__file__).resolve().parents[1]


def test_anzeige_kennt_jeden_zustand_der_pruefung():
    quelle = (WURZEL / "app/health_check.py").read_text("utf-8")
    zustaende = set(re.findall(r'_make_result\(\s*"(\w+)"', quelle))
    assert {"ok", "warn", "error", "skip", "fixed"} <= zustaende, zustaende
    html = (WURZEL / "app/webui/templates/mailboxes.html").read_text("utf-8")
    for tabelle in ("_STATUS_FARBE", "_STATUS_ZEICHEN", "_STATUS_TEXT"):
        m = re.search(r"var " + tabelle + r" = \{([^}]*)\}", html)
        assert m, tabelle
        schluessel = set(re.findall(r"(\w+):", m.group(1)))
        assert zustaende <= schluessel, f"{tabelle} ohne {zustaende - schluessel}"


def test_jeder_pruefpunkt_hat_eine_beschriftung():
    quelle = (WURZEL / "app/health_check.py").read_text("utf-8")
    punkte = set(re.findall(r'checks\["(\w+)"\]\s*=', quelle))
    html = (WURZEL / "app/webui/templates/mailboxes.html").read_text("utf-8")
    m = re.search(r"var _CHECK_LABELS = \{([^}]*)\}", html)
    beschriftet = set(re.findall(r"(\w+):", m.group(1)))
    assert punkte <= beschriftet, punkte - beschriftet


# ── Add-in-Fassung ────────────────────────────────────────────────────────────

VERF = "ac34fa25-4495-575e-ba00-cfd17ccaceb7"
KNOPF = "11111111-2222-3333-4444-555555555555"


def _stand(monkeypatch, aktiv=True, verfassen="1.9.167", knopf="1.9.163"):
    werte = {
        "ADDIN_ENABLED": aktiv,
        "_ADDIN_VERFASSEN_STAND": {"hash": "h", "version": verfassen},
        "_ADDIN_MANIFEST_STAND": {"hash": "h", "version": knopf},
    }
    monkeypatch.setattr(health_check, "_addin_id_aus_adresse",
                        lambda art: VERF if art == "verfassen" else KNOPF)
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get",
                        lambda k, *a, **kw: werte[k] if k in werte else alt(k, *a, **kw))


def test_aeltere_fassung_ist_warnung_mit_weg(monkeypatch):
    _stand(monkeypatch)
    r = health_check._check_addins({VERF: "1.9.164", KNOPF: "1.9.163"})
    assert r["status"] == "warn"
    assert "1.9.164 installiert, aktuell 1.9.167" in r["detail"]
    assert "Integrierte Apps" in r["detail"]


def test_vierstellige_fassung_vergleicht_numerisch(monkeypatch):
    _stand(monkeypatch, verfassen="1.9.146.1")
    assert health_check._check_addins({VERF: "1.9.146", KNOPF: "1.9.163"})["status"] == "warn"
    assert health_check._check_addins({VERF: "1.9.146.1", KNOPF: "1.9.163"})["status"] == "ok"
    # 1.9.99 < 1.9.100 — als Text wäre es umgekehrt
    _stand(monkeypatch, verfassen="1.9.100")
    assert health_check._check_addins({VERF: "1.9.99", KNOPF: "1.9.163"})["status"] == "warn"


def test_aktuell_ist_ok(monkeypatch):
    _stand(monkeypatch)
    r = health_check._check_addins({VERF: "1.9.167", KNOPF: "1.9.163"})
    assert r["status"] == "ok"
    assert "Signatur automatisch: 1.9.167" in r["detail"]


def test_nicht_installiert_ist_neutral(monkeypatch):
    _stand(monkeypatch)
    assert health_check._check_addins({VERF: "", KNOPF: ""})["status"] == "skip"
    teil = health_check._check_addins({VERF: "", KNOPF: "1.9.163"})
    assert teil["status"] == "ok" and "nicht installiert" in teil["detail"]


def test_ohne_addin_oder_abfrage_entfaellt(monkeypatch):
    _stand(monkeypatch, aktiv=False)
    assert health_check._check_addins({VERF: "1.9.164"})["status"] == "skip"
    _stand(monkeypatch)
    assert health_check._check_addins(None)["status"] == "skip"


def test_ps_lauf_fragt_get_app_mit_den_kennungen_ab(monkeypatch, tmp_path):
    """Die Kennungen müssen im Skript ankommen — sonst fragt Get-App nichts ab
    und jede Prüfung meldet still „nicht installiert“."""
    _stand(monkeypatch)
    import config
    monkeypatch.setattr(config, "CLIENT_ID", "app", raising=False)
    zert = tmp_path / "auth.pfx"
    zert.write_bytes(b"x")
    monkeypatch.setattr(health_check, "_AUTH_CERT_PATH", zert)
    alt = settings_store.get
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: (
        "contoso.onmicrosoft.com" if k == "TENANT_DOMAIN" else alt(k, *a, **kw)))
    gesehen = {}

    class Ergebnis:
        returncode = 0
        stderr = ""
        stdout = ('{"erika@contoso.example":{"dg_member":true,"dg_fixed":false,'
                  '"imap_perm":null,"imap_fixed":false,"addins":{"%s":"1.9.164"}}}' % VERF)

    def lauf(cmd, **kw):
        gesehen["skript"] = Path(cmd[-1]).read_text("utf-8")
        gesehen["timeout"] = kw.get("timeout")
        return Ergebnis()
    monkeypatch.setattr(health_check.subprocess, "run", lauf)
    r = health_check._check_exo_batch_sync(["erika@contoso.example"])
    assert "Get-App -Mailbox $email -Identity $aid" in gesehen["skript"]
    assert VERF in gesehen["skript"] and KNOPF in gesehen["skript"]
    assert gesehen["timeout"] >= 180 + 2 * 1 * 2
    assert r["erika@contoso.example"]["addins"] == {VERF: "1.9.164"}


def test_gesamtablauf_traegt_die_addin_fassung_ein(monkeypatch):
    import asyncio
    _stand(monkeypatch)
    monkeypatch.setattr(health_check, "_check_exo_mailbox_sync",
                        lambda e: health_check._make_result("ok", "da"))
    gespeichert = {}
    monkeypatch.setattr(settings_store, "force_update", lambda d: gespeichert.update(d))
    exo = {"erika@contoso.example": {"dg_member": True, "dg_fixed": False, "imap_perm": None,
                                     "imap_fixed": False, "addins": {VERF: "1.9.164", KNOPF: "1.9.163"}}}
    r = asyncio.run(health_check.run_checks_for_mailbox("erika@contoso.example", exo_data=exo))
    assert r["checks"]["addins"]["status"] == "warn"
    assert r["overall"] == "warn"


def test_kennung_kommt_aus_der_eingestellten_aussenadresse(monkeypatch):
    """Dieselbe Formel wie das Manifest, aber aus ADDIN_BASE_URL — nie aus dem
    anonymen Manifest-Abruf (fremder X-Forwarded-Host)."""
    werte = {"ADDIN_ENABLED": True, "ADDIN_BASE_URL": "https://sig.contoso.example",
             "_ADDIN_VERFASSEN_STAND": {"hash": "h", "version": "1.9.167"},
             "_ADDIN_MANIFEST_STAND": {"hash": "h", "version": "1.9.164"}}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    from webui.routen.addin import knopf_addin_id
    from webui.routen.addin_verfassen import addin_id
    soll = {a["name"]: a["id"] for a in health_check._addin_soll()}
    assert soll == {"Signatur automatisch": addin_id("sig.contoso.example"),
                    "Add-in (Knopf)": knopf_addin_id("sig.contoso.example")}


def test_ohne_aussenadresse_keine_abfrage(monkeypatch):
    werte = {"ADDIN_ENABLED": True, "_ADDIN_VERFASSEN_STAND": {"hash": "h", "version": "1.9.167"}}
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))
    assert health_check._addin_soll() == []
