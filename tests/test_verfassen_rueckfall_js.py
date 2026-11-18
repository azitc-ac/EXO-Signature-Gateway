"""Verfassen-Add-in im Client: NAA zuerst, Exchange-Identitätstoken nur als Rückfall.

Das echte Skript `verfassen_ereignis.js` läuft in Node gegen nachgespielte
Office-, MSAL- und fetch-Schnittstellen. Geprüft:

* Konto in Exchange Online, NAA klappt → Bearer, nie ein Identitätstoken.
* Konto auf Exchange lokal, MSAL scheitert mit `crypto_nonexistent` (gemessen
  18.11.2026, klassisches Outlook) → Identitätstoken im Kopf
  X-Exchange-Identitaet, Signatur wird eingefügt.
* accountType office365 → nie der Rückfall, auch nicht bei crypto_nonexistent.
* Andere NAA-Fehler bei unterstütztem NAA → kein Rückfall (Meldung wie bisher).
* NAA laut isSetSupported nicht vorhanden → Rückfall bei jedem NAA-Fehler.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SKRIPT = Path(__file__).resolve().parents[1] / "app/webui/static/verfassen_ereignis.js"

HARNESS = r"""
var fall = JSON.parse(process.argv[1]);
var protokoll = { abrufe: [], identitaet: 0, gesetzt: null, hinweis: null, naa: 0 };
var handler = {};
function ok(cb, v) { cb({ status: 'succeeded', value: v }); }
globalThis.Office = {
  AsyncResultStatus: { Succeeded: 'succeeded' },
  CoercionType: { Html: 'html', Text: 'text' },
  MailboxEnums: { ItemNotificationMessageType: { InformationalMessage: 'info' } },
  actions: { associate: function (n, f) { handler[n] = f; } },
  context: {
    diagnostics: { platform: 'PC' },
    requirements: { isSetSupported: function (n, v) {
      return n === 'NestedAppAuth' ? fall.naaSet : true; } },
    mailbox: {
      userProfile: fall.accountType === null ? {} : { accountType: fall.accountType },
      getUserIdentityTokenAsync: function (cb) { protokoll.identitaet++; ok(cb, 'EXCH.TOKEN.SIG'); },
      item: {
        getComposeTypeAsync: function (cb) { ok(cb, { composeType: 'newMail' }); },
        disableClientSignatureAsync: function (cb) { ok(cb); },
        body: {
          getTypeAsync: function (cb) { ok(cb, 'html'); },
          setSignatureAsync: function (html, o, cb) { protokoll.gesetzt = html; ok(cb); }
        },
        from: { getAsync: function (cb) { ok(cb, { emailAddress: 'max.mustermann@firma.at' }); } },
        to: { getAsync: function (cb) { ok(cb, []); } },
        cc: { getAsync: function (cb) { ok(cb, []); } },
        bcc: { getAsync: function (cb) { ok(cb, []); } },
        notificationMessages: { replaceAsync: function (id, m) { protokoll.hinweis = m.message; } }
      }
    }
  }
};
globalThis.msal = { createNestablePublicClientApplication: function () {
  protokoll.naa++;
  return Promise.resolve({ acquireTokenSilent: function () {
    if (fall.naaFehler) { var e = new Error('MSAL'); e.errorCode = fall.naaFehler; return Promise.reject(e); }
    return Promise.resolve({ accessToken: 'NAA.TOKEN' });
  } });
} };
globalThis.EXO_VERFASSEN = { api: 'https://sig.firma.at/api/addin/verfassen/signatur',
  meldung: 'https://sig.firma.at/api/addin/meldung', identkopf: 'X-Exchange-Identitaet',
  clientId: 'CID', authority: 'A', scope: 'S', kettenkopf: 'X-K', fassung: '1' };
globalThis.fetch = function (url, opt) {
  protokoll.abrufe.push({ url: url, kopf: (opt && opt.headers) || {} });
  return Promise.resolve({ ok: true, status: 200,
    json: function () { return Promise.resolve({ html: '<p>SIG</p>', vorlage: 'Voll' }); } });
};
require(fall.skript);
handler.beimVerfassen({ completed: function () {
  process.stdout.write(JSON.stringify(protokoll));
} });
"""


def _lauf(**fall):
    node = shutil.which("node")
    if not node:
        pytest.skip("node fehlt (in der CI vorhanden)")
    f = {"naaSet": True, "naaFehler": None, "accountType": "office365", "skript": str(SKRIPT)}
    f.update(fall)
    out = subprocess.run([node, "-e", HARNESS, json.dumps(f)], capture_output=True,
                         text=True, timeout=30, check=True)
    return json.loads(out.stdout)


def _signatur_abrufe(p):
    return [a for a in p["abrufe"] if "/verfassen/signatur" in a["url"]]


def test_cloud_naa_wie_bisher():
    p = _lauf()
    assert p["identitaet"] == 0 and p["gesetzt"] == "<p>SIG</p>"
    (a,) = _signatur_abrufe(p)
    assert a["kopf"] == {"Authorization": "Bearer NAA.TOKEN"}


def test_lokal_crypto_nonexistent_nimmt_das_identitaetstoken():
    p = _lauf(accountType="enterprise", naaFehler="crypto_nonexistent")
    assert p["naa"] == 1 and p["identitaet"] == 1          # NAA zuerst, dann Rückfall
    (a,) = _signatur_abrufe(p)
    assert a["kopf"] == {"X-Exchange-Identitaet": "EXCH.TOKEN.SIG"}
    assert "Authorization" not in a["kopf"]
    assert p["gesetzt"] == "<p>SIG</p>"


def test_ohne_accounttype_crypto_nonexistent_nimmt_den_rueckfall():
    """Mailbox < 1.6 kennt accountType nicht — der Rückfall bleibt möglich,
    das Gateway entscheidet (nur Hybrid-Nutzer)."""
    p = _lauf(accountType=None, naaFehler="crypto_nonexistent")
    assert p["identitaet"] == 1 and p["gesetzt"] == "<p>SIG</p>"


@pytest.mark.parametrize("art", ["office365", "outlookCom", "gmail"])
def test_cloud_konto_nie_rueckfall(art):
    p = _lauf(accountType=art, naaFehler="crypto_nonexistent", naaSet=False)
    assert p["identitaet"] == 0 and p["gesetzt"] is None
    assert _signatur_abrufe(p) == []
    assert "crypto_nonexistent" in p["hinweis"]             # Meldung wie bisher


def test_anderer_naa_fehler_kein_rueckfall():
    p = _lauf(accountType="enterprise", naaFehler="interaction_required")
    assert p["identitaet"] == 0 and p["gesetzt"] is None
    assert "interaction_required" in p["hinweis"]


def test_naa_nicht_unterstuetzt_rueckfall_bei_jedem_fehler():
    p = _lauf(accountType="enterprise", naaFehler="no_network_connectivity", naaSet=False)
    assert p["identitaet"] == 1 and p["gesetzt"] == "<p>SIG</p>"
