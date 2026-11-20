"""Die Erneuerungs-Mail an den Postfachinhaber (CASTLE) geht nur hinaus, wenn das
Gateway NICHT selbst bestellt hat. Bis v1.12.112 behauptete sie das Gegenteil
(„hat automatisch beantragt … kein weiterer Schritt erforderlich“)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))


def test_castle_mail_behauptet_keine_bestellung():
    from ca_backends.castle_acme import CastleAcmeBackend
    html = CastleAcmeBackend().get_instructions_html("a@x.de", 14, "01.12.2026", "", {})
    assert "noch <strong>nicht</strong> beantragt" in html
    assert "Kein weiterer Schritt" not in html


def test_naechste_erneuerung_nutzt_die_erste_schwelle():
    quelle = (Path(__file__).parent.parent / "app/webui/routen/smime.py").read_text(encoding="utf-8")
    assert 'max(settings_store.get("CERT_RENEWAL_THRESHOLDS")' in quelle
