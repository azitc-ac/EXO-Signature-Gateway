/* common.js — gemeinsame Frontend-Helfer für Gateway UND Hub.
 *
 * DIESE DATEI MUSS IN BEIDEN ANWENDUNGEN INHALTSGLEICH SEIN.
 * tools/driftcheck.py vergleicht die SHA-256 und schlägt bei Abweichung an.
 * Änderungen also immer in beide Kopien, oder driftcheck meldet es beim
 * nächsten Lauf.
 *
 * Warum es das gibt: es existierten elf handgeschriebene HTML-Escaper mit elf
 * verschiedenen Namen (_esc, escHtml, _escH, _escT, _escAttr, escC, escR,
 * escP, esc …). Zwei davon waren binnen einer Sitzung ReferenceErrors, weil der
 * Name an der Aufrufstelle nicht zum Namen an der Definition passte. Ein
 * gemeinsamer Name kann nicht falsch geschrieben werden, ohne sofort aufzufallen.
 */

/* HTML-Text maskieren. Für alles, was per innerHTML/Template-String in die Seite
 * kommt und nicht aus dem eigenen Code stammt: Server-Antworten, Fehlertexte
 * fremder Dienste, Namen, E-Mail-Adressen, Dateinamen. */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

/* Für Werte, die in ein Attribut gehen (value="…", title="…").
 * Identisch zu esc(), aber als eigener Name, damit an der Aufrufstelle
 * erkennbar bleibt, in welchem Zusammenhang maskiert wird. Zeilenumbrüche
 * werden zusätzlich entfernt — in einem Attribut haben sie nichts zu suchen
 * und brechen die Darstellung. */
function escAttr(s) {
  return esc(s).replace(/[\r\n]+/g, ' ');
}

/* Zustandsfarben NIE per style.color/style.background setzen (CLAUDE.md Regel 2):
 * der Browser normalisiert JS-gesetzte Inline-Styles zu rgb(), und die
 * Dark-Mode-Attribut-Selektoren [style*="…#hex"] greifen dann nicht mehr.
 * Stattdessen data-state setzen und im CSS beide Modi abdecken. */
function setState(el, state) {
  if (!el) return;
  if (typeof el === 'string') el = document.getElementById(el);
  if (!el) return;
  el.dataset.state = state;
}

/* Kurzmeldung an einem Element: Text + Zustand + einblenden.
 * Ersetzt die diversen _autoMsg/_showMsg-Varianten. */
function showMsg(el, text, ok) {
  if (typeof el === 'string') el = document.getElementById(el);
  if (!el) return;
  el.textContent = text;
  el.dataset.state = ok ? 'ok' : 'err';
  el.style.display = 'block';
}

/* JSON-POST mit einheitlicher Fehlerbehandlung. Wirft nicht, sondern liefert
 * immer ein Objekt mit ok/error — damit Aufrufstellen nicht jeweils eigene
 * try/catch-Varianten bauen. */
async function postJSON(url, body) {
  try {
    const r = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const ct = r.headers.get('content-type') || '';
    const d = ct.indexOf('application/json') === 0 ? await r.json() : {};
    if (!r.ok && !d.message && !d.error && !d.detail) {
      return { ok: false, error: 'HTTP ' + r.status };
    }
    if (d.ok === undefined) d.ok = r.ok;
    return d;
  } catch (e) {
    return { ok: false, error: 'Netzwerkfehler: ' + e };
  }
}

/* GET mit einheitlicher Fehlerbehandlung. Liefert immer ein Objekt:
 * bei Netzwerk- oder HTTP-Fehler { ok:false, error:"…" } statt zu werfen.
 * Für Anzeige-Widgets, die sonst still leer blieben. */
async function getJSON(url) {
  try {
    const r = await fetch(url);
    const ct = r.headers.get('content-type') || '';
    const d = ct.indexOf('application/json') === 0 ? await r.json() : {};
    if (!r.ok) return { ok: false, error: d.error || d.message || d.detail || ('HTTP ' + r.status) };
    if (d.ok === undefined) d.ok = true;
    return d;
  } catch (e) {
    return { ok: false, error: 'Netzwerkfehler: ' + e };
  }
}

/* Wie postJSON, aber mit frei wählbarer Methode — für DELETE und PUT. */
async function sendJSON(method, url, body) {
  try {
    const opts = { method: method };
    if (body !== undefined && body !== null) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(url, opts);
    const ct = r.headers.get('content-type') || '';
    const d = ct.indexOf('application/json') === 0 ? await r.json() : {};
    if (!r.ok && !d.message && !d.error && !d.detail) return { ok: false, error: 'HTTP ' + r.status };
    if (d.ok === undefined) d.ok = r.ok;
    return d;
  } catch (e) {
    return { ok: false, error: 'Netzwerkfehler: ' + e };
  }
}

/* Fehlertext aus einer Antwort von getJSON/postJSON/sendJSON. */
function errText(d) {
  return (d && (d.error || d.message || d.detail)) || 'Unbekannter Fehler';
}

/* Betrag in Cent als Euro-Text. War in mehreren Vorlagen einzeln nachgebaut. */
function eur(cents) {
  return (Number(cents || 0) / 100).toLocaleString('de-DE', {
    style: 'currency', currency: 'EUR',
  });
}
