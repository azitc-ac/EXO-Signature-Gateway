"""Mails an den Postfachinhaber rund um den Zertifikatsbezug.

Hintergrund: Die Bestätigungsmail der Zertifizierungsstelle trifft beim
Mitarbeiter unerwartet ein, meist englisch, von einem unbekannten Absender,
mit Klickaufforderung und 24-Stunden-Frist — Merkmal für Merkmal das Muster
einer Phishing-Mail. Sie warnt sogar selbst davor, zu bestätigen, wenn man
nichts bestellt hat. Bestellt hat aber der Arbeitgeber.

Ohne Einordnung aus der eigenen Domäne klickt ein geschulter Nutzer zu Recht
nicht — und der Bezug scheitert. Diese Tests halten fest, dass die Einordnung
existiert, das Richtige sagt und abschaltbar ist.
"""
import notification


def _abfangen(monkeypatch):
    """`_graph_send` ersetzen und die Aufrufe einsammeln."""
    gesendet = []
    monkeypatch.setattr(notification, "_graph_send",
                        lambda to, subject, html, *a, **k: gesendet.append(
                            {"to": to, "subject": subject, "html": html}) or True)
    monkeypatch.setattr(notification, "_should_notify", lambda *_: True)
    # _html_wrap() liest GATEWAY_NAME; settings_store wuerde dafuer /app/data
    # anlegen wollen. NIEMALS gegen das echte Datenverzeichnis testen —
    # siehe conftest.
    monkeypatch.setattr(notification.settings_store, "get", lambda *a, **k: "")
    return gesendet


def test_vorab_hinweis_geht_an_den_postfachinhaber(monkeypatch):
    g = _abfangen(monkeypatch)
    notification.send_user_cert_verification_pending("erika@example.org", "Certum")
    assert len(g) == 1
    assert g[0]["to"] == "erika@example.org"        # NICHT an die Administration


def test_vorab_hinweis_nennt_die_ca_und_entkraeftet_den_phishing_verdacht(monkeypatch):
    g = _abfangen(monkeypatch)
    notification.send_user_cert_verification_pending("erika@example.org", "Certum")
    h = g[0]["html"]
    assert "Certum" in h                    # der Name aus dem CA-Absender
    assert "echt" in h                      # ausdrueckliche Entwarnung
    assert "kein Passwort" in h             # was NICHT verlangt wird
    assert "installieren nichts" in h


def test_fertigmeldung_entwertet_die_installationsaufforderung(monkeypatch):
    """Die Ausstellungsmail der CA laedt zum Installieren ein — hier haelt der
    Server den Schluessel. Ohne Gegenrede landet der Nutzer in einer Sackgasse."""
    g = _abfangen(monkeypatch)
    notification.send_user_cert_ready("erika@example.org", "Certum")
    h = g[0]["html"]
    assert g[0]["to"] == "erika@example.org"
    assert "ignorieren" in h
    assert "nichts weiter tun" in h


def test_ca_name_wird_maskiert(monkeypatch):
    """Der Anbietername stammt aus dem Hub-Katalog, also von aussen."""
    g = _abfangen(monkeypatch)
    notification.send_user_cert_verification_pending("e@x.de", '<script>alert(1)</script>')
    assert "<script>" not in g[0]["html"]
    assert "&lt;script&gt;" in g[0]["html"]


def test_abschaltbar(monkeypatch):
    """Wer seine Mitarbeiter nicht anschreiben lassen will, muss das koennen."""
    gesendet = []
    monkeypatch.setattr(notification, "_graph_send",
                        lambda *a, **k: gesendet.append(a) or True)
    monkeypatch.setattr(notification, "_should_notify",
                        lambda schluessel: schluessel != "NOTIFY_USER_CERT")
    assert notification.send_user_cert_verification_pending("e@x.de", "Certum") is False
    assert notification.send_user_cert_ready("e@x.de", "Certum") is False
    assert gesendet == []


def test_anbieter_label_faellt_auf_die_kennung_zurueck():
    """Ohne Katalog lieber die Kennung als gar keinen Namen."""
    import hub_orders
    assert hub_orders._anbieter_label("certum") in ("certum", "Certum")
