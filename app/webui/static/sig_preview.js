/* Signatur-Vorschau in einem eigenen Dokument.
 *
 * WARUM EIN IFRAME
 * ----------------
 * Die Vorschau zeigt E-Mail-HTML, das beim Empfänger auf weißem Grund und
 * ohne unser Stylesheet erscheint. Wurde es per innerHTML in die Seite
 * gesetzt, griffen die Dark-Mode-Regeln hinein: `color:#1f2937` (Grundfarbe
 * einer Vorlage) wurde zu `#cbd5e1`, `color:#6b7280` (gedämpft) zu `#94a3b8`.
 * Die Grundfarbe erschien damit HELLER als die gedämpfte — die Vorschau
 * widersprach der Einstellung, und der Fehler schien in der Vorlage zu
 * liegen. Ein eigenes Dokument erbt nichts davon.
 *
 * Zweiter Grund: `sandbox` ohne `allow-scripts`. Ein Freitext-Block enthält
 * rohes HTML; über innerHTML führte etwa ein `<img onerror=…>` seinen Code in
 * der Oberfläche aus. Im Rahmen läuft nichts davon. `allow-same-origin`
 * bleibt, sonst käme man an die Höhe des Inhalts nicht heran.
 *
 * LINKS IN EINEM NEUEN TAB (seit v1.12.175)
 * -----------------------------------------
 * Ein Klick auf einen Link der Vorschau öffnete die Seite IM Rahmen. Jetzt geht
 * er in einen neuen Tab — für ALLE Vorschauen mit gleichem Ursprung über
 * `vorschauLinksNeuerTab()` und `VORSCHAU_SANDBOX` (Warteschlange, Selbsttest,
 * Portal laden diese Datei dafür mit). Die Vorlage selbst bleibt unverändert;
 * umgeschrieben wird nur das angezeigte Dokument.
 *
 * ⚠️ Warum nicht einfach <base target="_blank">: Der neue Tab muss die Sandbox
 * verlassen (`allow-popups-to-escape-sandbox`), sonst laufen dort keine Skripte
 * und viele Seiten bleiben leer. Mit `allow-same-origin` hätte ein Tab, der aus
 * einem `javascript:`-Link entsteht, dann den Ursprung des Gateways — eine
 * fremde Mail in Warteschlange oder Portal könnte so Code in der Oberfläche
 * ausführen. Deshalb bekommen NUR http(s)- und mailto-Links `_blank`, jeder
 * andere wird auf `_self` gezwungen (auch gegen ein `target` oder `<base>` aus
 * der Vorlage) und bleibt im skriptlosen Rahmen. `allow-scripts`, `allow-forms`
 * und `allow-top-navigation` kommen nie hinzu (tests/test_vorschau_links.py).
 * Die Notbetrieb-Rahmen (`sandbox=""`, CSP ohne Skripte, fremder Ursprung)
 * lösen das serverseitig: notbetrieb_lesen.mit_neuem_tab().
 */
'use strict';

const VORSCHAU_SANDBOX = 'allow-same-origin allow-popups allow-popups-to-escape-sandbox';
const _VORSCHAU_NEUER_TAB = /^(https?:|mailto:)/i;

/* Links eines Vorschau-Dokuments: http(s)/mailto → neuer Tab, alles andere
 * bleibt im Rahmen. Liefert die Zahl der umgestellten Links. */
function vorschauLinksNeuerTab(doc) {
  if (!doc || !doc.querySelectorAll) return 0;
  let n = 0;
  doc.querySelectorAll('a[href], area[href]').forEach(a => {
    if (_VORSCHAU_NEUER_TAB.test((a.getAttribute('href') || '').trim())) {
      a.setAttribute('target', '_blank');
      a.setAttribute('rel', 'noopener noreferrer');
      n++;
    } else {
      a.setAttribute('target', '_self');
    }
  });
  return n;
}

function sigVorschau(box, html) {
  if (!box) return;
  const rahmen = document.createElement('iframe');
  rahmen.setAttribute('sandbox', VORSCHAU_SANDBOX);
  rahmen.setAttribute('title', 'Signatur-Vorschau');
  rahmen.style.cssText = 'width:100%;border:0;background:#fff;display:block;min-height:60px';
  rahmen.srcdoc =
    '<!doctype html><html><head><meta charset="utf-8">'
    + '<style>html,body{margin:0;padding:0;background:#fff;'
    + 'font-family:Calibri,Arial,sans-serif;font-size:11pt}</style>'
    + '</head><body>' + (html || '') + '</body></html>';

  box.innerHTML = '';
  box.appendChild(rahmen);

  // Der Rahmen wächst nicht von selbst — die Höhe des Inhalts steht erst nach
  // dem Laden fest. Bilder kommen ggf. später; deshalb ein zweiter Anlauf.
  const anpassen = () => {
    try {
      const doc = rahmen.contentDocument;
      if (doc && doc.body) rahmen.style.height = (doc.body.scrollHeight + 8) + 'px';
    } catch (e) {
      rahmen.style.height = '260px';   // Notmaß, falls der Zugriff scheitert
    }
  };
  rahmen.addEventListener('load', () => {
    try { vorschauLinksNeuerTab(rahmen.contentDocument); } catch (e) { /* kein Zugriff: Links bleiben im Rahmen */ }
    anpassen(); setTimeout(anpassen, 400);
  });
  // Unsichtbar gefüllt (z. B. im geschlossenen <details>) misst scrollHeight 0
  // und der Rahmen blieb bei 60 px, auch nach dem Aufklappen. Der Beobachter
  // misst neu, sobald der Rahmen sichtbar wird oder seine Breite ändert; setzt
  // anpassen() dieselbe Höhe, meldet er nichts mehr (keine Schleife).
  if (typeof ResizeObserver === 'function') new ResizeObserver(anpassen).observe(rahmen);
}


/* Postfach- und „Schreibt“-Auswahl der Signatur-Vorschauen füllen.
 *
 * EINE Stelle für Vorlagen-Editor, Was-wäre-wenn-Vorschau und
 * Richtlinienergebnis (seit v1.12.110; vorher je Seite eine Kopie).
 * `postfaecher` aus /api/mailboxes/namen ({email, name}). „Schreibt“ bekommt
 * dieselben Postfächer ohne die Leerzeile. Eine Adresse aus der Aufrufadresse
 * (`vorgabe`) geht vor, sonst das zuletzt gewählte bzw. erste Postfach.
 * Liefert, ob ein Postfach gewählt ist.
 */
/* global vorschauPostfachWaehlen */  // common.js — alle drei Seiten erben es über base.html
function vorschauPostfaecherFuellen(sel, schreibt, postfaecher, vorgabe) {
  const adressen = [];
  (postfaecher || []).forEach(m => {
    const o = document.createElement('option');
    o.value = m.email;
    o.textContent = m.name !== m.email ? `${m.name} <${m.email}>` : m.email;
    sel.appendChild(o);
    adressen.push(m.email);
  });
  if (schreibt) Array.from(sel.options).slice(1).forEach(o => schreibt.appendChild(o.cloneNode(true)));
  if (vorgabe && adressen.includes(vorgabe)) {
    sel.value = vorgabe;
    return true;
  }
  return Boolean(vorschauPostfachWaehlen(sel, adressen));
}
