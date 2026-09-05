"""Header-Echo: beantwortet eingehende Mails mit ihren eigenen Kopfzeilen.

Ein IONOS-Postfach ist der Briefkasten, dieser Code der Automat. Er läuft als
Azure Function (Timer, jede Minute) oder als Schleife auf einem beliebigen
Rechner (``python -m header_echo --loop 60``). Es gibt keine Abhängigkeiten
außerhalb der Python-Standardbibliothek; ``azure-functions`` braucht nur der
Einstiegspunkt ``function_app.py``.
"""

__version__ = "1.0.0"
