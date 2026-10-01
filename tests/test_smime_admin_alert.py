"""Admin-Ablaufalarm NUR für eigene Signatur-Zertifikate, nicht für
Empfänger-Zertifikate (Kommunikationspartner).

Ein Empfänger-Zertifikat ist das Zertifikat eines Fremden — der Betreiber kann
es nicht erneuern, und es erneuert sich beim nächsten signierten Eingang von
selbst. Eine tägliche, dringlich wirkende Mail darüber ist Rauschen.

Der Test schlägt fehl, wenn Empfänger-Zertifikate wieder in den Admin-Alarm
aufgenommen werden (die zuvor behobene Lärmquelle).
"""
import scheduler
import smime_store
import notification


def _run(monkeypatch, signing, recipient):
    gesendet = []
    monkeypatch.setattr(smime_store, "list_certs", lambda: signing)
    monkeypatch.setattr(smime_store, "list_recipient_certs", lambda: recipient)
    monkeypatch.setattr(notification, "send_cert_expiry_alert",
                        lambda c: gesendet.append(c.get("email")) or True)
    monkeypatch.setattr(scheduler.settings_store, "get",
                        lambda k, *a, **kw: {"CERT_WARN_DAYS": 14,
                                             "CERT_RENEWAL_THRESHOLDS": [30, 14, 7, 1],
                                             "CA_USER_CONFIG": {},
                                             "NOTIFY_SMIME_EXPIRY": True}.get(k, None))
    scheduler._cert_alerts_sent.clear()
    scheduler._user_notif_sent.clear()
    scheduler._check_smime_lifecycle()
    return gesendet


def test_eigenes_signaturzert_loest_alarm_aus(monkeypatch):
    gesendet = _run(monkeypatch,
                    signing=[{"email": "erika@zarenko.net", "days_left": 5}],
                    recipient=[])
    assert "erika@zarenko.net" in gesendet


def test_empfaengerzert_loest_keinen_alarm_aus(monkeypatch):
    """Der eigentliche Regressionswächter: ein ablaufendes Partner-Zert darf
    KEINEN Admin-Alarm erzeugen. Nimmt man Empfänger-Zertifikate wieder in die
    Schleife auf, erscheint "partner@fremd.example" in `gesendet` → Fehlschlag."""
    gesendet = _run(monkeypatch,
                    signing=[],
                    recipient=[{"email": "partner@fremd.example", "days_left": 3}])
    assert gesendet == [], f"Partner-Zert hätte keinen Alarm auslösen dürfen: {gesendet}"
