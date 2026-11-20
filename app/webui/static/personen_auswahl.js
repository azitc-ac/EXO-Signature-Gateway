// Personenauswahl — EIN Baustein für jedes Feld, in dem man ein Postfach oder
// einen Benutzer sucht und übernimmt (seit v1.12.114; v1.12.113 noch lokal in
// mailboxes.html). Stile: _personen_auswahl_stil.html (eingebunden in base.html).
//
// Bis dahin gab es drei Bauweisen: eine <datalist> (zeigte auf Telefonen und in
// Teams nur Adressen) und zwei handgebaute Listen in den Einstellungen, deren
// Enter den ersten Eintrag über `div[style*="cursor:pointer"]` suchte — die
// Einträge hatten aber längst Klassen statt Inline-Stil, Enter tat nichts.
//
//   personenAuswahl(feld, {
//     eintraege: () => [{wert, name, adresse, gesperrt?, gruppe?}],
//     waehlen:   (wert, eintrag) => …,           // nach Tipp/Klick/Enter
//     frei:      (text) => …,                    // optional: Enter auf freiem Text mit „@“
//     beimFokus: true,                           // optional: Liste schon beim Antippen
//     nachTippen:(q) => …,                       // optional: z. B. Serversuche, dann .aktualisieren()
//     leerText:  'Kein aktiviertes Postfach passt.',   // oder Funktion (z. B. Fehler der Serversuche)
//   }) → {schliessen(), aktualisieren()}
//
// Das Feld steht in einer <span class="pa-huelle"> (Bezug für die Liste, die
// genau so breit ist wie das Feld). `gruppe` ergibt Zwischenüberschriften; die
// Einträge kommen in der Reihenfolge, in der eintraege() sie liefert.
// Farben setzt das Skript keine (Dark-Mode-Regel 2): Zustände über .pa-aktiv,
// aria-disabled und hidden.

/* global esc, escAttr */  // common.js — jede nutzende Seite erbt es über base.html

function _paNorm(t) {
  return String(t || '').normalize('NFD').replace(/[̀-ͯ]/g, '')
    .replace(/ß/g, 'ss').toLowerCase().trim();
}
function _paUmlaut(t) {
  return String(t || '').replace(/ä/g, 'ae').replace(/ö/g, 'oe').replace(/ü/g, 'ue')
    .replace(/Ä/g, 'Ae').replace(/Ö/g, 'Oe').replace(/Ü/g, 'Ue');
}
// „mueller“ soll „Müller“ finden wie „muller“: der Name zählt in beiden
// Schreibweisen (Akzent weg und Umlaut als ae/oe/ue).
function _paPasst(e, q) {
  return !q || [_paNorm(e.name), _paNorm(_paUmlaut(e.name)), _paNorm(e.adresse)]
    .some(function (t) { return t.indexOf(q) >= 0; });
}

function personenAuswahl(feld, optionen) {
  // Fehlt das Feld (Abschnitt auf dieser Seite nicht gerendert), ohne Wirkung —
  // sonst bräche ein TypeError das ganze übrige Skript der Seite ab.
  if (!feld) return {schliessen: function () {}, aktualisieren: function () {}};
  var huelle = feld.parentNode;
  var liste = document.createElement('div');
  liste.className = 'pa-liste';
  liste.id = feld.id + '-liste';
  liste.setAttribute('role', 'listbox');
  liste.hidden = true;
  huelle.appendChild(liste);
  feld.setAttribute('role', 'combobox');
  feld.setAttribute('aria-autocomplete', 'list');
  feld.setAttribute('aria-controls', liste.id);
  feld.setAttribute('aria-expanded', 'false');
  var treffer = [], aktiv = -1;

  function element(i) { return document.getElementById(liste.id + '-' + i); }
  function zu() {
    liste.hidden = true; aktiv = -1;
    feld.setAttribute('aria-expanded', 'false');
    feld.removeAttribute('aria-activedescendant');
  }
  function markieren(i) {
    if (aktiv >= 0 && element(aktiv)) {
      element(aktiv).classList.remove('pa-aktiv');
      element(aktiv).setAttribute('aria-selected', 'false');
    }
    aktiv = i;
    var el = i >= 0 ? element(i) : null;
    if (el) {
      el.classList.add('pa-aktiv');
      el.setAttribute('aria-selected', 'true');
      feld.setAttribute('aria-activedescendant', el.id);
      el.scrollIntoView({block: 'nearest'});
    } else {
      feld.removeAttribute('aria-activedescendant');
    }
  }
  function zeigen() {
    var q = _paNorm(feld.value);
    treffer = optionen.eintraege().filter(function (e) { return _paPasst(e, q); });
    var html = [], gruppe = null;
    treffer.forEach(function (e, i) {
      if (e.gruppe && e.gruppe !== gruppe) {
        gruppe = e.gruppe;
        html.push('<div class="pa-kopf" role="presentation">' + esc(gruppe) + '</div>');
      }
      var name = e.name && e.name !== e.adresse ? e.name : e.adresse;
      html.push('<div class="pa-eintrag" role="option" id="' + escAttr(liste.id + '-' + i) + '" data-i="' + i + '"' +
        ' title="' + escAttr(name + (name !== e.adresse ? ' <' + e.adresse + '>' : '') +
                             (e.gesperrt ? ' — ' + e.gesperrt : '')) + '"' +
        (e.gesperrt ? ' aria-disabled="true"' : '') + '>' +
        '<span class="pa-name">' + esc(name) + '</span>' +
        (name !== e.adresse ? '<span class="pa-adresse">' + esc(e.adresse) + '</span>' : '') +
        (e.gesperrt ? '<span class="pa-adresse">' + esc(e.gesperrt) + '</span>' : '') + '</div>');
    });
    liste.innerHTML = html.length ? html.join('')
      : '<div class="pa-leer">' + esc((typeof optionen.leerText === 'function' ? optionen.leerText()
                                       : optionen.leerText) || 'Kein Eintrag passt.') + '</div>';
    liste.hidden = false;
    feld.setAttribute('aria-expanded', 'true');
    aktiv = -1;
    markieren(-1);
  }
  function waehlbar(i) { return treffer[i] && !treffer[i].gesperrt; }
  function nehmen(i) {
    if (!waehlbar(i)) return;
    var e = treffer[i];
    feld.value = '';
    zu();
    optionen.waehlen(e.wert, e);
  }
  function schritt(r) {
    if (liste.hidden) zeigen();
    var len = treffer.length, start = aktiv >= 0 ? aktiv : (r > 0 ? -1 : len);
    for (var n = 1; n <= len; n++) {
      var i = (((start + r * n) % len) + len) % len;
      if (waehlbar(i)) { markieren(i); return; }
    }
  }
  feld.addEventListener('input', function () {
    zeigen();
    if (optionen.nachTippen) optionen.nachTippen(feld.value.trim());
  });
  feld.addEventListener('focus', function () { if (feld.value || optionen.beimFokus) zeigen(); });
  feld.addEventListener('keydown', function (e) {
    if (e.key === 'ArrowDown') { schritt(1); e.preventDefault(); }
    else if (e.key === 'ArrowUp') { schritt(-1); e.preventDefault(); }
    else if (e.key === 'Escape') { if (!liste.hidden) { zu(); e.preventDefault(); e.stopPropagation(); } }
    else if (e.key === 'Enter') {
      e.preventDefault();
      if (!liste.hidden && aktiv >= 0) { nehmen(aktiv); return; }
      var q = _paNorm(feld.value);
      if (!q) return;
      if (liste.hidden) zeigen();
      // Genau getippte Adresse, sonst ein einziger wählbarer Treffer.
      var genau = -1, wahl = [];
      treffer.forEach(function (t, i) {
        if (_paNorm(t.adresse) === q) genau = i;
        if (!t.gesperrt) wahl.push(i);
      });
      if (genau >= 0) { nehmen(genau); return; }       // gesperrt → nimmt nichts, Liste zeigt den Grund
      if (wahl.length === 1) { nehmen(wahl[0]); return; }
      if (optionen.frei && q.indexOf('@') > 0) { zu(); optionen.frei(feld.value); }
    }
    else if (e.key === 'Tab') { zu(); }
  });
  // mousedown statt click: das Feld behält den Fokus (auf dem Telefon bleibt
  // die Tastatur offen), und das Schließen „Klick daneben“ greift nicht vorher.
  liste.addEventListener('mousedown', function (e) {
    e.preventDefault();
    var it = e.target.closest('.pa-eintrag');
    if (it) nehmen(+it.dataset.i);
  });
  document.addEventListener('pointerdown', function (e) {
    if (!liste.hidden && !huelle.contains(e.target)) zu();
  });
  return {
    schliessen: zu,
    // Nach nachgeladenen Daten (Serversuche, neue Mitglieder): neu zeichnen,
    // aber nur, wenn die Liste gerade offen ist.
    aktualisieren: function () { if (!liste.hidden) zeigen(); },
  };
}
