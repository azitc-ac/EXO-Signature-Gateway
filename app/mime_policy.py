"""Die eine Policy, mit der empfangene Mails für den Rückweg neu serialisiert werden.

Zwei Anforderungen, beide verbindlich:

1. **CRLF.** Ohne SMTP-Policy erzeugt `as_bytes()` bare LF; Exchange zerlegt
   ein Multipart mit bare-LF-Boundaries nicht (leerer Body), und S/MIME
   erwartet CRLF als Kanonform. Siehe tests/test_reinject_crlf.py.

2. **Fremde Kopfzeilen nicht umkodieren.** `email.policy.SMTP` faltet bei 78
   Zeichen. Eine Kopfzeile, die das nicht hergibt — die Message-ID von
   Exchange Online ist 85 Zeichen lang und hat kein Leerzeichen — schreibt das
   email-Paket dann als RFC-2047-Wort (`=?utf-8?q?=3CAMBPR05…=40…?=`). In
   `References` und `In-Reply-To` erkennt darin kein Mailprogramm mehr eine
   Kennung: Beim Empfänger zerfällt die Konversation, und eine Antwort, die
   wieder durchs Gateway läuft, wird nicht als Teil der Kette erkannt.

RFC 5322 §2.1.1 erlaubt 998 Zeichen je Zeile; 78 ist nur eine Empfehlung.
Mit `max_line_length=998` lässt die Policy solche Zeilen, wie sie gekommen
sind, und faltet erst darüber. Alles andere — CRLF, das Falten der vom
Gateway selbst gesetzten Kopfzeilen — bleibt wie bei `email.policy.SMTP`.

Bewusst NICHT `refold_source="none"`: Damit blieben auch selbst gesetzte
Kopfzeilen mit Nicht-ASCII-Zeichen unkodiert, und das Serialisieren bräche mit
UnicodeEncodeError ab.
"""
import email.policy

SMTP_REINJECT = email.policy.SMTP.clone(max_line_length=998)
