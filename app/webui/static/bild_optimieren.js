/* Eingebettete Bilder für Signaturen verkleinern — im Browser, vor dem Speichern.
 *
 * ANLASS (Ideenliste 20.11.2026)
 * ------------------------------
 * Bis v1.12.134 nahm der Baukasten ein hochgeladenes Bild unverändert als
 * Data-URI und lehnte alles über 512 KB ab („Bitte vorher verkleinern“). Ein
 * Firmenlogo direkt aus der Grafikabteilung (3000 px breit, PNG) sprengte so
 * Outlooks Grenze für den Signaturbereich (30.000 Zeichen); die Anzeigebreite im
 * Baukasten verkleinert die DATEI nicht. Wer kein Bildprogramm zur Hand hatte,
 * kam nicht weiter.
 *
 * WAS ES TUT
 * ----------
 * * Pixelbreite auf das Doppelte der Anzeigebreite (scharf auf hochauflösenden
 *   Bildschirmen), nie größer als das Original.
 * * Mit Transparenz → PNG. Ohne → das kleinere aus JPG (Qualität 0,85) und PNG
 *   (Fotos werden JPG, flache Grafiken bleiben oft PNG).
 * * SVG wird gerastert — Outlook zeigt eingebettete SVG nicht.
 * * GIF bleibt unverändert (eine Animation ginge verloren).
 * * Ist das Ergebnis nicht kleiner und war nichts zu verkleinern, bleibt das
 *   Original.
 *
 * Liefert {dataUrl, geaendert, vorher:{breite, hoehe, bytes, typ},
 *          nachher:{breite, hoehe, bytes, typ}, hinweis}.
 */
'use strict';

const BILD_MAX_PIXEL = 1200;          // absolute Obergrenze der Pixelbreite
const BILD_JPG_QUALITAET = 0.85;

function _bildBytesAusDataUrl(url) {
  const i = String(url || '').indexOf(',');
  if (i < 0) return 0;
  const b64 = url.slice(i + 1).replace(/\s/g, '');
  return Math.floor(b64.length * 3 / 4) - (b64.endsWith('==') ? 2 : b64.endsWith('=') ? 1 : 0);
}

function _bildTyp(url) {
  const m = /^data:([^;,]+)/.exec(String(url || ''));
  return m ? m[1].toLowerCase() : '';
}

function _alsDataUrl(blob) {
  return new Promise((ok, fehler) => {
    const r = new FileReader();
    r.onload = () => ok(r.result);
    r.onerror = () => fehler(r.error);
    r.readAsDataURL(blob);
  });
}

function _bildLaden(url) {
  return new Promise((ok, fehler) => {
    const img = new Image();
    img.onload = () => ok(img);
    img.onerror = () => fehler(new Error('Bild lässt sich nicht lesen'));
    img.src = url;
  });
}

function bildGroesseText(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / 1024 / 1024).toLocaleString('de-DE', {maximumFractionDigits: 1}) + ' MB';
  return Math.max(1, Math.round(bytes / 1024)).toLocaleString('de-DE') + ' KB';
}

// Schrittweise halbieren: Ein Sprung von 3000 auf 200 px mit einem drawImage
// wird körnig, weil der Browser dabei nur wenige Quellpixel mittelt.
function _skalieren(quelle, breite, hoehe) {
  let w = quelle.naturalWidth || quelle.width, h = quelle.naturalHeight || quelle.height;
  let stufe = quelle;
  while (w / 2 >= breite) {
    const c = document.createElement('canvas');
    c.width = Math.round(w / 2); c.height = Math.round(h / 2);
    const k = c.getContext('2d');
    k.imageSmoothingEnabled = true; k.imageSmoothingQuality = 'high';
    k.drawImage(stufe, 0, 0, c.width, c.height);
    stufe = c; w = c.width; h = c.height;
  }
  const ziel = document.createElement('canvas');
  ziel.width = breite; ziel.height = hoehe;
  const k = ziel.getContext('2d');
  k.imageSmoothingEnabled = true; k.imageSmoothingQuality = 'high';
  k.drawImage(stufe, 0, 0, breite, hoehe);
  return ziel;
}

function _hatTransparenz(canvas) {
  const d = canvas.getContext('2d').getImageData(0, 0, canvas.width, canvas.height).data;
  for (let i = 3; i < d.length; i += 4) if (d[i] < 255) return true;
  return false;
}

async function bildOptimieren(quelle, anzeigeBreite) {
  const url = typeof quelle === 'string' ? quelle : await _alsDataUrl(quelle);
  const typ = _bildTyp(url);
  const bytes = typeof quelle === 'string' ? _bildBytesAusDataUrl(url) : quelle.size;
  const img = await _bildLaden(url);
  const vorher = {breite: img.naturalWidth, hoehe: img.naturalHeight, bytes: bytes, typ: typ};
  const unveraendert = (hinweis) => ({dataUrl: url, geaendert: false, vorher: vorher, nachher: vorher, hinweis: hinweis});
  if (typ === 'image/gif') return unveraendert('GIF unverändert (eine Animation ginge sonst verloren).');
  if (!vorher.breite || !vorher.hoehe) return unveraendert('');
  const soll = Math.max(1, Math.min(BILD_MAX_PIXEL, Math.round(2 * (parseInt(anzeigeBreite, 10) || 150))));
  const breite = Math.min(vorher.breite, soll);
  const hoehe = Math.max(1, Math.round(vorher.hoehe * breite / vorher.breite));
  const canvas = _skalieren(img, breite, hoehe);
  let kandidaten = [canvas.toDataURL('image/png')];
  if (!_hatTransparenz(canvas)) kandidaten.push(canvas.toDataURL('image/jpeg', BILD_JPG_QUALITAET));
  kandidaten.sort((a, b) => a.length - b.length);
  const neu = kandidaten[0];
  const neuBytes = _bildBytesAusDataUrl(neu);
  const muss = typ === 'image/svg+xml' || breite < vorher.breite;
  if (!muss && neuBytes >= bytes) return unveraendert('Bereits passend — unverändert.');
  const nachher = {breite: breite, hoehe: hoehe, bytes: neuBytes, typ: _bildTyp(neu)};
  return {
    dataUrl: neu, geaendert: true, vorher: vorher, nachher: nachher,
    hinweis: 'Verkleinert: ' + vorher.breite + ' × ' + vorher.hoehe + ' px, ' + bildGroesseText(bytes)
      + ' → ' + breite + ' × ' + hoehe + ' px, ' + bildGroesseText(neuBytes)
      + ' (' + (nachher.typ === 'image/jpeg' ? 'JPG' : 'PNG') + ')',
  };
}
