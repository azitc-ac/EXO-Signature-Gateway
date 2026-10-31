"""Azure-Ausrollweg: eine Quelle für cloud-init, zwei Verbraucher.

`deploy/azure/cloud-init.yaml` wird von `azure-vm-setup.ps1` gelesen und in
`deploy/azure/azuredeploy.json` („Deploy to Azure“) eingebettet. Eine zweite,
handgepflegte Kopie liefe auseinander — dieselbe Klasse wie bei den
Rechtstexten. Diese Tests halten beide Verbraucher an der Quelle.
"""
import importlib.util
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
QUELLE = ROOT / "deploy" / "azure" / "cloud-init.yaml"
VORLAGE = ROOT / "deploy" / "azure" / "azuredeploy.json"
PS = ROOT / "azure-vm-setup.ps1"


def _werkzeug():
    spec = importlib.util.spec_from_file_location("azure_vorlage", ROOT / "tools" / "azure-vorlage.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_vorlage_ist_aus_der_quelle_erzeugt():
    """Wer cloud-init.yaml ändert und --fix vergisst, fällt hier auf."""
    assert VORLAGE.read_text(encoding="utf-8") == _werkzeug().erzeugen(), \
        "azuredeploy.json veraltet → python3 tools/azure-vorlage.py --fix"


def test_ps_skript_hat_keine_eigene_kopie():
    text = PS.read_text(encoding="utf-8-sig")
    assert "#cloud-config" not in text, "cloud-init gehört nach deploy/azure/cloud-init.yaml"
    assert "deploy/azure/cloud-init.yaml" in text


def test_platzhalter_werden_in_beiden_wegen_ersetzt():
    quelle = QUELLE.read_text(encoding="utf-8")
    vorlage = json.loads(VORLAGE.read_text(encoding="utf-8"))
    ps = PS.read_text(encoding="utf-8-sig")
    for platzhalter in ("__ADMIN_USER__", "__REPO_URL__"):
        assert platzhalter in quelle
        assert f"'{platzhalter}'" in vorlage["variables"]["customData"]
        assert f"Replace('{platzhalter}'" in ps
    # Kein weiterer, von niemandem ersetzter Platzhalter
    assert set(re.findall(r"__[A-Z_]+__", quelle)) == {"__ADMIN_USER__", "__REPO_URL__"}


def test_beide_wege_oeffnen_dieselben_ports():
    vorlage = json.loads(VORLAGE.read_text(encoding="utf-8"))
    nsg = next(r for r in vorlage["resources"] if r["type"].endswith("networkSecurityGroups"))
    in_vorlage = {int(r["properties"]["destinationPortRange"]) for r in nsg["properties"]["securityRules"]}
    in_ps = {int(p) for p in re.findall(r"port=(\d+);", PS.read_text(encoding="utf-8-sig"))}
    assert in_vorlage == in_ps == {22, 25, 80, 443, 587}


def test_image_passt_zur_architektur():
    """Die *p*-Größen sind ARM64 und brauchen das arm64-Image, die übrigen x64."""
    w = _werkzeug()
    assert all(re.match(r"Standard_[BD]\d+p", g) for g in w.GROESSEN_ARM64)
    assert not any(re.match(r"Standard_[BD]\d+p", g) for g in w.GROESSEN_X64)
    vorlage = json.loads(VORLAGE.read_text(encoding="utf-8"))
    assert vorlage["variables"]["armGroessen"] == w.GROESSEN_ARM64
    assert "'12-arm64', '12-gen2'" in vorlage["variables"]["imageSku"]
