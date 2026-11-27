/* Protokoll- und Mail-Listen — EIN Weg für alle Tabellen mit Absender,
 * Empfänger und Betreff (Mail-Protokoll im Dashboard, Relay-Protokoll,
 * Direktversand-Vorgänge, Portal-Nachrichten, Warteschlange).
 *
 * Bis v1.12.175 baute jede Ansicht ihre Zellen selbst: Das Mail-Protokoll
 * kürzte den Betreff, brach Absender und Empfänger aber mit word-break:break-all
 * um („alexander@zaren / ko.net“), andere Listen kürzten mit eigenen max-width.
 *
 * Regeln (CSS in protokoll_tabelle.css, geprüft von tests/test_protokoll_tabelle.py):
 * - jede Zeile einzeilig, zu Langes endet mit „…“, der volle Text steht im
 *   title (fehlt er, setzt ihn diese Datei aus dem Zelltext);
 * - Spalten mit `data-spalte="…" data-anpassbar` im Kopf lassen sich am
 *   rechten Rand breiter ziehen; die Breite merkt sich der Browser
 *   (localStorage, je Tabelle), nicht der Server;
 * - `class="ganz"` am Kopf: Spalte wird nicht gekürzt (Knöpfe, Marken);
 * - Vorgabebreite per `data-breite="120px"` am Kopf.
 */
'use strict';

const _PROTOKOLL_SPEICHER = 'protokollSpalten:';
const _PROTOKOLL_MIN_PX = 60;

function _protokollBreitenLesen(schluessel) {
  try {
    const roh = JSON.parse(localStorage.getItem(_PROTOKOLL_SPEICHER + schluessel) || '{}');
    return (roh && typeof roh === 'object') ? roh : {};
  } catch (e) { return {}; }
}

function _protokollBreitenSchreiben(schluessel, breiten) {
  try { localStorage.setItem(_PROTOKOLL_SPEICHER + schluessel, JSON.stringify(breiten)); } catch (e) { /* privat/gesperrt */ }
}

/* Zellen nach dem (Neu-)Füllen: Spaltenklasse übernehmen, title ergänzen. */
function _protokollZellen(tabelle) {
  const kopf = tabelle.tHead && tabelle.tHead.rows[0];
  if (!kopf) return;
  const ganz = Array.from(kopf.cells).map(th => th.classList.contains('ganz'));
  Array.from(tabelle.tBodies).forEach(tb => Array.from(tb.rows).forEach(tr => {
    Array.from(tr.cells).forEach((td, i) => {
      if (td.colSpan > 1) return;
      if (ganz[i]) { td.classList.add('ganz'); return; }
      const text = (td.textContent || '').trim();
      if (text && !td.hasAttribute('title')) td.setAttribute('title', text);
    });
  }));
}

function _protokollGriff(tabelle, th, schluessel) {
  const griff = document.createElement('span');
  griff.className = 'spalten-griff';
  griff.setAttribute('aria-hidden', 'true');
  griff.title = 'Spaltenbreite ziehen';
  griff.addEventListener('pointerdown', ev => {
    ev.preventDefault();
    const start = ev.clientX, breite = th.getBoundingClientRect().width;
    griff.classList.add('zieht');
    try { griff.setPointerCapture(ev.pointerId); } catch (e) { /* ältere Browser */ }
    const ziehen = e => { th.style.width = Math.max(_PROTOKOLL_MIN_PX, Math.round(breite + e.clientX - start)) + 'px'; };
    const fertig = () => {
      griff.classList.remove('zieht');
      griff.removeEventListener('pointermove', ziehen);
      griff.removeEventListener('pointerup', fertig);
      griff.removeEventListener('pointercancel', fertig);
      const breiten = _protokollBreitenLesen(schluessel);
      breiten[th.dataset.spalte] = Math.round(th.getBoundingClientRect().width);
      _protokollBreitenSchreiben(schluessel, breiten);
    };
    griff.addEventListener('pointermove', ziehen);
    griff.addEventListener('pointerup', fertig);
    griff.addEventListener('pointercancel', fertig);
  });
  griff.addEventListener('click', ev => ev.stopPropagation());
  th.appendChild(griff);
}

/* Tabelle einmal einrichten; danach hält ein Beobachter neu gefüllte Zeilen
 * auf Linie. Mehrfacher Aufruf (z. B. nach Neuaufbau) ist harmlos. */
function protokollTabelle(tabelle, schluessel) {
  if (!tabelle || !tabelle.tHead) return;
  tabelle.classList.add('protokoll-tabelle');
  if (tabelle.dataset.protokoll !== schluessel) {
    tabelle.dataset.protokoll = schluessel;
    const breiten = _protokollBreitenLesen(schluessel);
    Array.from(tabelle.tHead.rows[0].cells).forEach(th => {
      if (th.dataset.breite) th.style.width = th.dataset.breite;
      const name = th.dataset.spalte;
      if (!name || !th.hasAttribute('data-anpassbar')) return;
      if (Number(breiten[name]) >= _PROTOKOLL_MIN_PX) th.style.width = Number(breiten[name]) + 'px';
      _protokollGriff(tabelle, th, schluessel);
    });
    if (typeof MutationObserver === 'function') {
      const beob = new MutationObserver(() => _protokollZellen(tabelle));
      Array.from(tabelle.tBodies).forEach(tb => beob.observe(tb, {childList: true}));
    }
  }
  _protokollZellen(tabelle);
}
