/* Verfassen-Add-in: Gateway-Signatur beim Verfassen einfügen (OnNewMessageCompose —
 * neue Mail, Antwort, Weiterleitung).
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

  function signaturHolen(zugriff, antwort) {
    var adresse = EXO_VERFASSEN.api + '?client=' + encodeURIComponent(plattform());
    if (antwort) {
      adresse += '&art=' + encodeURIComponent(antwort.art);
      if (antwort.konversation) adresse += '&konversation=' + encodeURIComponent(antwort.konversation);
      if (antwort.betreff) adresse += '&betreff=' + encodeURIComponent(antwort.betreff.slice(0, 255));
      if (antwort.an) adresse += '&an=' + encodeURIComponent(antwort.an);
    }
    return fetch(adresse, { headers: { Authorization: 'Bearer ' + zugriff } })
      .then(function (r) {
        if (!r.ok) throw new Error('Gateway antwortet ' + r.status);
        return r.json();
      });
  }

  // Kennzeichnet den Entwurf: Die Antwort-Regel hat gegriffen. Das Gateway
  // übernimmt die Entscheidung beim Versand und zählt sie. Ohne die API
  // (Outlook mobil vor 4.2405) entscheidet es wie bisher selbst.
  function ketteMelden(item) {
    if (!item.internetHeaders || typeof item.internetHeaders.setAsync !== 'function') {
      return Promise.resolve();
    }
    var kopf = {};
    kopf[EXO_VERFASSEN.kettenkopf] = '1';
    return asynk(function (cb) { item.internetHeaders.setAsync(kopf, cb); })
      .catch(function () { /* Zählung ist Beiwerk */ });
  }

  // Betreff und Empfänger des Entwurfs — Rückfall, wenn Outlook keine
  // conversationId liefert (iOS im Antwortentwurf). Fehlt eine API oder
  // scheitert sie, bleibt der Wert leer; das Gateway kommt damit zurecht.
  function feld(obj, umwandeln) {
    if (!obj || typeof obj.getAsync !== 'function') return Promise.resolve('');
    return asynk(function (cb) { obj.getAsync(cb); }).then(umwandeln, function () { return ''; });
  }

  function antwortAngaben(item, art) {
    return Promise.all([
      feld(item.subject, function (v) { return String(v || ''); }),
      feld(item.to, function (v) {
        return (v || []).map(function (e) { return e.emailAddress; }).filter(Boolean)
          .slice(0, 20).join(',');
      })
    ]).then(function (w) {
      return { art: art, konversation: item.conversationId || '', betreff: w[0], an: w[1] };
    });
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
        // Antwort/Weiterleitung: Das Gateway wendet dieselbe Regel an wie beim
        // Versand (volle Signatur beim ersten eigenen Beitrag, danach die
        // Antwort-Signatur) und braucht dafür die Konversation — über deren
        // Kennung oder, wo Outlook keine liefert, über Betreff und Empfänger.
        // Findet das Gateway sie nicht, bleibt der Entwurf leer und es
        // entscheidet beim Versand.
        if (!art) return null;
        var angaben = art.composeType === 'newMail' ? Promise.resolve(null)
          : antwortAngaben(item, art.composeType === 'forward' ? 'forward' : 'reply');
        return angaben.then(function (antwort) {
          return token().then(function (z) { return signaturHolen(z, antwort); });
        }).then(function (d) {
          var html = (d && d.html) || '';
          return (d && d.kette ? ketteMelden(item) : Promise.resolve()).then(function () { return html; });
        }).then(function (html) {
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
