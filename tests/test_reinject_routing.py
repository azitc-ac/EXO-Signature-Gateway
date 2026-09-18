"""Domänen-Routing in reinject.send() — Verdrahtung und Pass-through.

Prüft: im smtp-Modus werden routende Empfänger an `_relay_to_target` abgespalten
und der Rest geht an `_send_smtp`; in einem Graph-Modus ist das Routing inaktiv
(kein Abspalten); und der Fremd-Hop reicht die Mail byte-genau durch — OHNE den
DKIM-Strip, der nur für den EXO-Rückweg richtig ist.
"""
import reinject
import relay_stats
import settings_store
import domain_routing


def _fake_settings(monkeypatch, werte):
    monkeypatch.setattr(settings_store, "get", lambda k, *a, **kw: werte.get(k))


def _neutralisiere_umfeld(monkeypatch):
    """send() ruft Nachbarmodule — hier auf harmlose Vorgaben setzen."""
    import exo_mailboxes
    import sende_identitaeten
    monkeypatch.setattr(exo_mailboxes, "known_addresses", set)
    monkeypatch.setattr(sende_identitaeten, "ist_identitaets_adresse",
                        lambda a: False)


def test_smtp_modus_spaltet_routende_empfaenger_ab(monkeypatch):
    _neutralisiere_umfeld(monkeypatch)
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "ex.local"}},
        "DOMAIN_ROUTES": {"contoso.de": "onprem"},
        "IDENT_DELIVER_VIA_GRAPH": False,
    })
    relay_calls, smtp_calls, stats_calls = [], [], []
    monkeypatch.setattr(reinject, "_relay_to_target",
                        lambda z, mf, rt, cb: relay_calls.append((z, list(rt))))
    monkeypatch.setattr(reinject, "_send_smtp",
                        lambda mf, rt, cb: smtp_calls.append(list(rt)))
    monkeypatch.setattr(relay_stats, "merke_routing",
                        lambda z, bytes_=0, ok=True: stats_calls.append((z, ok)))

    reinject.send("erika@zarenko.net",
                  ["a@contoso.de", "b@zarenko.net", "c@contoso.de"],
                  b"From: erika@zarenko.net\r\n\r\nkorpus")

    assert relay_calls == [("onprem", ["a@contoso.de", "c@contoso.de"])]
    assert smtp_calls == [["b@zarenko.net"]]
    assert stats_calls == [("onprem", True)]        # geroutete Mail wird verbucht


def test_alle_empfaenger_gerouted_kein_exo_versand(monkeypatch):
    _neutralisiere_umfeld(monkeypatch)
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "ex.local"}},
        "DOMAIN_ROUTES": {"contoso.de": "onprem"},
        "IDENT_DELIVER_VIA_GRAPH": False,
    })
    relay_calls, smtp_calls = [], []
    monkeypatch.setattr(reinject, "_relay_to_target",
                        lambda z, mf, rt, cb: relay_calls.append((z, list(rt))))
    monkeypatch.setattr(reinject, "_send_smtp",
                        lambda mf, rt, cb: smtp_calls.append(list(rt)))
    monkeypatch.setattr(relay_stats, "merke_routing",
                        lambda z, bytes_=0, ok=True: None)

    reinject.send("erika@zarenko.net", ["a@contoso.de", "b@contoso.de"],
                  b"From: erika@zarenko.net\r\n\r\nkorpus")

    assert relay_calls == [("onprem", ["a@contoso.de", "b@contoso.de"])]
    assert smtp_calls == []          # kein EXO-Rest → gar kein _send_smtp


def test_relay_fehler_wird_als_fehler_verbucht(monkeypatch):
    _neutralisiere_umfeld(monkeypatch)
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "smtp")
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "ex.local"}},
        "DOMAIN_ROUTES": {"contoso.de": "onprem"},
        "IDENT_DELIVER_VIA_GRAPH": False,
    })
    stats_calls, fail_calls = [], []
    monkeypatch.setattr(reinject, "_relay_to_target",
                        lambda z, mf, rt, cb: (_ for _ in ()).throw(RuntimeError("nope")))
    monkeypatch.setattr(reinject, "_send_smtp", lambda *a: None)
    monkeypatch.setattr(reinject, "_fail_delivery",
                        lambda *a, **k: fail_calls.append(a))
    monkeypatch.setattr(relay_stats, "merke_routing",
                        lambda z, bytes_=0, ok=True: stats_calls.append((z, ok)))

    reinject.send("erika@zarenko.net", ["a@contoso.de"],
                  b"From: erika@zarenko.net\r\n\r\nkorpus")

    assert stats_calls == [("onprem", False)]      # Fehlversuch als Fehler verbucht
    assert fail_calls                              # _fail_delivery wurde gerufen


def test_graph_modus_routing_inaktiv(monkeypatch):
    _neutralisiere_umfeld(monkeypatch)
    monkeypatch.setattr(settings_store, "reinject_mode", lambda: "graph")
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "ex.local"}},
        "DOMAIN_ROUTES": {"contoso.de": "onprem"},
        "IDENT_DELIVER_VIA_GRAPH": False,
        "GRAPH_MIXED_FORK_MODE": "scoped",
        "GRAPH_SMTP_FALLBACK": False,
    })
    relay_calls = []
    monkeypatch.setattr(reinject, "_relay_to_target",
                        lambda z, mf, rt, cb: relay_calls.append(z))
    # Graph-Versand abfangen, damit der Test nicht ins Netz greift
    import graph_reinject
    graph_calls = []
    monkeypatch.setattr(graph_reinject, "send_via_graph",
                        lambda mf, rt, cb: graph_calls.append(list(rt)) or True)
    monkeypatch.setattr(graph_reinject, "send_via_graph_mime",
                        lambda mf, rt, cb: graph_calls.append(list(rt)) or True)

    reinject.send("erika@zarenko.net", ["a@contoso.de", "b@zarenko.net"],
                  b"From: erika@zarenko.net\r\n\r\nkorpus")

    assert relay_calls == []          # kein Routing im Graph-Modus
    assert graph_calls == [["a@contoso.de", "b@zarenko.net"]]   # volle Liste


def test_relay_to_target_reicht_byte_genau_durch_ohne_dkim_strip(monkeypatch):
    """Der Fremd-Hop darf NICHT strippen — sonst bräche eine gültige Signatur."""
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "ex.local", "port": 25,
                                     "starttls": False}},
        "RELAY_TARGET_PW": {},
    })
    import aussenadresse
    monkeypatch.setattr(aussenadresse, "ehlo_hostname", lambda: "gw.test")

    gesendet = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=None, local_hostname=None):
            gesendet["host"] = host
            gesendet["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            gesendet["starttls"] = True

        def login(self, u, p):
            gesendet["login"] = (u, p)

        def sendmail(self, mf, rt, content):
            gesendet["args"] = (mf, list(rt), content)

    monkeypatch.setattr(reinject.smtplib, "SMTP", FakeSMTP)

    raw = (b"From: erika@zarenko.net\r\n"
           b"To: a@contoso.de\r\n"
           b"DKIM-Signature: v=1; a=rsa-sha256; d=zarenko.net; b=SIG==\r\n"
           b"\r\nkorpus")
    reinject._relay_to_target("onprem", "erika@zarenko.net",
                              ["a@contoso.de"], raw)

    mf, rt, content = gesendet["args"]
    assert mf == "erika@zarenko.net"
    assert rt == ["a@contoso.de"]
    # BYTE-GENAU durchgereicht: DKIM bleibt, nichts angefasst
    assert content == raw
    assert b"DKIM-Signature" in content
    assert gesendet["host"] == "ex.local" and gesendet["port"] == 25
    assert "starttls" not in gesendet      # starttls=False → kein STARTTLS
    assert "login" not in gesendet         # kein user/pass → kein AUTH
