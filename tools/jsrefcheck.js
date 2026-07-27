#!/usr/bin/env node
/*
 * Sucht Funktionsaufrufe, deren Name nirgends definiert ist.
 *
 * Anlass (2026-07-27): `_reflowPlain()` in settings_connect.html rief viermal
 * `_mdEscape()` auf — eine Funktion, die es nicht gab. Bei der Zusammenführung
 * der handgeschriebenen Escaper auf `esc()` war diese Aufrufstelle stehen
 * geblieben. Ergebnis war ein ReferenceError innerhalb eines try-Blocks, den
 * der catch-Zweig in „Nutzungsbedingungen konnten nicht geladen werden"
 * übersetzte — eine Meldung, die auf das Falsche zeigt.
 *
 * `jscheck.py` fängt das NICHT: die Datei ist syntaktisch einwandfrei.
 *
 * Kommentare und Zeichenketten werden mit einem Zustandsautomaten entfernt,
 * nicht per Regex. Ein Apostroph in einem deutschen Kommentar („Outlook's")
 * lässt eine Regex-Lösung ganze Dateiregionen verschlucken — der erste Versuch
 * erkannte dadurch 5 von 40 Funktionen und meldete den Rest als undefiniert.
 * Regex-Literale müssen ebenfalls erkannt werden, sonst desynchronisiert
 * `/[&<>"']/g` den Automaten.
 */
'use strict';
const fs = require('fs');
const path = require('path');

const EINGEBAUT = new Set(['fetch', 'parseInt', 'parseFloat', 'alert', 'confirm', 'prompt',
  'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'encodeURIComponent',
  'decodeURIComponent', 'encodeURI', 'decodeURI', 'isNaN', 'isFinite', 'String', 'Number',
  'Boolean', 'Array', 'Object', 'JSON', 'Math', 'Date', 'RegExp', 'Error', 'Promise', 'Map',
  'Set', 'WeakMap', 'WeakSet', 'Symbol', 'BigInt', 'Proxy', 'Reflect', 'console', 'document',
  'window', 'navigator', 'location', 'history', 'screen', 'btoa', 'atob', 'FormData', 'Blob',
  'File', 'FileReader', 'URL', 'URLSearchParams', 'AbortController', 'EventSource', 'WebSocket',
  'XMLHttpRequest', 'Headers', 'Request', 'Response', 'Uint8Array', 'Int8Array', 'Uint16Array',
  'Uint32Array', 'Int32Array', 'Float32Array', 'Float64Array', 'ArrayBuffer', 'DataView',
  'TextEncoder', 'TextDecoder', 'DOMParser', 'XMLSerializer', 'Image', 'Audio', 'Option',
  'MouseEvent', 'KeyboardEvent', 'CustomEvent', 'Event', 'MutationObserver',
  'IntersectionObserver', 'ResizeObserver', 'getComputedStyle', 'requestAnimationFrame',
  'cancelAnimationFrame', 'queueMicrotask', 'structuredClone', 'Intl', 'Notification',
  'crypto', 'performance', 'matchMedia', 'open', 'close', 'print', 'scrollTo',
  // Office-Add-in-Laufzeit (wird als externes Skript geladen)
  'Office', 'OfficeRuntime', 'Excel', 'Word', 'Outlook']);

const SCHLUESSELWORT = new Set(['if', 'for', 'while', 'switch', 'catch', 'return', 'typeof',
  'new', 'await', 'function', 'else', 'do', 'delete', 'in', 'of', 'instanceof', 'void', 'throw',
  'case', 'yield', 'super', 'this', 'try', 'async', 'get', 'set', 'static', 'export', 'import']);

/** Kommentare, Zeichenketten und Regex-Literale durch Leerzeichen ersetzen. */
function nurCode(src) {
  let out = '';
  let i = 0;
  let letztesZeichen = '';        // letztes bedeutsames Zeichen — entscheidet / = Division oder Regex
  while (i < src.length) {
    const c = src[i], n = src[i + 1];
    if (c === '/' && n === '/') {
      while (i < src.length && src[i] !== '\n') i++;
      continue;
    }
    if (c === '/' && n === '*') {
      i += 2;
      while (i < src.length && !(src[i] === '*' && src[i + 1] === '/')) i++;
      i += 2;
      out += ' ';
      continue;
    }
    if (c === '"' || c === "'" || c === '`') {
      const ende = c;
      i++;
      while (i < src.length) {
        if (src[i] === '\\') { i += 2; continue; }
        if (src[i] === ende) { i++; break; }
        i++;
      }
      out += '""';
      letztesZeichen = '"';
      continue;
    }
    // Regex-Literal: ein '/' nach einem Operator/Klammer-auf, nicht nach Wert.
    if (c === '/' && !/[\w$)\]]/.test(letztesZeichen)) {
      i++;
      let inKlasse = false;
      while (i < src.length) {
        if (src[i] === '\\') { i += 2; continue; }
        if (src[i] === '[') inKlasse = true;
        else if (src[i] === ']') inKlasse = false;
        else if (src[i] === '/' && !inKlasse) { i++; break; }
        else if (src[i] === '\n') break;
        i++;
      }
      while (i < src.length && /[a-z]/.test(src[i])) i++;   // Flags
      out += ' RE ';
      letztesZeichen = 'E';
      continue;
    }
    out += c;
    if (!/\s/.test(c)) letztesZeichen = c;
    i++;
  }
  return out;
}

function definiert(code) {
  const s = new Set();
  for (const m of code.matchAll(/\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)/g)) s.add(m[1]);
  for (const m of code.matchAll(/\b(?:var|let|const)\s+([A-Za-z_$][\w$]*)/g)) s.add(m[1]);
  for (const m of code.matchAll(/\bclass\s+([A-Za-z_$][\w$]*)/g)) s.add(m[1]);
  // window.foo = …  bzw.  foo = function/async
  for (const m of code.matchAll(/(?:window\.)?([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function/g)) s.add(m[1]);
  for (const m of code.matchAll(/(?:window\.)?([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>/g)) s.add(m[1]);
  // Parameter und Destrukturierung — Rückruffunktionen heißen oft wie Aufrufe
  for (const m of code.matchAll(/\(([^)(]*)\)\s*(?:=>|\{)/g)) {
    for (const teil of m[1].split(',')) {
      const t = teil.trim().replace(/=.*$/, '').trim();
      if (/^[A-Za-z_$][\w$]*$/.test(t)) s.add(t);
    }
  }
  return s;
}

function skripte(html) {
  return [...html.matchAll(/<script(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map(m => m[1]).join('\n');
}

const dir = process.argv[2];
const gemeinsam = process.argv.slice(3);
if (!dir) {
  console.error('Aufruf: jsrefcheck.js <vorlagen-verzeichnis> [gemeinsame.js …]');
  process.exit(2);
}

const global = new Set();
for (const f of gemeinsam) {
  if (fs.existsSync(f)) definiert(nurCode(fs.readFileSync(f, 'utf8'))).forEach(n => global.add(n));
}
// base.html wird von allen Seiten erweitert — seine Helfer stehen dort bereit.
const basis = path.join(dir, 'base.html');
if (fs.existsSync(basis)) {
  definiert(nurCode(skripte(fs.readFileSync(basis, 'utf8')))).forEach(n => global.add(n));
}

let treffer = 0, geprueft = 0;
for (const f of fs.readdirSync(dir).filter(x => x.endsWith('.html')).sort()) {
  const code = nurCode(skripte(fs.readFileSync(path.join(dir, f), 'utf8')));
  if (!code.trim()) continue;
  geprueft++;
  const lokal = definiert(code);
  const gesehen = new Set();
  for (const m of code.matchAll(/(^|[^\w$.])([A-Za-z_$][\w$]*)\s*\(/g)) {
    const n = m[2];
    if (gesehen.has(n) || lokal.has(n) || global.has(n)
        || EINGEBAUT.has(n) || SCHLUESSELWORT.has(n)) continue;
    gesehen.add(n);
    console.log(`  ${f}: ${n}(…) ist nirgends definiert`);
    treffer++;
  }
}
console.log(`\n  ${geprueft} Vorlagen geprüft, ${treffer} nirgends definierte(r) Aufruf(e).`);
process.exit(treffer ? 1 : 0);
