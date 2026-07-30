#!/usr/bin/env node
/*
 * Sucht Bezeichner, die zur Laufzeit nirgends aufgelöst werden — mit echter
 * Geltungsbereichs-Analyse statt Namensliste.
 *
 * Anlass (2026-07-30): `_updShowSuccess()` in backup.html las `current`, um
 * "vorher → nachher" zu bilden. Deklariert war der Name aber nur INNERHALB von
 * `_updOnTargetChange()`. Ergebnis war ein ReferenceError, den der catch-Zweig
 * der Abfrageschleife in "Container wird neu gestartet…" übersetzte — das
 * Update blieb dauerhaft in dieser Anzeige stehen, obwohl es fertig war.
 *
 * Warum die vorhandenen Prüfungen das NICHT sahen:
 *   jscheck.py   — `node --check` prüft Syntax; die war einwandfrei.
 *   jsrefcheck.js— prüft nur AUFRUFE (`foo(…)`), `current` ist ein reiner
 *                  Lesezugriff. Und es sammelt Definitionen DATEIWEIT in eine
 *                  flache Menge: `current` kam darin vor, nur eben in einer
 *                  anderen Funktion. Ohne Geltungsbereiche ist der Fall
 *                  grundsätzlich nicht zu sehen.
 *
 * Dieses Werkzeug ERSETZT jsrefcheck.js — Aufrufe sind Bezeichner, die
 * Prüfung hier ist die genauere. Sein Anlass bleibt gültig und sei
 * festgehalten: `_reflowPlain()` in settings_connect.html rief viermal
 * `_mdEscape()` auf, das es nach der Zusammenführung der Escaper nicht mehr
 * gab; der ReferenceError landete in einem catch-Zweig und wurde dort als
 * „Nutzungsbedingungen konnten nicht geladen werden" ausgegeben — eine
 * Meldung, die auf das Falsche zeigt. Genau dieselbe Verwechslung erzeugte
 * jetzt „Container wird neu gestartet…".
 *
 * Beim ersten Lauf fand diese Prüfung drei Fehler, die beide Vorgänger nicht
 * sehen konnten: `escC` als Funktionsreferenz in `needing.map(escC)` (Rest
 * derselben Escaper-Zusammenführung — kein Aufruf, daher unsichtbar für
 * jsrefcheck), sowie `_licFall` und `_licState` in `licBuy()`, deren
 * Deklarationen beim Umbau auf das Abonnement entfielen.
 *
 * ABGRENZUNG — was diese Prüfung bewusst NICHT tut
 * ------------------------------------------------
 * Geltungsbereiche werden auf FUNKTIONSEBENE gebildet, nicht auf Blockebene:
 * ein `let` in einem if-Block gilt hier für die ganze Funktion. Damit entgehen
 * uns Verstöße gegen die temporale Totzone und Block-Grenzen.
 *
 * Das ist Absicht. Der wiederkehrende Fehler ist "Name lebt in einer anderen
 * FUNKTION" — und den fängt diese Ebene vollständig. Blockgenauigkeit brächte
 * kaum zusätzliche Funde, aber deutlich mehr Falsch-Positive, und ein
 * Prüfskript, das rauscht, wird abgeschaltet.
 *
 * AUFRUF
 * ------
 *     node tools/jsscopecheck.js <vorlagen-verzeichnis> [gemeinsame.js …]
 */
'use strict';
const fs = require('fs');
const path = require('path');
const acorn = require('acorn');

// Browser- und Laufzeit-Namen, die es immer gibt. Bewusst als Liste und nicht
// über `globalThis` des Node-Prozesses: dort fehlen `document`/`window`, dafür
// gäbe es `process`/`require`, die im Browser gerade NICHT da sind.
const GLOBALS = new Set([
  // Sprache
  'undefined', 'NaN', 'Infinity', 'globalThis', 'arguments', 'eval',
  'parseInt', 'parseFloat', 'isNaN', 'isFinite', 'String', 'Number', 'Boolean',
  'Array', 'Object', 'JSON', 'Math', 'Date', 'RegExp', 'Error', 'TypeError',
  'RangeError', 'SyntaxError', 'Promise', 'Map', 'Set', 'WeakMap', 'WeakSet',
  'Symbol', 'BigInt', 'Proxy', 'Reflect', 'Function', 'Intl',
  'encodeURIComponent', 'decodeURIComponent', 'encodeURI', 'decodeURI',
  'Uint8Array', 'Int8Array', 'Uint16Array', 'Uint32Array', 'Int32Array',
  'Float32Array', 'Float64Array', 'ArrayBuffer', 'DataView', 'structuredClone',
  // Fenster und Dokument
  'window', 'document', 'navigator', 'location', 'history', 'screen', 'self',
  'top', 'parent', 'frames', 'localStorage', 'sessionStorage', 'console',
  'alert', 'confirm', 'prompt', 'open', 'close', 'print', 'scrollTo', 'scrollBy',
  'getComputedStyle', 'matchMedia', 'devicePixelRatio', 'visualViewport',
  'innerWidth', 'innerHeight', 'outerWidth', 'outerHeight', 'scrollX', 'scrollY',
  'pageXOffset', 'pageYOffset', 'origin', 'crypto', 'performance', 'caches',
  // Zeit und Ablauf
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval',
  'requestAnimationFrame', 'cancelAnimationFrame', 'queueMicrotask',
  // Netz und Daten
  'fetch', 'Headers', 'Request', 'Response', 'FormData', 'Blob', 'File',
  'FileReader', 'URL', 'URLSearchParams', 'AbortController', 'EventSource',
  'WebSocket', 'XMLHttpRequest', 'btoa', 'atob', 'TextEncoder', 'TextDecoder',
  'DOMParser', 'XMLSerializer', 'Notification',
  // DOM-Typen und Ereignisse
  'Node', 'Element', 'HTMLElement', 'HTMLInputElement', 'Image', 'Audio',
  'Option', 'Event', 'CustomEvent', 'MouseEvent', 'KeyboardEvent', 'DragEvent',
  'ClipboardEvent', 'MutationObserver', 'IntersectionObserver', 'ResizeObserver',
  'CSS', 'DOMRect',
  // Office-Add-in-Laufzeit (als externes Skript geladen)
  'Office', 'OfficeRuntime', 'Excel', 'Word', 'Outlook',
]);

// ── Vorlage → prüfbares JavaScript ────────────────────────────────────────────

/** Inline-Skripte einsammeln (externe via src bringen keinen Inhalt mit). */
function skripte(html) {
  return [...html.matchAll(/<script(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map(m => m[1]).join('\n;\n');
}

/**
 * Jinja neutralisieren — sonst ist die Vorlage kein gültiges JavaScript.
 * `{{ x }}` wird zu `0` (erhält die Struktur von Zuweisung und Argument),
 * `{% … %}` entfällt. Gleiche Behandlung wie in jscheck.py.
 */
function ohneJinja(src) {
  return src.replace(/\{\{[\s\S]*?\}\}/g, '0').replace(/\{%[\s\S]*?%\}/g, '');
}

// ── Geltungsbereiche ──────────────────────────────────────────────────────────

const IST_FUNKTION = new Set([
  'FunctionDeclaration', 'FunctionExpression', 'ArrowFunctionExpression',
]);

/** Alle Namen aus einem Bindungsmuster (auch verschachtelt) einsammeln. */
function musterNamen(node, raus) {
  if (!node) return;
  switch (node.type) {
    case 'Identifier':          raus.add(node.name); break;
    case 'ObjectPattern':       node.properties.forEach(p => musterNamen(p.value || p.argument, raus)); break;
    case 'ArrayPattern':        node.elements.forEach(e => musterNamen(e, raus)); break;
    case 'AssignmentPattern':   musterNamen(node.left, raus); break;
    case 'RestElement':         musterNamen(node.argument, raus); break;
  }
}

/** Kindknoten eines AST-Knotens. */
function kinder(node) {
  const raus = [];
  for (const schluessel of Object.keys(node)) {
    if (schluessel === 'type' || schluessel === 'start' || schluessel === 'end') continue;
    const wert = node[schluessel];
    if (Array.isArray(wert)) {
      for (const x of wert) if (x && typeof x.type === 'string') raus.push(x);
    } else if (wert && typeof wert.type === 'string') {
      raus.push(wert);
    }
  }
  return raus;
}

/**
 * Deklarationen eines Funktions-Geltungsbereichs sammeln: der ganze Teilbaum,
 * aber OHNE in verschachtelte Funktionen abzusteigen — deren Rümpfe bilden
 * eigene Bereiche. Ihre NAMEN gehören allerdings hierher.
 */
function deklarationen(fnNode) {
  const raus = new Set();
  if (IST_FUNKTION.has(fnNode.type)) {
    raus.add('arguments');
    fnNode.params.forEach(p => musterNamen(p, raus));
    if (fnNode.id) raus.add(fnNode.id.name);     // benannter Funktionsausdruck
  }
  const rumpf = IST_FUNKTION.has(fnNode.type) ? [fnNode.body] : kinder(fnNode);

  (function ab(knoten) {
    for (const k of knoten) {
      if (!k) continue;
      switch (k.type) {
        case 'VariableDeclarator': musterNamen(k.id, raus); break;
        case 'FunctionDeclaration':
        case 'ClassDeclaration':   if (k.id) raus.add(k.id.name); break;
        case 'CatchClause':        musterNamen(k.param, raus); break;
        case 'ImportDefaultSpecifier':
        case 'ImportNamespaceSpecifier':
        case 'ImportSpecifier':    raus.add(k.local.name); break;
      }
      // Nicht in fremde Funktionsrümpfe absteigen
      if (IST_FUNKTION.has(k.type)) continue;
      ab(kinder(k));
    }
  })(rumpf);

  return raus;
}

/**
 * Namen, die durch ZUWEISUNG entstehen statt durch Deklaration:
 *   window.foo = …   — der übliche Weg, aus einer IIFE etwas herauszureichen
 *   foo = …          — implizite Globale (funktioniert nur ohne "use strict")
 *
 * Beide erzeugen zur Laufzeit eine Bindung, sind hier also KEIN Fund. Sie
 * gelten für die ganze Datei, unabhängig davon, in welcher Funktion die
 * Zuweisung steht.
 */
function zuweisungsNamen(baum) {
  const raus = new Set();
  (function ab(node) {
    if (node.type === 'AssignmentExpression') {
      const l = node.left;
      if (l.type === 'Identifier') raus.add(l.name);
      else if (l.type === 'MemberExpression' && !l.computed
               && l.object.type === 'Identifier' && l.object.name === 'window'
               && l.property.type === 'Identifier') raus.add(l.property.name);
    }
    for (const k of kinder(node)) ab(k);
  })(baum);
  return raus;
}

/**
 * Steht dieser Identifier für einen Lesezugriff? Eigenschaftsnamen,
 * Sprungmarken und Deklarationsstellen sind keine.
 */
function istLesezugriff(node, eltern) {
  if (!eltern) return true;
  const e = eltern;
  if (e.type === 'MemberExpression' && e.property === node && !e.computed) return false;
  if (e.type === 'Property' && e.key === node && !e.computed && !e.shorthand) return false;
  if (e.type === 'MethodDefinition' && e.key === node && !e.computed) return false;
  if (e.type === 'PropertyDefinition' && e.key === node && !e.computed) return false;
  if (e.type === 'LabeledStatement' && e.label === node) return false;
  if ((e.type === 'BreakStatement' || e.type === 'ContinueStatement') && e.label === node) return false;
  if (e.type === 'ExportSpecifier' || e.type === 'ImportSpecifier') return false;
  // Deklarationsstellen: der Name wird gesetzt, nicht gelesen
  if (e.type === 'VariableDeclarator' && e.id === node) return false;
  if ((e.type === 'FunctionDeclaration' || e.type === 'ClassDeclaration'
       || e.type === 'FunctionExpression') && e.id === node) return false;
  if (IST_FUNKTION.has(e.type) && e.params.includes(node)) return false;
  if (e.type === 'AssignmentPattern' && e.left === node) return false;
  if (e.type === 'RestElement' && e.argument === node) return false;
  if (e.type === 'CatchClause' && e.param === node) return false;
  return true;
}

/** Ungebundene Lesezugriffe einer Datei ermitteln. */
function ungebunden(code, bekannt) {
  const baum = acorn.parse(code, {
    ecmaVersion: 'latest', sourceType: 'script',
    allowReturnOutsideFunction: true, allowAwaitOutsideFunction: true,
  });

  const funde = [];
  const wurzel = deklarationen(baum);
  zuweisungsNamen(baum).forEach(n => wurzel.add(n));
  (function lauf(node, eltern, kette) {
    if (node !== baum && IST_FUNKTION.has(node.type)) {
      kette = kette.concat([deklarationen(node)]);
    }
    if (node.type === 'Identifier') {
      if (istLesezugriff(node, eltern)) {
        const name = node.name;
        const gebunden = bekannt.has(name) || kette.some(s => s.has(name));
        if (!gebunden) funde.push({ name, pos: node.start });
      }
      return;
    }
    // Bei Mustern nicht in die Bindungsseite absteigen
    for (const k of kinder(node)) lauf(k, node, kette);
  })(baum, null, [wurzel]);

  return funde;
}

// ── Ablauf ────────────────────────────────────────────────────────────────────

const dir = process.argv[2];
const gemeinsam = process.argv.slice(3);
if (!dir) {
  console.error('Aufruf: jsscopecheck.js <vorlagen-verzeichnis> [gemeinsame.js …]');
  process.exit(2);
}

// Wie in jsrefcheck.js: die Helfer aus common.js/base.html stehen NICHT jeder
// Seite zur Verfügung — portal.html und smime_selfservice.html sind bewusst
// eigenständig. Diese Unterscheidung ist der halbe Sinn der Prüfung.
const gemeinsameNamen = new Set();
for (const f of gemeinsam) {
  if (!fs.existsSync(f)) continue;
  const q = f.endsWith('.html') ? ohneJinja(skripte(fs.readFileSync(f, 'utf8')))
                                : fs.readFileSync(f, 'utf8');
  const a = acorn.parse(q, { ecmaVersion: 'latest', sourceType: 'script' });
  deklarationen(a).forEach(n => gemeinsameNamen.add(n));
  zuweisungsNamen(a).forEach(n => gemeinsameNamen.add(n));
}
const basis = path.join(dir, 'base.html');
if (fs.existsSync(basis)) {
  const q = ohneJinja(skripte(fs.readFileSync(basis, 'utf8')));
  const a = acorn.parse(q, { ecmaVersion: 'latest', sourceType: 'script' });
  deklarationen(a).forEach(n => gemeinsameNamen.add(n));
  zuweisungsNamen(a).forEach(n => gemeinsameNamen.add(n));
}

function nutztGemeinsames(html, datei) {
  return datei === 'base.html'
    || /\{%\s*extends\s+["']base\.html["']\s*%\}/.test(html)
    || /<script[^>]*\bsrc\s*=\s*["'][^"']*common\.js/.test(html);
}

let treffer = 0, geprueft = 0, eigenstaendig = 0, unlesbar = 0;
for (const f of fs.readdirSync(dir).filter(x => x.endsWith('.html')).sort()) {
  const html = fs.readFileSync(path.join(dir, f), 'utf8');
  const code = ohneJinja(skripte(html));
  if (!code.trim()) continue;
  geprueft++;

  const bekannt = new Set(GLOBALS);
  if (nutztGemeinsames(html, f)) gemeinsameNamen.forEach(n => bekannt.add(n));
  else eigenstaendig++;

  let funde;
  try {
    funde = ungebunden(code, bekannt);
  } catch (e) {
    // Nicht parsebar heißt hier NICHT "in Ordnung" — jscheck.py prüft die
    // Syntax und würde denselben Fall melden. Sichtbar machen, nicht schlucken.
    console.log(`  ${f}: nicht parsebar — ${e.message}`);
    unlesbar++;
    continue;
  }
  const gesehen = new Set();
  for (const { name } of funde) {
    if (gesehen.has(name)) continue;
    gesehen.add(name);
    const zeile = code.slice(0, funde.find(x => x.name === name).pos).split('\n').length;
    console.log(`  ${f}: \`${name}\` ist in diesem Geltungsbereich nicht gebunden (ab Zeile ~${zeile} im JS)`);
    treffer++;
  }
}

console.log(`\n  ${geprueft} Vorlagen geprüft (${eigenstaendig} ohne gemeinsames JavaScript), `
          + `${treffer} ungebundene(r) Bezeichner`
          + (unlesbar ? `, ${unlesbar} nicht parsebar` : '') + '.');
process.exit(treffer || unlesbar ? 1 : 0);
