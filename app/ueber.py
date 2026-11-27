"""Herausgeber und Produkt — EINE Quelle für die Fußzeile „Über“ (base.html).

Vorlage ist die Fußzeile der Produktwebseite (exogateway.zarenko.net):
Logo, Herausgeber, Produkt und Fassung, Produktseite, Blog, Impressum.
Datenschutz verweist auf die Erklärung des Produkts (seit v1.12.178). Bewusst der PRODUKTNAME, nicht `GATEWAY_NAME`: Die Zeile sagt,
wer die Software herausgibt — der Anzeigename einer Installation steht oben
im Menü. Eine Support-Adresse steht hier (noch) nicht.

Geprüft von tests/test_ueber_fusszeile.py.
"""
from __future__ import annotations

HERAUSGEBER = "Alexander Zarenko – IT Consulting"
PRODUKT = "EXO Signature Gateway"
LINKS = (
    ("Produktseite", "https://exogateway.zarenko.net/"),
    ("Blog", "https://blog.zarenko.net/"),
    ("Impressum", "https://exogateway.zarenko.net/impressum.html"),
    # Datenschutzerklärung des PRODUKTS (Gateway & Hub), nicht der Webseite —
    # dieselbe, der bei der Hub-Anbindung zugestimmt wird (legal/, product-privacy).
    ("Datenschutz (Produkt)", "https://sighub.zarenko.net/legal/product-privacy"),
)
