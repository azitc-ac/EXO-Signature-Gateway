"""Die Sperre aus tests/speicher_sperre.py greift beim Start von pytest."""
import pytest

import conftest
import speicher_sperre as s

VOLL = "MemAvailable: 880000 kB\nSwapTotal: 2097148 kB\nSwapFree: 17000 kB\n"
KNAPP_RAM = "MemAvailable: 500000 kB\nSwapTotal: 2097148 kB\nSwapFree: 2000000 kB\n"
GUT = "MemAvailable: 2500000 kB\nSwapTotal: 2097148 kB\nSwapFree: 2000000 kB\n"
OHNE_SWAP = "MemAvailable: 9000000 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"


def test_voller_swap_und_knapper_speicher_sperren():
    assert "Swap fast voll" in s.einwand(VOLL)
    assert "Arbeitsspeicher" in s.einwand(KNAPP_RAM)


@pytest.mark.parametrize("text", [GUT, OHNE_SWAP, ""])
def test_genug_luft_oder_keine_angaben_sperrt_nicht(text):
    assert s.einwand(text) is None


def test_start_bricht_ab(monkeypatch):
    monkeypatch.delenv("PYTEST_SPEICHER_EGAL", raising=False)
    monkeypatch.setattr(conftest, "_meminfo_lesen", lambda: VOLL)
    with pytest.raises(pytest.UsageError, match="PYTEST_SPEICHER_EGAL"):
        conftest.pytest_configure(None)


def test_bewusst_uebergehen(monkeypatch):
    monkeypatch.setenv("PYTEST_SPEICHER_EGAL", "1")
    monkeypatch.setattr(conftest, "_meminfo_lesen", lambda: VOLL)
    conftest.pytest_configure(None)
