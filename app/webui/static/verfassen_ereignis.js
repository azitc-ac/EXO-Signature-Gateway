/* Verfassen-Add-in: Gateway-Signatur bei NEUER Mail einfügen (OnNewMessageCompose).
 *
 * Wird von /addin/verfassen/laufzeit.js hinter MSAL (globalThis.msal) und der
 * Konfiguration EXO_VERFASSEN ausgeliefert — Classic Outlook lädt nur diese EINE
 * Datei, ohne Importe und ohne Office.onReady (MS Learn, „Activate add-ins with
 * events"). Jeder Weg endet in event.completed(), sonst läuft das Add-in bis zum
 * Zeitlimit (≈300 s) weiter.
 */
/* global Office, msal, EXO_VERFASSEN */
(function () {
  'use strict';
  var HINWEIS_ID = 'exoVerfassen';
  var pca = null;

  function hinweis(text) {
    try {
      Office.context.mailbox.item.notificationMessages.replaceAsync(HINWEIS_ID, {
        type: Office.MailboxEnums.ItemNotificationMessageType.InformationalMessage,
        message: String(text).slice(0, 150),
        icon: 'icon16',
        persistent: false
      });
    } catch (e) { /* Hinweis ist Beiwerk */ }
  }

  function asynk(fn) {
    return new Promise(function (ok, fehler) {
      fn(function (r) {
        if (r.status === Office.AsyncResultStatus.Succeeded) ok(r.value);
        else fehler(new Error((r.error && r.error.message) || 'Office-Aufruf fehlgeschlagen'));
      });
    });
  }

  function token() {
    if (!pca) {
      pca = msal.createNestablePublicClientApplication({
        auth: { clientId: EXO_VERFASSEN.clientId, authority: EXO_VERFASSEN.authority }
      });
    }
    // Nur still: Im Ereignis darf kein Fenster aufgehen (acquireTokenPopup scheidet aus).
    return pca.then(function (app) {
      return app.acquireTokenSilent({ scopes: [EXO_VERFASSEN.scope] });
    }).then(function (r) { return r.accessToken; });
  }

  function plattform() {
    try { return String(Office.context.diagnostics.platform || ''); } catch (e) { return ''; }
  }

  function signaturHolen(zugriff) {
    var adresse = EXO_VERFASSEN.api + '?client=' + encodeURIComponent(plattform());
    return fetch(adresse, { headers: { Authorization: 'Bearer ' + zugriff } })
      .then(function (r) {
        if (!r.ok) throw new Error('Gateway antwortet ' + r.status);
        return r.json();
      }).then(function (d) { return d.html || ''; });
  }

  function beimVerfassen(event) {
    var item = Office.context.mailbox.item;
    // Das Manifest verlangt nur Mailbox 1.5 (Outlook mobil). Fehlen die
    // Signatur-APIs, still beenden — ein Hinweis hilft dort niemandem.
    if (typeof item.getComposeTypeAsync !== 'function' || !item.body ||
        typeof item.body.setSignatureAsync !== 'function') {
      event.completed();
      return;
    }
    asynk(function (cb) { item.getComposeTypeAsync(cb); })
      .then(function (art) {
        // Antworten/Weiterleitungen: das Gateway entscheidet (Antwort-Signatur
        // ab der 2. eigenen Mail im Verlauf) — hier nichts einfügen.
        if (!art || art.composeType !== 'newMail') return null;
        return token().then(signaturHolen).then(function (html) {
          if (!html) return null;
          return asynk(function (cb) { item.disableClientSignatureAsync(cb); })
            .catch(function () { /* ohne Client-Signatur weiter */ })
            .then(function () {
              return asynk(function (cb) {
                item.body.setSignatureAsync(html, { coercionType: Office.CoercionType.Html }, cb);
              });
            });
        });
      })
      .catch(function (e) {
        hinweis('Gateway-Signatur nicht eingefügt: ' + ((e && (e.errorCode || e.message)) || e));
      })
      .then(function () { event.completed(); });
  }

  // OWA/neues Outlook laden die HTML-Seite und erwarten die Initialisierung.
  if (Office.onReady) Office.onReady(function () {});
  Office.actions.associate('beimVerfassen', beimVerfassen);
})();
