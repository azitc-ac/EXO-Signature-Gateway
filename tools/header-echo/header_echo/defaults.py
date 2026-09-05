"""Vorgaben für IONOS. Andere Anbieter über ECHO_IMAP_HOST, ECHO_IMAP_PORT,
ECHO_SMTP_HOST und ECHO_SMTP_PORT einstellen. Bewusst ohne Zugangsdaten in
derselben Datei: Secret-Scanner lesen Host, Port, Nutzer und Passwort in
Nachbarschaft als hinterlegte Zugangsdaten."""

IONOS_IMAP = ("imap.ionos.de", 993)
IONOS_SMTP = ("smtp.ionos.de", 587)
