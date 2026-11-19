/* Stille Anmeldung der Outlook-Add-ins beim Gateway — EINE Quelle für
 *   - das Verfassen-Add-in (Ereignis-Laufzeit, /addin/verfassen/laufzeit.js),
 *   - den Aufgabenbereich des Knopf-Add-ins und den Teams-/Outlook-Tab (/self).
 *
 * Reihenfolge:
 *   1. NAA (MSAL createNestablePublicClientApplication + acquireTokenSilent):
 *      Entra-Zugriffstoken über das Konto, mit dem Outlook angemeldet ist.
 *   2. Exchange-Identitätstoken (Office.context.mailbox.getUserIdentityTokenAsync,
 *      Mailbox 1.1) — nur für Konten auf Exchange LOKAL. Microsoft: NAA kennt
 *      nur Microsoft-Konten und Entra-ID-Identitäten; Exchange-Online-Tokens sind
 *      in allen Tenants abgeschaltet, in Exchange lokal weiter unterstützt
 *      (MS Learn, „Nested app auth requirement sets“ und „Exchange identity
 *      token structure“). accountType „enterprise“ heisst laut MS Learn
 *      („Nested app authentication FAQ“): Postfach auf Exchange lokal.
 *   3. Ein Fenster öffnet dieser Baustein NIE — das entscheidet der Aufrufer
 *      (im Ereignis ist es verboten, im Aufgabenbereich der letzte Weg).
 *
 * Läuft auch in der JavaScript-only-Laufzeit von Classic Outlook: kein DOM,
 * kein window, kein localStorage — nur Office, msal, fetch und Promise.
 */
/* global Office, msal */
var ExoAnmeldung = (function () {
  'use strict';

  // NAA-Fehler, die „hier gibt es kein NAA“ bedeuten: MSAL fand keine
  // NAA-Brücke und scheiterte am Browser-Weg (Classic Outlook, Ereignis-
  // Laufzeit ohne Web Crypto: crypto_nonexistent, gemessen 18.11.2026).
  var NAA_FEHLT = { crypto_nonexistent: true, unsupported_method: true };
  // Konten, die nie ein Exchange-Identitätstoken bekommen (Exchange Online:
  // abgeschaltet; Outlook.com/Gmail: laut MS Learn nicht unterstützt).
  var CLOUD = { office365: true, outlookCom: true, gmail: true };

  function asynk(fn) {
    return new Promise(function (ok, fehler) {
      fn(function (r) {
        if (r.status === Office.AsyncResultStatus.Succeeded) ok(r.value);
        else fehler(new Error((r.error && r.error.message) || 'Office-Aufruf fehlgeschlagen'));
      });
    });
  }

  // isSetSupported('NestedAppAuth', '1.1') — die von Microsoft genannte
  // Laufzeitprüfung (im Outlook-Manifest nicht deklarierbar). Ohne Office-Host
  // (Teams-Tab) oder bei unbekannter Antwort: wie bisher NAA versuchen.
  function naaUnterstuetzt() {
    try { return Office.context.requirements.isSetSupported('NestedAppAuth', '1.1') !== false; }
    catch (e) { return true; }
  }

  function kontoArt() {
    try { return String(Office.context.mailbox.userProfile.accountType || ''); }
    catch (e) { return ''; }                     // Mailbox < 1.6 oder kein Office-Host
  }

  // Gibt es den Rückfall überhaupt? Office-Host mit der Schnittstelle, und kein Cloud-Konto.
  function exchangeMoeglich() {
    var mb = null;
    try { mb = Office.context.mailbox; } catch (e) { return false; }
    if (!mb || typeof mb.getUserIdentityTokenAsync !== 'function') return false;
    return CLOUD[kontoArt()] !== true;
  }

  // Nach einem NAA-Fehler: Exchange-Identitätstoken versuchen?
  function exchangeRueckfall(fehler) {
    if (!exchangeMoeglich()) return false;
    if (kontoArt() === 'enterprise' || !naaUnterstuetzt()) return true;
    return NAA_FEHLT[String((fehler && fehler.errorCode) || '')] === true;
  }

  function fehlerText(e) { return String((e && (e.errorCode || e.message)) || e); }

  // konf: {clientId, authority, scope, identkopf, sitzung?}
  function erstelle(konf) {
    var pca = null, sitzungVersucht = false;

    function naaToken() {
      if (typeof msal === 'undefined' || !konf.clientId || !konf.scope) {
        return Promise.reject(new Error('NAA nicht eingerichtet'));
      }
      if (!pca) {
        pca = msal.createNestablePublicClientApplication({
          auth: { clientId: konf.clientId, authority: konf.authority }
        });
      }
      // Nur still — acquireTokenPopup scheidet aus (im Ereignis verboten).
      return pca.then(function (app) {
        return app.acquireTokenSilent({ scopes: [konf.scope] });
      }).then(function (r) { return r.accessToken; });
    }

    function identitaet(naaFehler) {
      return asynk(function (cb) { Office.context.mailbox.getUserIdentityTokenAsync(cb); })
        .then(function (t) { return { art: 'exchange', wert: t }; }, function (e2) {
          throw new Error((naaFehler ? fehlerText(naaFehler) + '; ' : '') +
                          'Exchange-Identität: ' + ((e2 && e2.message) || e2));
        });
    }

    // {art: 'naa'|'exchange', wert} — oder Ablehnung mit dem Grund.
    function anmeldung() {
      if (!naaUnterstuetzt() && exchangeMoeglich()) return identitaet(null);
      return naaToken().then(function (z) { return { art: 'naa', wert: z }; }, function (e) {
        if (!exchangeRueckfall(e)) throw e;
        return identitaet(e);
      });
    }

    function kopf(a) {
      var k = {};
      if (a.art === 'exchange') k[konf.identkopf] = a.wert;
      else k.Authorization = 'Bearer ' + a.wert;
      return k;
    }

    // Anmeldung → Self-Service-Sitzungsmerkmal (POST konf.sitzung). Je Instanz
    // EIN Versuch — die Seite legt je Aufruf eine an, ein zweiter Aufruf lehnt
    // sofort ab (kein Kreislauf 401 → still → 401 …).
    function sitzung() {
      if (sitzungVersucht) return Promise.reject(new Error('stille Anmeldung schon versucht'));
      sitzungVersucht = true;
      return anmeldung()
        .then(function (a) {
          // ?client= nur fürs Protokoll (Desktop/Web/Handy unterscheidbar).
          var plattform = '';
          try { plattform = String(Office.context.diagnostics.platform || ''); } catch (e) { /* leer */ }
          var ziel = konf.sitzung + (konf.sitzung.indexOf('?') === -1 ? '?' : '&')
                     + 'client=' + encodeURIComponent(plattform);
          return fetch(ziel, { method: 'POST', credentials: 'omit', headers: kopf(a) });
        })
        .then(function (r) {
          if (!r.ok) throw new Error('Gateway antwortet ' + r.status);
          return r.json();
        })
        .then(function (d) {
          if (!d || !d.token) throw new Error('kein Sitzungsmerkmal');
          return d.token;
        });
    }

    return { anmeldung: anmeldung, kopf: kopf, sitzung: sitzung };
  }

  return { erstelle: erstelle, asynk: asynk, naaUnterstuetzt: naaUnterstuetzt,
           exchangeRueckfall: exchangeRueckfall };
}());
