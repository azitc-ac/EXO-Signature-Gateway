"""LE-Erneuerungs-Mail: Deep-Link zum Neustart-Bereich — NAVIGATION, kein Trigger.

Sicherheitsinvariante: Der Link führt zur Seite/zum Anker (`/advanced#neustart`),
NICHT auf einen Aktions-Endpunkt. Ein GET-Link, der den Neustart direkt auslöst,
würde von Mail-Safe-Links vorab „detoniert" (ungewollter Neustart) — dieselbe
Lehre wie beim Zustimmungslink. Baut jemand den Link auf einen Aktions-Endpunkt
um, muss dieser Test fehlschlagen.
"""
import notification


def test_le_renewed_mail_hat_navigations_deeplink(monkeypatch):
    import aussenadresse
    captured = {}
    monkeypatch.setattr(notification, "_should_notify", lambda k: True)
    monkeypatch.setattr(notification, "_get_notify_to", lambda: "admin@example.org")
    monkeypatch.setattr(aussenadresse, "basis", lambda: "https://gw.example")
    monkeypatch.setattr(notification, "_graph_send",
                        lambda to, subj, html: captured.update(html=html, subj=subj) or True)

    assert notification.send_le_renewed("sig.example.net", "01.01.2027") is True
    html = captured["html"]
    assert 'href="https://gw.example/advanced#neustart"' in html      # Navigation zum Knopf
    assert "/api/restart" not in html                                 # KEIN Auto-Trigger im Mail-Link


def test_le_renewed_mail_ohne_basis_url_kein_knopf(monkeypatch):
    import aussenadresse
    captured = {}
    monkeypatch.setattr(notification, "_should_notify", lambda k: True)
    monkeypatch.setattr(notification, "_get_notify_to", lambda: "admin@example.org")
    monkeypatch.setattr(aussenadresse, "basis", lambda: "")
    monkeypatch.setattr(notification, "_graph_send",
                        lambda to, subj, html: captured.update(html=html) or True)

    notification.send_le_renewed("sig.example.net", "01.01.2027")
    assert "/advanced#neustart" not in captured["html"]               # ohne Basis-URL kein Link
