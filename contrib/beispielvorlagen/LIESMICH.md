# Beispielvorlagen

Neun Vorlagen zum Ausprobieren, alle im Baukasten bearbeitbar. Importieren:
*Signaturen → Vorlagen → Importieren …* (ab v1.12.123), `beispielvorlagen.zip`
hochladen und auswählen, was hinein soll. Eine Vorlage gleichen Namens wird
nicht überschrieben, sondern umbenannt.

| Vorlage | Art | Gedacht für |
|---|---|---|
| `Klassisch_Logo` | Signatur | Standardsignatur mit Kontaktzeilen, Anschrift und Logo darunter |
| `Zwei_Spalten` | Signatur | Logo links, Angaben rechts mit Trennlinie |
| `Kasten_modern` | Signatur | Angaben in einem Kasten mit runden Ecken (auch in Outlook Desktop) |
| `Geteiltes_Postfach` | Signatur | Geteilte Postfächer: Die schreibende Person steht oben, „für Postfach“ nur, wenn jemand für das Postfach schreibt (Baustein-Einstellung „Anzeigen wenn“) |
| `Intern_kurz` | Signatur | Interne Signatur (alle Empfänger intern) |
| `Antwort_kurz` | Signatur | Antwort-Signatur (ab der 2. eigenen Mail im Verlauf) |
| `Abwesenheit_de_en` | Abwesenheit | Zweisprachig, mit Zeitraum aus der Abwesenheit (`oof.zeitraum` / `oof.period`) |
| `Kampagne_Banner` | Banner | Hinweisband mit Etikett und Link, etwa für eine Kampagne |
| `Disclaimer_kurz` | Disclaimer | Vertraulichkeitshinweis deutsch/englisch |

Nach dem Import: Logo im Baustein *Logo* durch das eigene ersetzen, Texte und
die Adresse `info@example.com` anpassen.

`vorschau/` enthält je Vorlage ein Bild mit erfundenen Testdaten.

## Neu erzeugen

Die Quelle ist die Bausteinliste in `erzeugen.py`. HTML und Text erzeugt der
Renderer des Gateways, die Bilder rendert die Signatur-Engine (Playwright):

    .venv/bin/python contrib/beispielvorlagen/erzeugen.py

`tests/test_beispielvorlagen.py` prüft, dass HTML und Text zur Bausteinliste
passen, das Paket sich fehlerfrei importieren lässt und jede Vorlage rendert.
