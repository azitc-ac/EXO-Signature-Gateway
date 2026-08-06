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

/* ── Markdown ────────────────────────────────────────────────────────────────
 * Kleiner Wandler für die Texte, die als Markdown vorliegen: Rechtstexte,
 * CA-Bedingungen, Changelog-Einträge. Lag als `_mdToHtml` lokal in
 * settings_connect.html — mit der Folge, dass die Changelog-Anzeige in der
 * Update-Sektion den Text ROH ausgab: sichtbare `**`, Backticks und
 * Tabellenstriche. Ein zweiter Wandler wäre die falsche Antwort gewesen.
 *
 * Bewusst klein gehalten: Überschriften, Listen, Tabellen, Trennlinien, fett,
 * kursiv, Code, Verweise. Kein vollständiges Markdown — was hier ankommt,
 * schreiben wir selbst.
 *
 * Farben stammen aus der freigegebenen Palette (CLAUDE.md); ohne Angabe griffe
 * beim Verweis das Browser-Standardblau, das im Dark Mode schlecht lesbar ist.
 */
function _mdInline(s) {
  return esc(s)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    // Kursiv NACH fett — danach sind keine ** mehr übrig
    .replace(/\*([^*\n]+)\*/g,'<em>$1</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // color:#0369a1 stammt aus der freigegebenen Palette (CLAUDE.md) und wird im
    // Dark Mode zu #7dd3fc umgeschaltet. Ohne Angabe griffe das Browser-Standardblau,
    // das auf dunklem Grund schlecht lesbar ist — eine globale a-Regel gibt es nicht.
    .replace(/(https?:\/\/[^\s<)]+)/g,
             '<a href="$1" target="_blank" rel="noopener" style="color:#0369a1">$1</a>');
}
function _mdToHtml(md) {
  var out = [], tbl = null;
  // In Absätze zerlegen (Leerzeile trennt), Tabellen/Listen bleiben zeilenweise
  var blocks = String(md || '').replace(/\r\n/g, '\n').split(/\n{2,}/);
  blocks.forEach(function(block) {
    var lines = block.split('\n').filter(function(l) { return l.trim() !== ''; });
    if (!lines.length) return;
    // Tabelle: mindestens zwei Zeilen, alle beginnen mit |
    if (lines.length >= 2 && lines.every(function(l) { return l.trim().charAt(0) === '|'; })) {
      var rows = lines.filter(function(l) { return !/^\|[\s:|-]+\|$/.test(l.trim()); });
      var html = '<table style="width:100%;border-collapse:collapse;margin:8px 0;font-size:12px">';
      rows.forEach(function(l, i) {
        var cells = l.trim().replace(/^\||\|$/g, '').split('|');
        var tag = i === 0 ? 'th' : 'td';
        html += '<tr>' + cells.map(function(c) {
          return '<' + tag + ' style="border:1px solid #e2e8f0;padding:4px 8px;text-align:left">'
                 + _mdInline(c.trim()) + '</' + tag + '>';
        }).join('') + '</tr>';
      });
      out.push(html + '</table>');
      return;
    }
    // Überschrift
    var h = lines[0].match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      var lvl = Math.min(h[1].length + 1, 4);
      out.push('<h' + lvl + ' style="margin:14px 0 6px;font-size:' + (16 - h[1].length) + 'px">'
               + _mdInline(h[2]) + '</h' + lvl + '>');
      lines = lines.slice(1);
      if (!lines.length) return;
    }
    // Trennlinie
    if (lines.every(function(l) { return /^-{3,}$/.test(l.trim()); })) {
      out.push('<hr style="border:none;border-top:1px solid #e2e8f0;margin:12px 0">');
      return;
    }
    // Liste
    if (lines[0].trim().charAt(0) === '-') {
      var items = [], cur = '';
      lines.forEach(function(l) {
        if (/^\s*-\s+/.test(l)) { if (cur) items.push(cur); cur = l.replace(/^\s*-\s+/, ''); }
        else { cur += ' ' + l.trim(); }     // Fortsetzungszeile anhängen
      });
      if (cur) items.push(cur);
      out.push('<ul style="margin:6px 0 6px 18px;padding:0">'
        + items.map(function(i) { return '<li style="margin:2px 0">' + _mdInline(i) + '</li>'; }).join('')
        + '</ul>');
      return;
    }
    // Normaler Absatz: harte Umbrüche zu Leerzeichen zusammenziehen
    out.push('<p style="margin:0 0 10px">' + _mdInline(lines.join(' ')) + '</p>');
  });
  return out.join('');
}

/* Ursache eines gefangenen Fehlers sichtbar machen.
 *
 * Anlass (2026-07-27): `_reflowPlain()` rief `_mdEscape()` auf — eine Funktion,
 * die es nicht gab. Der ReferenceError landete in einem catch-Zweig, der ihn
 * verwarf und stattdessen „Nutzungsbedingungen konnten nicht geladen werden"
 * anzeigte. Die Meldung zeigte damit auf den Hub, der einwandfrei antwortete.
 * Hätte dort „… : _mdEscape is not defined" gestanden, wäre die Ursache sofort
 * sichtbar gewesen statt nach Wochen.
 *
 * Deshalb tut die Funktion beides: sie protokolliert den vollständigen Fehler
 * samt Aufrufkette in der Konsole UND liefert einen kurzen Anhang für die
 * Anzeige. Rückgabe ist ein FERTIGES Textstück inklusive Klammern, damit die
 * Aufrufstelle nur anhängen muss:
 *
 *     catch (e) { el.textContent = 'Netzwerkfehler' + ursache(e, 'certTerms'); }
 *
 * Ist keine Ursache zu ermitteln, bleibt es beim bisherigen Satz — ein
 * angehängtes „(undefined)" hilft niemandem.
 */
function ursache(e, ort) {
  try { console.error('[' + (ort || 'unbekannt') + ']', e); } catch (_) { /* Konsole fehlt */ }
  var t = '';
  if (e == null) t = '';
  else if (typeof e === 'string') t = e;
  else t = e.message || e.name || String(e);
  t = String(t).trim();
  if (!t || t === '[object Object]') return '';
  return ' (' + (t.length > 160 ? t.slice(0, 157) + '…' : t) + ')';
}


/* Fehler DIREKT am betroffenen Feld anzeigen — nicht am Ende des Abschnitts.
 *
 * Anlass (2026-07-29): „Mindestbetrag 25 €." erschien in der Sammelmeldung ganz
 * unten, während das Eingabefeld weiter oben stand. Bei einem langen Abschnitt
 * sieht man beides nie gleichzeitig; man liest eine Rüge und muss suchen,
 * worauf sie sich bezieht.
 *
 * Die Meldung wird als Geschwister direkt hinter das Feld gehängt und beim
 * nächsten Aufruf wiederverwendet, damit sich bei mehrfacher Eingabe nichts
 * stapelt. `fieldClear()` räumt sie weg, sobald der Wert wieder stimmt.
 *
 * Farbe kommt aus dem CSS (.field-msg[data-state]) — hier wird nur der Zustand
 * gesetzt, nie eine Farbe (siehe CLAUDE.md, Dark-Mode-Regel 2).
 */
function fieldMsg(el, text, ok) {
  if (typeof el === 'string') el = document.getElementById(el);
  if (!el) return;
  var box = el.nextElementSibling;
  if (!box || !box.classList || !box.classList.contains('field-msg')) {
    box = document.createElement('div');
    box.className = 'field-msg';
    el.parentNode.insertBefore(box, el.nextSibling);
  }
  box.textContent = text;
  box.dataset.state = ok ? 'ok' : 'err';
  box.style.display = 'block';
  // Auch das Feld selbst kennzeichnen: bei mehreren Eingaben nebeneinander ist
  // sonst nicht zu sehen, welches gemeint ist.
  el.dataset.invalid = ok ? '' : '1';
  // Liegt das Feld in einem eingeklappten Bereich, wäre die Rüge unsichtbar —
  // der Vorgang bräche scheinbar grundlos ab. Deshalb alle umschliessenden
  // <details> aufklappen. Das gehört hierher und nicht an die Aufrufstellen,
  // sonst muss jede künftige daran denken.
  if (!ok && el.closest) {
    var d = el.closest('details');
    while (d) {
      d.open = true;
      d = d.parentElement && d.parentElement.closest ? d.parentElement.closest('details') : null;
    }
  }
}

function fieldClear(el) {
  if (typeof el === 'string') el = document.getElementById(el);
  if (!el) return;
  var box = el.nextElementSibling;
  if (box && box.classList && box.classList.contains('field-msg')) box.style.display = 'none';
  el.dataset.invalid = '';
}


/* Lange Erklärtexte auf zwei Zeilen kürzen, mit „mehr"/„weniger".
 *
 * Anlass (2026-07-29): 38 Hinweistexte, mehrere davon 300–480 Zeichen. Sie
 * drängen das Bedienbare nach unten und werden gerade deshalb nicht gelesen.
 *
 * Warum die Zeilenbegrenzung per CSS und kein Aufteilen am ersten Satz: Text zu
 * zerlegen geht an Abkürzungen („z.B.", „i.S.d.", „Ziffer 6.11") schief. Die
 * Begrenzung braucht den Inhalt gar nicht zu kennen, wirkt bei jedem künftigen
 * Text und lässt sich ohne Änderung an den Vorlagen einführen.
 *
 * Ohne JavaScript bleibt der volle Text stehen — die Kürzung ist eine Zutat,
 * keine Voraussetzung fürs Lesen.
 *
 * Nur Block-Elemente: `span.hint` steht meist inline hinter einem Feld. Die
 * Kürzung setzt `display:-webkit-box`, macht ein solches span also zum Block
 * und verschöbe das Layout — bei kurzen Texten ohne jeden Gewinn.
 */
function initHintClamps(root, minLen) {
  var scope = root || document;
  var grenze = minLen || 150;
  var texte = scope.querySelectorAll('p.hint:not([data-clamp]), div.hint:not([data-clamp])');
  Array.prototype.forEach.call(texte, function (p) {
    if ((p.textContent || '').trim().length < grenze) return;
    p.dataset.clamp = 'zu';
    var schalter = document.createElement('button');
    schalter.type = 'button';
    schalter.className = 'hint-toggle';
    schalter.textContent = 'mehr';
    schalter.addEventListener('click', function () {
      var zu = p.dataset.clamp === 'zu';
      p.dataset.clamp = zu ? 'auf' : 'zu';
      schalter.textContent = zu ? 'weniger' : 'mehr';
    });
    p.parentNode.insertBefore(schalter, p.nextSibling);
  });
}


// ── Zuletzt gewähltes Vorschau-Postfach ──────────────────────────────────────
//
// Gemeinsam für Editor-Live-Vorschau und Vorschau-Seite: Zwei Kopien liefen
// sonst auseinander, und wer zwischen beiden wechselt, bekäme unterschiedliche
// Vorauswahlen.
//
// Regel: das zuletzt gewählte Postfach, sonst das erste der Liste. Wer immer
// dieselbe Signatur prüft, muss sie nicht bei jedem Öffnen neu wählen; wer das
// Gerät wechselt oder das Postfach verliert, bekommt trotzdem sofort eine
// Vorschau statt einer leeren Fläche.
const VORSCHAU_POSTFACH_SCHLUESSEL = 'exo.vorschau.postfach';

function vorschauPostfachMerken(email) {
  try {
    if (email) localStorage.setItem(VORSCHAU_POSTFACH_SCHLUESSEL, email);
    else localStorage.removeItem(VORSCHAU_POSTFACH_SCHLUESSEL);
  } catch (e) { /* privates Fenster o.ä. — dann eben ohne Gedächtnis */ }
}

function vorschauPostfachWaehlen(sel, adressen) {
  // `adressen` sind die tatsächlich vorhandenen Werte. Ein gemerktes Postfach,
  // das es nicht mehr gibt, darf nicht zu einer leeren Auswahl führen.
  let gemerkt = null;
  try { gemerkt = localStorage.getItem(VORSCHAU_POSTFACH_SCHLUESSEL); }
  catch (e) { /* s.o. */ }
  const wahl = (gemerkt && adressen.includes(gemerkt)) ? gemerkt : (adressen[0] || '');
  if (wahl) sel.value = wahl;
  return wahl;
}
