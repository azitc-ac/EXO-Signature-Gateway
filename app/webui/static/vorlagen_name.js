// Anzeigename einer Vorlage. Die Standardvorlage heißt intern „default“
// (gespeichert in Zuordnungen, Richtlinien, Sicherungen) und in der Oberfläche
// „Standard“. Python-Gegenstück: signature_engine.anzeigename — beide hält
// tests/test_vorlage_standard_anzeige.py deckungsgleich.
function vorlageAnzeige(name) {
  return name === 'default' ? 'Standard' : (name || '');
}
