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
 */
'use strict';

function sigVorschau(box, html) {
  if (!box) return;
  const rahmen = document.createElement('iframe');
  rahmen.setAttribute('sandbox', 'allow-same-origin');
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
  rahmen.addEventListener('load', () => { anpassen(); setTimeout(anpassen, 400); });
}
