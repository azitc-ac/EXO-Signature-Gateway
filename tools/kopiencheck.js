#!/usr/bin/env node
/*
 * Sucht KOPIEN im JavaScript der Oberfläche: gleich gebaute Code-Stücke an
 * mehreren Stellen, auch wenn die Variablen anders heißen.
 *
 * Anlass (2026-10-21): Sieben Stellen öffneten ein Anmeldefenster in vier
 * Bauweisen; die jüngste blockierte der Browser stumm. Die Konsolidierung vom
 * 26.07. hatte das nicht erfasst, weil sie nach BEKANNTEN Kategorien vorging
 * (Escaper, Speichern, Meldungen …) und `driftcheck.py` nur diese prüft. Eine
 * Kopie, die noch niemand als Kategorie benannt hat, sieht keine Prüfung.
 * Dieses Werkzeug sucht ohne Kategorie: nach Ähnlichkeit.
 *
 * Verfahren: Inline-Skripte der Vorlagen (Jinja neutralisiert wie in
 * jsscopecheck.js) und die eigenen .js-Dateien werden mit acorn in Tokens
 * zerlegt. Bezeichner werden zu `I`, Zeichenketten zu `S`, Zahlen zu `N` —
 * AUSSER nach einem Punkt: Eigenschaftsnamen (`.location.href`, `.open`)
 * bleiben stehen, denn sie tragen die Bedeutung. Gleiche Folgen von mindestens
 * MIN Tokens an zwei Stellen gelten als Kopie und werden maximal verlängert.
 *
 * Was es NICHT sieht: Kopien mit umgestellter Reihenfolge oder eingeschobenen
 * Zeilen (dann zerfällt die Folge unter MIN), und Kopien in Python.
 *
 * Aufruf:
 *   node tools/kopiencheck.js [--min=N] [--json] PFAD…
 *   node tools/kopiencheck.js app/webui/templates app/webui/static
 * Ausgabe: Gruppen, längste zuerst, je Fundstelle Datei:Zeilen.
 *
 * Sperre (CI) mit Ausgangsstand:
 *   --stand=tools/kopien_stand.json             vergleicht, Exit 1 bei neuen Kopien
 *   --stand=tools/kopien_stand.json --schreiben schreibt den Stand fest
 * Der Stand hält je Datei fest, wie viele Tokens in Kopien stecken. Steigt die
 * Zahl einer Datei, ist eine Kopie dazugekommen (oder eine bestehende länger
 * geworden) — die CI schlägt fehl. Sinkt sie, ist das erwünscht; dann den Stand
 * neu schreiben, damit der Gewinn gesichert ist. Bewusst je Datei und nicht je
 * Gruppe: Gruppen-Kennungen verschieben sich bei jeder Bearbeitung in der Nähe,
 * die Summe nicht.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const acorn = require('acorn');

const args = process.argv.slice(2);
const MIN = Number((args.find(a => a.startsWith('--min=')) || '--min=60').slice(6));
const JSON_AUS = args.includes('--json');
const STAND = (args.find(a => a.startsWith('--stand=')) || '').slice(8);
const SCHREIBEN = args.includes('--schreiben');
const wurzeln = args.filter(a => !a.startsWith('--'));

// ── Quellen einsammeln ────────────────────────────────────────────────────────

function dateien(p) {
  const st = fs.statSync(p);
  if (st.isFile()) return [p];
  return fs.readdirSync(p).flatMap(n => {
    if (n === 'vendor' || n === 'node_modules') return [];
    return dateien(path.join(p, n));
  });
}

/** [{datei, zeile0, code}] — je Skriptblock mit Zeilenversatz in der Datei. */
function quellen(datei) {
  const text = fs.readFileSync(datei, 'utf8');
  if (datei.endsWith('.js')) return [{ datei, zeile0: 0, code: text }];
  if (!datei.endsWith('.html')) return [];
  const raus = [];
  for (const m of text.matchAll(/<script(?![^>]*\bsrc\s*=)[^>]*>([\s\S]*?)<\/script>/g)) {
    const anfang = m.index + m[0].indexOf('>') + 1;
    const zeile0 = text.slice(0, anfang).split('\n').length - 1;
    // Jinja neutralisieren, Zeilenumbrüche erhalten (Zeilennummern bleiben stimmig)
    const code = m[1]
      .replace(/\{\{[\s\S]*?\}\}/g, s => '0' + '\n'.repeat((s.match(/\n/g) || []).length))
      .replace(/\{%[\s\S]*?%\}/g, s => '\n'.repeat((s.match(/\n/g) || []).length))
      .replace(/\{#[\s\S]*?#\}/g, s => '\n'.repeat((s.match(/\n/g) || []).length));
    raus.push({ datei, zeile0, code });
  }
  return raus;
}

// ── Tokens normalisieren ──────────────────────────────────────────────────────

function tokens(q) {
  const raus = [];
  let vorher = null;
  try {
    for (const t of acorn.tokenizer(q.code, { ecmaVersion: 'latest', locations: true, allowHashBang: true })) {
      const typ = t.type.label;
      let k;
      if (typ === 'name') k = (vorher === '.' || vorher === '?.') ? '.' + t.value : 'I';
      else if (typ === 'string' || typ === 'template') k = 'S';
      else if (typ === 'num') k = 'N';
      else if (typ === 'regexp') k = 'R';
      else k = t.value !== undefined && typeof t.value === 'string' && t.type.keyword ? t.value : typ;
      raus.push({ k, zeile: q.zeile0 + t.loc.start.line });
      vorher = typ;
    }
  } catch (e) {
    // Syntaxfehler meldet jscheck.py; hier nur überspringen
  }
  return raus;
}

// ── Gleiche Folgen finden und maximal verlängern ──────────────────────────────

const qs = wurzeln.flatMap(dateien).sort().flatMap(quellen);
const toks = qs.map(tokens);

const fenster = new Map();          // Schlüssel → [[quelle, pos]]
toks.forEach((ts, qi) => {
  for (let i = 0; i + MIN <= ts.length; i++) {
    let k = '';
    for (let j = i; j < i + MIN; j++) k += ts[j].k + '\u0001';
    const liste = fenster.get(k);
    if (liste) { if (liste.length < 30) liste.push([qi, i]); }
    else fenster.set(k, [[qi, i]]);
  }
});

const treffer = [];                 // {a:[qi,von,bis], b:[qi,von,bis], n}
const gesehen = new Set();
for (const liste of fenster.values()) {
  if (liste.length < 2) continue;
  for (let x = 0; x < liste.length; x++) {
    for (let y = x + 1; y < liste.length; y++) {
      let [qa, a] = liste[x], [qb, b] = liste[y];
      const A = toks[qa], B = toks[qb];
      // nur links-maximale Starts (sonst jede Verschiebung derselben Kopie)
      if (a > 0 && b > 0 && A[a - 1].k === B[b - 1].k) continue;
      if (qa === qb && Math.abs(a - b) < MIN) continue;      // Selbstüberlappung
      let n = MIN;
      while (a + n < A.length && b + n < B.length && A[a + n].k === B[b + n].k
             && !(qa === qb && a + n >= b)) n++;
      const sig = `${qa}:${a}:${qb}:${b}`;
      if (gesehen.has(sig)) continue;
      gesehen.add(sig);
      treffer.push({ a: [qa, a, a + n - 1], b: [qb, b, b + n - 1], n });
    }
  }
}

// ── Zu Gruppen zusammenfassen (überlappende Stellen = dieselbe Stelle) ────────

const stellen = [];                 // {qi, von, bis}
const eltern = [];
function finde(i) { while (eltern[i] !== i) i = eltern[i] = eltern[eltern[i]]; return i; }
function stelle(qi, von, bis) {
  for (let i = 0; i < stellen.length; i++) {
    const s = stellen[i];
    if (s.qi === qi && von <= s.bis && bis >= s.von) {
      s.von = Math.min(s.von, von); s.bis = Math.max(s.bis, bis);
      return i;
    }
  }
  stellen.push({ qi, von, bis }); eltern.push(stellen.length - 1);
  return stellen.length - 1;
}
for (const t of treffer) {
  const i = stelle(...t.a), j = stelle(...t.b);
  eltern[finde(i)] = finde(j);
}

const gruppen = new Map();
stellen.forEach((s, i) => {
  const w = finde(i);
  if (!gruppen.has(w)) gruppen.set(w, []);
  gruppen.get(w).push(s);
});

const ergebnis = [...gruppen.values()]
  .filter(g => g.length >= 2)
  .map(g => {
    const orte = g.map(s => {
      const ts = toks[s.qi];
      return { datei: qs[s.qi].datei, von: ts[s.von].zeile, bis: ts[s.bis].zeile, tokens: s.bis - s.von + 1 };
    }).sort((x, y) => x.datei.localeCompare(y.datei) || x.von - y.von);
    return { tokens: Math.max(...orte.map(o => o.tokens)), orte };
  })
  .sort((x, y) => y.tokens * y.orte.length - x.tokens * x.orte.length);

// Tokens in Kopien je Datei (überlappende Stellen einer Datei nur einmal)
function jeDatei() {
  const bereiche = new Map();               // datei → [[von, bis]] in Tokens je Quelle
  for (const s of stellen) {
    if (!ergebnis.length) break;
    const k = qs[s.qi].datei + '\u0000' + s.qi;
    if (!bereiche.has(k)) bereiche.set(k, []);
    bereiche.get(k).push([s.von, s.bis]);
  }
  const summe = {};
  for (const [k, liste] of bereiche) {
    const datei = path.relative(process.cwd(), k.split('\u0000')[0]);
    liste.sort((a, b) => a[0] - b[0]);
    let n = 0, ende = -1;
    for (const [v, b] of liste) {
      if (b <= ende) continue;
      n += b - Math.max(v, ende + 1) + 1;
      ende = b;
    }
    summe[datei] = (summe[datei] || 0) + n;
  }
  return Object.fromEntries(Object.entries(summe).sort());
}

if (STAND) {
  const ist = jeDatei();
  if (SCHREIBEN) {
    fs.writeFileSync(STAND, JSON.stringify({ min: MIN, tokens_in_kopien: ist }, null, 1) + '\n');
    console.log(`Stand geschrieben: ${STAND} (${Object.keys(ist).length} Dateien, `
                + `${Object.values(ist).reduce((a, b) => a + b, 0)} Tokens in Kopien)`);
    process.exit(0);
  }
  const soll = JSON.parse(fs.readFileSync(STAND, 'utf8'));
  if (soll.min !== MIN) { console.log(`Stand wurde mit --min=${soll.min} geschrieben, Lauf mit ${MIN}.`); process.exit(1); }
  const mehr = [], weniger = [];
  for (const [d, n] of Object.entries(ist)) {
    const alt = soll.tokens_in_kopien[d] || 0;
    if (n > alt) mehr.push(`  ${d}: ${alt} → ${n} Tokens in Kopien`);
    else if (n < alt) weniger.push(d);
  }
  for (const d of Object.keys(soll.tokens_in_kopien)) if (!(d in ist)) weniger.push(d);
  if (mehr.length) {
    console.log('Neue Kopien (oder bestehende verlängert):\n' + mehr.join('\n'));
    console.log('\nDie Fundstellen zeigt: node tools/kopiencheck.js ' + wurzeln.join(' '));
    console.log('Gemeinsamen Baustein nutzen (common.js) statt kopieren. Ist die Kopie gewollt, '
                + 'den Stand mit --schreiben neu festhalten und das im Commit begründen.');
    process.exit(1);
  }
  console.log(`Keine neuen Kopien (${Object.keys(ist).length} Dateien mit Altbestand).`
              + (weniger.length ? ` Weniger als im Stand: ${weniger.join(', ')} — Stand neu schreiben (--schreiben).` : ''));
  process.exit(0);
}

if (JSON_AUS) {
  console.log(JSON.stringify(ergebnis, null, 1));
} else {
  for (const g of ergebnis) {
    console.log(`── ${g.orte.length} Stellen, ~${g.tokens} Tokens`);
    for (const o of g.orte) console.log(`   ${o.datei}:${o.von}-${o.bis}`);
  }
  const dateienBetroffen = new Set(ergebnis.flatMap(g => g.orte.map(o => o.datei))).size;
  console.log(`\n${ergebnis.length} Kopien-Gruppe(n) ab ${MIN} Tokens in ${dateienBetroffen} Datei(en); `
              + `${qs.length} Skriptblöcke aus ${new Set(qs.map(q => q.datei)).size} Dateien geprüft.`);
}
