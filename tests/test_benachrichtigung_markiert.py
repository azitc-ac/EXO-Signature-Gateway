"""Benachrichtigungen per Graph tragen die Gateway-Markierung (v1.10.118).

Portal-Benachrichtigungen gehen vom Postfach des ursprünglichen Absenders aus.
Ist dieses Postfach aktiviert, führte die FromMemberOf-Regel die
Benachrichtigung ohne Markierung erneut über das Gateway; im DEV-Tenant kam sie
beim Empfänger nicht an. Jeder andere Rückweg in den Transport setzt die
Markierung bereits (graph_reinject, handler).
"""
import notification
import loop_detector


class _Antwort:
    status_code = 202
    text = ""


def test_graph_send_setzt_die_markierung(monkeypatch):
    gesendet = {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            gesendet.update(url=url, json=json)
            return _Antwort()

    monkeypatch.setattr(notification.graph_client, "_acquire_token", lambda: "t")
    monkeypatch.setattr(notification.httpx, "Client", _Client)
    monkeypatch.setattr(loop_detector, "marker_wert", lambda: "1-abcdef012345")
    monkeypatch.setattr(loop_detector, "header_name", lambda: "X-Sig-Applied")
    assert notification._graph_send("p@partner.example", "Betreff", "<p>x</p>",
                                    sender="alexw@firma.example")
    assert gesendet["url"].endswith("/users/alexw@firma.example/sendMail")
    assert gesendet["json"]["message"]["internetMessageHeaders"] == [
        {"name": "X-Sig-Applied", "value": "1-abcdef012345"}]
