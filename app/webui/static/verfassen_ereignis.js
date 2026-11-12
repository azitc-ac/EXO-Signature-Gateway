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

  // Auf dem Handy gibt es keinen Knopf beim Verfassen (Outlook mobil kennt nur
  // das Start-Ereignis). Die Zeile sagt, welche Signatur eingefügt wurde und wo
  // man sie ändert: in der App unter „Apps“. Am PC steht dafür der Knopf bereit.
  function mobil() {
    var p = plattform();
    return p === 'iOS' || p === 'Android';
  }

  function eingefuegtMelden(vorlage) {
    if (!vorlage || !mobil()) return;
    hinweis('Signatur \u201e' + vorlage + '\u201c eingef\u00fcgt \u2013 \u00e4ndern unter Apps \u2192 ' +
            (EXO_VERFASSEN.app || 'Signatur & Abwesenheit'));
  }

  // Fehler auch ins Gateway-Protokoll — der Hinweis in Outlook ist nur für den
  // Anwender sichtbar. Ein gescheiterter Bericht bleibt folgenlos.
  function melden(text, zugriff, vorlage) {
    try {
      var kopf = { 'Content-Type': 'application/json' };
      if (zugriff) kopf.Authorization = 'Bearer ' + zugriff;
      fetch(EXO_VERFASSEN.meldung, {
        method: 'POST', headers: kopf,
        body: JSON.stringify({ quelle: 'verfassen', text: String(text).slice(0, 300),
                               client: plattform(), vorlage: vorlage || '' })
      }).catch(function () { /* Bericht ist Beiwerk */ });
    } catch (e) { /* Bericht ist Beiwerk */ }
  }

  function tausender(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, '.'); }

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

  // Von-Adresse des Entwurfs (Mailbox 1.7) — weicht sie vom angemeldeten
  // Nutzer ab, schreibt jemand aus einem geteilten Postfach. Fehlt die
  // Schnittstelle oder scheitert sie: leer, alles wie bisher.
  function vonHolen(item) {
    if (!item.from || typeof item.from.getAsync !== 'function') return Promise.resolve('');
    return asynk(function (cb) { item.from.getAsync(cb); })
      .then(function (v) { return (v && v.emailAddress) || ''; }, function () { return ''; });
  }

  // Alle Empfänger (An, Cc, Bcc) — das Gateway entscheidet daraus, ob die
  // interne Signatur gilt (intern.py: Postfach des Tenants oder eigene Domäne).
  function empfaengerHolen(item) {
    function liste(obj) {
      return feld(obj, function (v) {
        return (v || []).map(function (e) { return e.emailAddress; }).filter(Boolean);
      }).then(function (l) { return l || []; });
    }
    return Promise.all([liste(item.to), liste(item.cc), liste(item.bcc)])
      .then(function (l) { return l[0].concat(l[1], l[2]).slice(0, 100).join(','); });
  }

  function signaturHolen(zugriff, antwort, von, empfaenger) {
    var adresse = EXO_VERFASSEN.api + '?client=' + encodeURIComponent(plattform());
    if (von) adresse += '&von=' + encodeURIComponent(von);
    if (empfaenger) adresse += '&empfaenger=' + encodeURIComponent(empfaenger);
    if (antwort) {
      adresse += '&art=' + encodeURIComponent(antwort.art);
      if (antwort.bezug) adresse += '&bezug=' + encodeURIComponent(antwort.bezug);
      if (antwort.konversation) adresse += '&konversation=' + encodeURIComponent(antwort.konversation);
      if (antwort.betreff) adresse += '&betreff=' + encodeURIComponent(antwort.betreff.slice(0, 255));
      if (antwort.an) adresse += '&an=' + encodeURIComponent(antwort.an);
      if (antwort.cc !== null) adresse += '&cc=' + encodeURIComponent(antwort.cc);
    }
    return fetch(adresse, { headers: { Authorization: 'Bearer ' + zugriff } })
      .then(function (r) {
        if (!r.ok) throw new Error('Gateway antwortet ' + r.status);
        return r.json();
      });
  }

  // Kennzeichnet den Entwurf mit der Wahl des Add-ins: 1 = Antwort-Signatur,
  // 0 = volle. Das Gateway vergleicht sie beim Versand mit seiner eigenen
  // Entscheidung (ANTWORT_LETZTES_WORT) und zählt die Wirkung. Ohne die API
  // (Outlook mobil vor 4.2405) bleibt die Signatur des Add-ins unangetastet.
  function ketteMelden(item, kette) {
    if (!item.internetHeaders || typeof item.internetHeaders.setAsync !== 'function') {
      return Promise.resolve();
    }
    var kopf = {};
    kopf[EXO_VERFASSEN.kettenkopf] = kette ? '1' : '0';
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

  function adressliste(v) {
    return (v || []).map(function (e) { return e.emailAddress; }).filter(Boolean)
      .slice(0, 20).join(',');
  }

  function antwortAngaben(item, art) {
    return Promise.all([
      feld(item.subject, function (v) { return String(v || ''); }),
      feld(item.to, adressliste),
      // Cc unterscheidet „Allen antworten“ auf die eigene Mail von „Antworten“
      // auf die des Empfängers. null = nicht lesbar (dann zählt nur An).
      item.cc && typeof item.cc.getAsync === 'function'
        ? asynk(function (cb) { item.cc.getAsync(cb); }).then(adressliste, function () { return null; })
        : Promise.resolve(null)
    ]).then(function (w) {
      // inReplyTo (Mailbox 1.14) nennt die beantwortete Mail genau; wo es
      // fehlt (Outlook mobil bis 1.5), schätzt das Gateway aus den übrigen Angaben.
      var bezug = '';
      try { bezug = item.inReplyTo || ''; } catch (e) { /* ältere Clients */ }
      return { art: art, bezug: bezug, konversation: item.conversationId || '',
               betreff: w[0], an: w[1], cc: w[2] };
    });
  }

  // Nur-Text-Entwurf (etwa Explorer → „Senden an → E-Mail-Empfänger“):
  // setSignatureAsync mit HTML scheitert dort mit InvalidFormatError (MS Learn,
  // Office.Body). Das Gateway signiert reine Textmails beim Versand selbst; eine
  // Textsignatur vom Add-in trüge keine Markierung und stünde dann doppelt drin.
  // Fehlt getTypeAsync oder scheitert es, gilt der Entwurf als HTML wie bisher.
  function nurText(item) {
    if (typeof item.body.getTypeAsync !== 'function') return Promise.resolve(false);
    return asynk(function (cb) { item.body.getTypeAsync(cb); })
      .then(function (typ) { return typ === Office.CoercionType.Text; },
            function () { return false; });
  }

  // Zuletzt gesetzte Signatur dieses Entwurfs (sessionData, Mailbox 1.11 und
  // Outlook mobil) — bei Empfänger- oder Absenderwechsel nur neu setzen, wenn
  // sich die Wahl ändert. Fehlt die API, wird jedes Mal gesetzt.
  var MERKER = 'exoSignatur';
  function gemerkt(item) {
    if (!item.sessionData || typeof item.sessionData.getAsync !== 'function') return Promise.resolve(null);
    return asynk(function (cb) { item.sessionData.getAsync(MERKER, cb); })
      .then(function (v) { return v || ''; }, function () { return ''; });
  }
  function merken(item, wert) {
    if (!item.sessionData || typeof item.sessionData.setAsync !== 'function') return Promise.resolve();
    return asynk(function (cb) { item.sessionData.setAsync(MERKER, wert, cb); })
      .catch(function () { /* Merker ist Beiwerk */ });
  }

  // EIN Ablauf für alle drei Ereignisse:
  //   'start'      — neue Mail, Antwort, Weiterleitung (OnNewMessageCompose)
  //   'empfaenger' — Empfänger geändert (OnMessageRecipientsChanged): interne
  //                  Signatur, solange alle intern sind, sonst die normale
  //   'absender'   — Von-Adresse geändert (OnMessageFromChanged): Signatur des
  //                  geteilten Postfachs
  // Bei den Änderungs-Ereignissen bleibt ein leerer Befund ohne Wirkung (die
  // Signatur bleibt stehen), und gesetzt wird nur, wenn sich die Wahl ändert.
  function signaturSetzen(event, anlass) {
    var item = Office.context.mailbox.item;
    var zugriff = '', vorlage = '';   // für die Fehlermeldung ans Gateway
    var start = anlass === 'start';
    // Das Manifest verlangt nur Mailbox 1.5 (Outlook mobil). Fehlen die
    // Signatur-APIs, still beenden — ein Hinweis hilft dort niemandem.
    if (typeof item.getComposeTypeAsync !== 'function' || !item.body ||
        typeof item.body.setSignatureAsync !== 'function') {
      event.completed();
      return;
    }
    nurText(item)
      .then(function (text) {
        return text ? null : asynk(function (cb) { item.getComposeTypeAsync(cb); });
      })
      .then(function (art) {
        // Antwort/Weiterleitung: Das Gateway wendet dieselbe Regel an wie beim
        // Versand (volle Signatur beim ersten eigenen Beitrag, danach die
        // Antwort-Signatur) und braucht dafür die Konversation — über deren
        // Kennung oder, wo Outlook keine liefert, über Betreff und Empfänger.
        // Findet das Gateway sie nicht, bleibt der Entwurf leer und es
        // entscheidet beim Versand.
        if (!art) return null;
        var antwortArt = art.composeType !== 'newMail';
        var angaben = !antwortArt ? Promise.resolve(null)
          : antwortAngaben(item, art.composeType === 'forward' ? 'forward' : 'reply');
        return angaben.then(function (antwort) {
          return token().then(function (z) {
            zugriff = z;
            return Promise.all([vonHolen(item), empfaengerHolen(item)]).then(function (w) {
              return signaturHolen(z, antwort, w[0], w[1]);
            });
          });
        }).then(function (d) {
          var html = (d && d.html) || '';
          vorlage = (d && d.vorlage) || '';
          if (start && d && d.zu_gross) {
            // Das Gateway hat es protokolliert; hier nur der Hinweis an den Anwender.
            hinweis('Signatur \u201e' + vorlage + '\u201c ist f\u00fcr Outlook zu gro\u00df (' +
                    tausender(d.zu_gross) + ' Zeichen, erlaubt ' + tausender(d.max) +
                    ') \u2013 das Gateway f\u00fcgt sie beim Versand an.');
          }
          // Nur bei einer Antwort, für die das Gateway entschieden hat ("kette"
          // vorhanden) — dann prüft es beim Versand nach (ANTWORT_LETZTES_WORT).
          var entschieden = start && d && typeof d.kette === 'boolean' && antwortArt;
          return (entschieden ? ketteMelden(item, d.kette) : Promise.resolve())
            .then(function () { return { html: html, vorlage: vorlage }; });
        }).then(function (sig) {
          if (!sig.html) return null;
          return gemerkt(item).then(function (alt) {
            if (!start && alt === sig.vorlage) return null;   // Wahl unverändert
            return asynk(function (cb) { item.disableClientSignatureAsync(cb); })
              .catch(function () { /* ohne Client-Signatur weiter */ })
              .then(function () {
                return asynk(function (cb) {
                  item.body.setSignatureAsync(sig.html, { coercionType: Office.CoercionType.Html }, cb);
                });
              })
              .then(function () { return merken(item, sig.vorlage); })
              .then(function () { eingefuegtMelden(sig.vorlage); });
          });
        });
      })
      .catch(function (e) {
        var text = 'Gateway-Signatur nicht eingefügt: ' + ((e && (e.errorCode || e.message)) || e);
        hinweis(text);
        melden(text, zugriff, vorlage);
      })
      .then(function () { event.completed(); });
  }

  function beimVerfassen(event) { signaturSetzen(event, 'start'); }
  function beiEmpfaengern(event) { signaturSetzen(event, 'empfaenger'); }
  function beiAbsender(event) { signaturSetzen(event, 'absender'); }

  // OWA/neues Outlook laden die HTML-Seite und erwarten die Initialisierung.
  if (Office.onReady) Office.onReady(function () {});
  Office.actions.associate('beimVerfassen', beimVerfassen);
  Office.actions.associate('beiEmpfaengern', beiEmpfaengern);
  Office.actions.associate('beiAbsender', beiAbsender);
})();
