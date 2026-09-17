"""Domänen-Routing in reinject.send() — Verdrahtung und Pass-through.

Prüft: im smtp-Modus werden routende Empfänger an `_relay_to_target` abgespalten
und der Rest geht an `_send_smtp`; in einem Graph-Modus ist das Routing inaktiv
(kein Abspalten); und der Fremd-Hop reicht die Mail byte-genau durch — OHNE den
DKIM-Strip, der nur für den EXO-Rückweg richtig ist.
"""
import reinject
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
    relay_calls, smtp_calls = [], []
    monkeypatch.setattr(reinject, "_relay_to_target",
                        lambda z, mf, rt, cb: relay_calls.append((z, list(rt))))
    monkeypatch.setattr(reinject, "_send_smtp",
                        lambda mf, rt, cb: smtp_calls.append(list(rt)))

    reinject.send("erika@zarenko.net",
                  ["a@contoso.de", "b@zarenko.net", "c@contoso.de"],
                  b"From: erika@zarenko.net\r\n\r\nkorpus")

    assert relay_calls == [("onprem", ["a@contoso.de", "c@contoso.de"])]
    assert smtp_calls == [["b@zarenko.net"]]


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

    reinject.send("erika@zarenko.net", ["a@contoso.de", "b@contoso.de"],
                  b"From: erika@zarenko.net\r\n\r\nkorpus")

    assert relay_calls == [("onprem", ["a@contoso.de", "b@contoso.de"])]
    assert smtp_calls == []          # kein EXO-Rest → gar kein _send_smtp


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
    # (Client-Zert-Wahl wird separat geprüft, s.u.)
    # BYTE-GENAU durchgereicht: DKIM bleibt, nichts angefasst
    assert content == raw
    assert b"DKIM-Signature" in content
    assert gesendet["host"] == "ex.local" and gesendet["port"] == 25
    assert "starttls" not in gesendet      # starttls=False → kein STARTTLS
    assert "login" not in gesendet         # kein user/pass → kein AUTH


def test_relay_to_target_praesentiert_separates_smtp_zert(monkeypatch, tmp_path):
    """Der on-prem-Hop präsentiert das SEPARATE SMTP-Zert (falls gesetzt) als
    Client-Zert — nicht das gemeinsame. So tritt der Gateway on-prem mit der
    Listener-Identität auf (TlsDomainCapabilities)."""
    import ssl as _ssl
    import smtp_cert
    import aussenadresse
    _fake_settings(monkeypatch, {
        "RELAY_TARGETS": {"onprem": {"host": "ex.local", "port": 25, "starttls": True}},
        "RELAY_TARGET_PW": {},
    })
    cf = tmp_path / "smtp_cert.pem"
    kf = tmp_path / "smtp_key.pem"
    cf.write_text("cert"); kf.write_text("key")
    monkeypatch.setattr(smtp_cert, "pfade", lambda: (str(cf), str(kf)))
    monkeypatch.setattr(aussenadresse, "ehlo_hostname", lambda: "gw.test")

    used = {}
    monkeypatch.setattr(_ssl.SSLContext, "load_cert_chain",
                        lambda self, certfile, keyfile: used.update(cert=certfile))

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def ehlo(self):
            pass

        def starttls(self, context=None):
            pass

        def login(self, *a):
            pass

        def sendmail(self, *a):
            pass

    monkeypatch.setattr(reinject.smtplib, "SMTP", FakeSMTP)
    reinject._relay_to_target("onprem", "a@x.de", ["b@ex.local"], b"raw")

    # das Override-Zert, NICHT config.SMTP_TLS_CERT
    assert used.get("cert") == str(cf)
