"""In-process self-tests for the mail processing pipeline.

Tests call mail_processor.inject() directly with synthetic MIME messages —
no network, no Exchange credentials, no external services required.

Each test checks that the gateway signature is placed in the correct position,
or correctly skipped when a gateway sig is already present in the thread.
"""

from __future__ import annotations

import email.mime.multipart
import email.mime.text
import textwrap
from dataclasses import dataclass, field
from typing import Optional

import mail_processor
import settings_store

# ── Test signature ─────────────────────────────────────────────────────────────
_SIG_HTML = textwrap.dedent("""\
    <div style="font-family:Arial,sans-serif;font-size:11pt">
    <b>Alexander Zarenko</b><br>
    Zarenko GmbH &middot; Gesch&auml;ftsf&uuml;hrer<br>
    Tel: <a href="tel:+493012345678">+49 30 123&nbsp;45678</a><br>
    <a href="mailto:alexander@zarenko.net">alexander@zarenko.net</a>
    &middot; <a href="https://zarenko.net">zarenko.net</a>
    </div>
""")

_SIG_TXT = "Alexander Zarenko | Zarenko GmbH | +49 30 123 45678 | alexander@zarenko.net"

# Outlook client sig — same key tokens → fingerprint match → STRIP in WordSection1
_CLIENT_SIG = (
    '<div>'
    'Alexander Zarenko<br>'
    'Zarenko GmbH | Gesch&auml;ftsf&uuml;hrer<br>'
    'Tel: +49 30 123 45678<br>'
    '<a href="mailto:alexander@zarenko.net">alexander@zarenko.net</a>'
    '</div>'
)

# ── HTML fragments ─────────────────────────────────────────────────────────────

_OUTLOOK_SEP = (
    '<div style="border:none;border-top:solid #E1E1E1 1.0pt;padding:3.0pt 0cm 0cm 0cm">'
    '<p class="MsoNormal"><b>Von:</b> Erika Musterfrau &lt;erika@example.com&gt;</p>'
    '<p class="MsoNormal"><b>Gesendet:</b> Montag, 30. Juni 2026 10:00</p>'
    '<p class="MsoNormal"><b>Betreff:</b> AW: Test</p>'
    '</div>'
)

_OUTLOOK_SEP_REVERSED_CSS = (
    '<div style="padding:3.0pt 0cm 0cm 0cm;border-top:solid #E1E1E1 1.0pt;border:none">'
    '<p class="MsoNormal"><b>Von:</b> Erika Musterfrau &lt;erika@example.com&gt;</p>'
    '</div>'
)

_QUOTED_CONTENT = (
    '<p class="MsoNormal">Guten Tag,</p>'
    '<p class="MsoNormal">vielen Dank f&uuml;r Ihre Nachricht.</p>'
    '<p class="MsoNormal">Mit freundlichen Gr&uuml;&szlig;en, Erika Musterfrau</p>'
)

_NESTED_INNER = (
    '<p class="MsoNormal">Von: Max Mustermann &lt;max@example.de&gt;</p>'
    '<p class="MsoNormal">Hallo, anbei die Anfrage von Erika:</p>'
    '<div id="divRplyFwdMsg">'
    '<p class="MsoNormal">Von: Erika Musterfrau &lt;erika@example.com&gt;</p>'
    '<p class="MsoNormal">Sehr geehrte Damen und Herren, bitte um Angebot.</p>'
    '</div>'
)


def _outlook_html(
    compose: str = "Hallo,<br>bitte finden Sie anbei...",
    separator: str = _OUTLOOK_SEP,
    quoted: str = _QUOTED_CONTENT,
    include_client_sig: bool = True,
) -> str:
    sig = _CLIENT_SIG if include_client_sig else ""
    return (
        "<html><head></head><body>"
        '<div class="WordSection1">'
        f"<p>{compose}</p>"
        + separator
        + quoted
        + sig
        + "</div></body></html>"
    )


def _multipart(html: str) -> email.message.Message:
    msg = email.mime.multipart.MIMEMultipart("alternative")
    msg["Subject"] = "Selbsttest"
    msg["From"] = "alexander@zarenko.net"
    msg["To"] = "test@example.com"
    msg.attach(email.mime.text.MIMEText("Testinhalt", "plain", "utf-8"))
    msg.attach(email.mime.text.MIMEText(html, "html", "utf-8"))
    return msg


def _html_only(html: str) -> email.message.Message:
    msg = email.mime.text.MIMEText(html, "html", "utf-8")
    msg["Subject"] = "Selbsttest"
    msg["From"] = "alexander@zarenko.net"
    msg["To"] = "test@example.com"
    return msg


# ── Result type ────────────────────────────────────────────────────────────────

@dataclass
class TestResult:
    name: str
    passed: bool
    detail: str
    input_html: str = field(default="")
    output_html: str = field(default="")


# ── Active sig (set by run_all, used by every test via _run) ──────────────────
_active_sig_html: str = _SIG_HTML
_active_sig_txt: str = _SIG_TXT


# ── Helpers ────────────────────────────────────────────────────────────────────

def _run(msg: email.message.Message) -> tuple[email.message.Message, str]:
    """Inject and return (result_msg, output_html)."""
    result = mail_processor.inject(msg, _active_sig_html, _active_sig_txt)
    return result, mail_processor.extract_html(result) or ""


def _sig_pos(html: str) -> Optional[int]:
    p = html.find(mail_processor._SIG_MARKER_START)
    return p if p != -1 else None


def _find(html: str, *fragments: str) -> Optional[int]:
    for f in fragments:
        p = html.lower().find(f.lower())
        if p != -1:
            return p
    return None


def _ok(name: str, detail: str, i: str = "", o: str = "") -> TestResult:
    return TestResult(name, True, detail, i, o)


def _fail(name: str, detail: str, i: str = "", o: str = "") -> TestResult:
    return TestResult(name, False, detail, i, o)


# ── Individual tests ───────────────────────────────────────────────────────────

def test_new_email() -> TestResult:
    name = "Neue E-Mail (kein Zitat-Block)"
    html_in = (
        "<html><body>"
        "<p>Hallo,</p>"
        "<p>bitte finden Sie anbei unser Angebot.</p>"
        "</body></html>"
    )
    _, html_out = _run(_multipart(html_in))
    sp = _sig_pos(html_out)
    if sp is None:
        return _fail(name, "Gateway-Sig-Marker nicht im Ergebnis gefunden", html_in, html_out)
    body_pos = html_out.lower().rfind("</body>")
    if body_pos == -1:
        return _fail(name, "Kein </body> im Ergebnis", html_in, html_out)
    if sp < body_pos:
        return _ok(name, f"Sig vor </body> bei pos {sp} (</body> bei {body_pos})", html_in, html_out)
    return _fail(name, f"Sig-Marker bei {sp} ist NACH </body> bei {body_pos}", html_in, html_out)


def test_outlook_desktop_reply() -> TestResult:
    name = "Outlook Desktop Reply (Separator border:none + 1pt)"
    html_in = _outlook_html()
    _, html_out = _run(_multipart(html_in))
    sp = _sig_pos(html_out)
    if sp is None:
        return _fail(name, "Gateway-Sig-Marker nicht im Ergebnis gefunden", html_in, html_out)
    sep_pos = _find(html_out, "border:none", "border-top:solid")
    if sep_pos is None:
        return _fail(name, "Outlook-Separator nicht im Ergebnis gefunden", html_in, html_out)
    if sp < sep_pos:
        return _ok(name, f"Sig bei pos {sp}, vor Separator bei {sep_pos} ✓", html_in, html_out)
    return _fail(name, f"Sig bei {sp} ist NACH Separator bei {sep_pos}", html_in, html_out)


def test_outlook_desktop_css_order() -> TestResult:
    name = "Outlook Desktop: CSS-Properties in umgekehrter Reihenfolge"
    html_in = _outlook_html(separator=_OUTLOOK_SEP_REVERSED_CSS)
    _, html_out = _run(_multipart(html_in))
    sp = _sig_pos(html_out)
    if sp is None:
        return _fail(name, "Gateway-Sig-Marker nicht gefunden — Separator vermutlich nicht erkannt", html_in, html_out)
    sep_pos = _find(html_out, "padding:3.0pt", "border-top:solid")
    if sep_pos is None:
        return _fail(name, "Separator im Ergebnis nicht auffindbar", html_in, html_out)
    if sp < sep_pos:
        return _ok(name, f"Sig bei {sp}, vor umgekehrtem Separator bei {sep_pos} ✓", html_in, html_out)
    return _fail(name, f"Sig bei {sp} ist NACH Separator bei {sep_pos}", html_in, html_out)


def test_owa_reply() -> TestResult:
    name = "OWA Reply (divRplyFwdMsg)"
    html_in = (
        "<html><body>"
        "<p>Danke f&uuml;r Ihre Nachricht.</p>"
        '<div id="divRplyFwdMsg">'
        "<hr>"
        "<b>Von:</b> Erika Musterfrau &lt;erika@example.com&gt;<br>"
        "<b>Gesendet:</b> 30. Juni 2026"
        "</div>"
        "<p>Vorherige Nachricht hier.</p>"
        "</body></html>"
    )
    _, html_out = _run(_multipart(html_in))
    sp = _sig_pos(html_out)
    if sp is None:
        return _fail(name, "Gateway-Sig-Marker nicht gefunden", html_in, html_out)
    sep_pos = _find(html_out, 'id="divRplyFwdMsg"', "id='divrplyfwdmsg'")
    if sep_pos is None:
        return _fail(name, "divRplyFwdMsg nicht im Ergebnis gefunden", html_in, html_out)
    if sp < sep_pos:
        return _ok(name, f"Sig bei {sp}, vor divRplyFwdMsg bei {sep_pos} ✓", html_in, html_out)
    return _fail(name, f"Sig bei {sp} ist NACH divRplyFwdMsg bei {sep_pos}", html_in, html_out)


def test_ios_mail_reply() -> TestResult:
    name = "iOS Mail Reply (blockquote)"
    html_in = (
        "<html><body>"
        "<div>Hallo,</div>"
        "<div>danke f&uuml;r Ihre Nachricht.</div>"
        '<blockquote type="cite">'
        "<div>Zitierte Nachricht hier.</div>"
        "</blockquote>"
        "</body></html>"
    )
    _, html_out = _run(_multipart(html_in))
    sp = _sig_pos(html_out)
    if sp is None:
        return _fail(name, "Gateway-Sig-Marker nicht gefunden", html_in, html_out)
    bq_pos = _find(html_out, "<blockquote")
    if bq_pos is None:
        return _fail(name, "blockquote nicht im Ergebnis gefunden", html_in, html_out)
    if sp < bq_pos:
        return _ok(name, f"Sig bei {sp}, vor blockquote bei {bq_pos} ✓", html_in, html_out)
    return _fail(name, f"Sig bei {sp} ist NACH blockquote bei {bq_pos}", html_in, html_out)


def test_nested_thread() -> TestResult:
    """Verschachtelter Thread: innerer divRplyFwdMsg tief im Thread darf nicht gewinnen."""
    name = "Verschachtelter Thread (Sig vor äußerem Separator, nicht vor innerem)"
    html_in = (
        "<html><head></head><body>"
        '<div class="WordSection1">'
        "<p>Ich leite das weiter.</p>"
        + _OUTLOOK_SEP
        + _NESTED_INNER
        + _CLIENT_SIG
        + "</div></body></html>"
    )
    _, html_out = _run(_multipart(html_in))
    sp = _sig_pos(html_out)
    if sp is None:
        return _fail(name, "Gateway-Sig-Marker nicht gefunden", html_in, html_out)
    sep_pos = _find(html_out, "border:none", "border-top:solid")
    inner_pos = _find(html_out, 'id="divRplyFwdMsg"')
    if sep_pos is None:
        return _fail(name, "Äußerer Outlook-Separator nicht im Ergebnis gefunden", html_in, html_out)
    if inner_pos is None:
        return _fail(name, "Innerer divRplyFwdMsg nicht im Ergebnis gefunden", html_in, html_out)
    if sp > inner_pos:
        return _fail(name, f"Sig bei {sp} ist NACH innerem divRplyFwdMsg bei {inner_pos} — falscher Einsetzpunkt", html_in, html_out)
    if sp < sep_pos:
        return _ok(name, f"Sig bei {sp}, vor äußerem Separator bei {sep_pos} (innerer bei {inner_pos}) ✓", html_in, html_out)
    return _fail(name, f"Sig bei {sp} ist NACH äußerem Separator bei {sep_pos}", html_in, html_out)


def test_skip_on_marker() -> TestResult:
    name = "SKIP: Gateway-Marker (<!-- exo-sig-start -->) im Thread"
    prev_sig = (
        mail_processor._SIG_MARKER_START
        + f'<div class="{mail_processor._SIG_CLASS}">' + _SIG_HTML + "</div>"
        + mail_processor._SIG_MARKER_END
    )
    html_in = (
        "<html><body>"
        "<p>Neue Antwort hier.</p>"
        + prev_sig
        + _OUTLOOK_SEP
        + _QUOTED_CONTENT
        + "</body></html>"
    )
    _, html_out = _run(_multipart(html_in))
    count = html_out.count(mail_processor._SIG_MARKER_START)
    if count == 1:
        return _ok(name, "SKIP korrekt — nur 1 Sig-Marker im Ergebnis ✓", html_in, html_out)
    if count == 0:
        return _fail(name, "Kein Marker im Ergebnis — Marker aus Eingabe verschwunden?", html_in, html_out)
    return _fail(name, f"Doppelte Injection! {count} Marker im Ergebnis", html_in, html_out)


def test_skip_on_class_sentinel() -> TestResult:
    name = "SKIP: Class-Sentinel (exo-gateway-sig) im Thread — iOS Mail Szenario"
    prev_sig_ios = f'<div class="{mail_processor._SIG_CLASS}">' + _SIG_HTML + "</div>"
    html_in = (
        "<html><body>"
        "<p>Neue Antwort hier.</p>"
        + prev_sig_ios
        + '<blockquote type="cite"><p>Vorherige Mail.</p></blockquote>'
        + "</body></html>"
    )
    _, html_out = _run(_multipart(html_in))
    count = html_out.count(mail_processor._SIG_MARKER_START)
    class_count = html_out.count(f'class="{mail_processor._SIG_CLASS}"')
    if count == 0 and class_count >= 1:
        return _ok(name, "SKIP korrekt — kein neuer Marker injiziert, Class-Sentinel erkannt ✓", html_in, html_out)
    if count > 0:
        return _fail(name, f"Doppelte Injection! {count} Marker im Ergebnis — Class-Sentinel wurde nicht erkannt", html_in, html_out)
    return _fail(name, f"Unerwarteter Zustand: marker={count}, class={class_count}", html_in, html_out)


def test_no_false_skip_on_client_sig() -> TestResult:
    name = "Kein falscher SKIP: Nur Outlook-Client-Sig, kein Gateway-Marker"
    html_in = (
        "<html><head></head><body>"
        '<div class="WordSection1">'
        "<p>Neue Antwort hier.</p>"
        + _OUTLOOK_SEP
        + _QUOTED_CONTENT
        + "<p>Beste Gr&uuml;&szlig;e aus der Vorgeschichte:</p>"
        + _CLIENT_SIG
        + _CLIENT_SIG
        + "</div></body></html>"
    )
    _, html_out = _run(_multipart(html_in))
    sp = _sig_pos(html_out)
    if sp is None:
        return _fail(name, "Gateway-Sig wurde NICHT injiziert — falscher SKIP_SIG_IN_THREAD!", html_in, html_out)
    return _ok(name, f"Korrekt injiziert bei pos {sp} — kein falscher SKIP ✓", html_in, html_out)


def test_client_sig_stripped() -> TestResult:
    name = "Outlook-Client-Sig wird vor Injection gestrippt"
    if settings_store.get("STRIP_CLIENT_SIGS") is False:
        return TestResult(name, True, "STRIP_CLIENT_SIGS deaktiviert — Test übersprungen")
    html_in = _outlook_html(include_client_sig=True)
    _, html_out = _run(_multipart(html_in))
    sp = _sig_pos(html_out)
    if sp is None:
        return _fail(name, "Gateway-Sig nicht gefunden", html_in, html_out)
    count = html_out.count(mail_processor._SIG_MARKER_START)
    if count != 1:
        return _fail(name, f"Erwartet 1 Sig-Marker, gefunden: {count}", html_in, html_out)
    return _ok(name, f"Gateway-Sig korrekt bei pos {sp}, Client-Sig gestrippt ✓", html_in, html_out)


def test_outlook_separator_not_stripped() -> TestResult:
    name = "Outlook-Separator wird NICHT als Sig-Kandidat gestrippt"
    html_in = _outlook_html(include_client_sig=False)
    _, html_out = _run(_multipart(html_in))
    sp = _sig_pos(html_out)
    if sp is None:
        return _fail(name, "Gateway-Sig nicht gefunden — Separator wurde möglicherweise fälschlich gestrippt", html_in, html_out)
    sep_pos = _find(html_out, "border:none", "border-top:solid")
    if sep_pos is None:
        return _fail(name, "Outlook-Separator wurde aus dem Ergebnis entfernt — fälschlich gestrippt!", html_in, html_out)
    if sp < sep_pos:
        return _ok(name, f"Separator erhalten bei {sep_pos}, Sig korrekt davor bei {sp} ✓", html_in, html_out)
    return _fail(name, f"Sig bei {sp} ist NACH Separator bei {sep_pos}", html_in, html_out)


def test_class_sentinel_in_result() -> TestResult:
    name = 'Ergebnis enthält class="exo-gateway-sig" Wrapper'
    html_in = "<html><body><p>Test</p></body></html>"
    _, html_out = _run(_multipart(html_in))
    if f'class="{mail_processor._SIG_CLASS}"' in html_out:
        return _ok(name, 'class="exo-gateway-sig" Wrapper im Ergebnis vorhanden ✓', html_in, html_out)
    return _fail(name, 'class="exo-gateway-sig" fehlt — iOS Mail Sentinel nicht eingebaut', html_in, html_out)


# ── Runner ─────────────────────────────────────────────────────────────────────

_ALL_TESTS = [
    test_new_email,
    test_outlook_desktop_reply,
    test_outlook_desktop_css_order,
    test_owa_reply,
    test_ios_mail_reply,
    test_nested_thread,
    test_skip_on_marker,
    test_skip_on_class_sentinel,
    test_no_false_skip_on_client_sig,
    test_client_sig_stripped,
    test_outlook_separator_not_stripped,
    test_class_sentinel_in_result,
]


def run_all(sig_html: str | None = None, sig_txt: str | None = None) -> dict:
    global _active_sig_html, _active_sig_txt
    _active_sig_html = sig_html if sig_html is not None else _SIG_HTML
    _active_sig_txt  = sig_txt  if sig_txt  is not None else _SIG_TXT
    using_real_sig = sig_html is not None
    try:
        results = []
        for fn in _ALL_TESTS:
            try:
                r = fn()
            except Exception as exc:
                r = TestResult(name=fn.__name__, passed=False, detail=f"Exception: {exc}")
            results.append(r)
    finally:
        _active_sig_html = _SIG_HTML
        _active_sig_txt  = _SIG_TXT

    passed = sum(1 for r in results if r.passed)
    return {
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "using_real_sig": using_real_sig,
        "results": [
            {
                "name": r.name,
                "passed": r.passed,
                "detail": r.detail,
                "input_html": r.input_html,
                "output_html": r.output_html,
            }
            for r in results
        ],
    }
