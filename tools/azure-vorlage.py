#!/usr/bin/env python3
"""Erzeugt deploy/azure/azuredeploy.json („Deploy to Azure“) aus cloud-init.yaml.

Warum erzeugt und nicht geschrieben: Eine ARM-Vorlage kann beim Ausrollen keine
Datei nachladen, cloud-init muss also als Text in ihr stehen. Von Hand gepflegt
wäre das eine zweite Kopie neben `deploy/azure/cloud-init.yaml`, die auch
`azure-vm-setup.ps1` benutzt — und zwei Kopien laufen auseinander. Deshalb gibt
es genau eine Quelle, und diese Datei bettet sie ein.

Aufruf aus dem Gateway-Repo:
    python3 tools/azure-vorlage.py          # prüfen (Exit 1 bei Abweichung, CI)
    python3 tools/azure-vorlage.py --fix    # azuredeploy.json neu schreiben
"""
import json
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent
QUELLE = GATEWAY / "deploy" / "azure" / "cloud-init.yaml"
ZIEL = GATEWAY / "deploy" / "azure" / "azuredeploy.json"

REPO_URL = "https://github.com/azitc-ac/EXO-Signature-Gateway.git"

# ARM64 ist die Vorgabe (günstiger, im Betrieb erprobt). Das Image muss zur
# Architektur der Größe passen — Debian veröffentlicht je Architektur eine SKU.
GROESSEN_ARM64 = ["Standard_B2pls_v2", "Standard_B2ps_v2", "Standard_B4pls_v2", "Standard_D2pls_v5"]
GROESSEN_X64 = ["Standard_B2als_v2", "Standard_B2s", "Standard_B2ms", "Standard_D2als_v6"]

# Eingehende Ports — dieselben wie in azure-vm-setup.ps1.
PORTS = [
    ("SSH", 22, 100, "SSH-Zugang (Verwaltung)"),
    ("HTTP", 80, 110, "Let's Encrypt HTTP-01 + Einrichtung"),
    ("HTTPS", 443, 120, "Weboberfläche + Outlook-Add-ins"),
    ("SMTP", 25, 130, "Exchange Online: ausgehender Connector zum Gateway"),
    ("Submission", 587, 140, "Sende-Identitäten: authentifizierte Einlieferung"),
]

NET = "2023-09-01"
COMPUTE = "2023-09-01"


def p(name: str) -> str:
    return f"parameters('{name}')"


def vorlage(cloud_init: str) -> dict:
    regeln = []
    for name, port, prio, text in PORTS:
        regeln.append({
            "name": name,
            "properties": {
                "priority": prio, "protocol": "Tcp", "access": "Allow", "direction": "Inbound",
                "sourceAddressPrefix": f"[{p('sshQuelle')}]" if port == 22 else "*",
                "sourcePortRange": "*", "destinationAddressPrefix": "*",
                "destinationPortRange": str(port), "description": text,
            },
        })
    return {
        "$schema": "https://schema.management.azure.com/schemas/2019-04-01/deploymentTemplate.json#",
        "contentVersion": "1.0.0.0",
        "metadata": {
            "description": "EXO Signature Gateway: Debian-12-VM mit statischer IP, Firewall-Regeln, "
                           "Docker und Gateway (cloud-init). Erzeugt von tools/azure-vorlage.py "
                           "aus deploy/azure/cloud-init.yaml — nicht von Hand ändern.",
        },
        "parameters": {
            "location": {"type": "string", "defaultValue": "[resourceGroup().location]",
                         "metadata": {"description": "Region (Vorgabe: die der Ressourcengruppe)."}},
            "vmName": {"type": "string", "defaultValue": "exo-gateway", "maxLength": 50,
                       "metadata": {"description": "Name der VM; davon abgeleitet die Namen von IP, NSG, Netz."}},
            "vmSize": {"type": "string", "defaultValue": GROESSEN_ARM64[0],
                       "allowedValues": GROESSEN_ARM64 + GROESSEN_X64,
                       "metadata": {"description": "VM-Größe. 2 vCPU / 4 GB reichen; *p*-Größen sind ARM64."}},
            "adminUsername": {"type": "string", "defaultValue": "azureuser",
                              "metadata": {"description": "SSH-Benutzer auf der VM."}},
            "adminSshPublicKey": {"type": "string",
                                  "metadata": {"description": "Öffentlicher SSH-Schlüssel (Inhalt von ~/.ssh/id_ed25519.pub o. ä.)."}},
            "sshQuelle": {"type": "string", "defaultValue": "*",
                          "metadata": {"description": "Von wo SSH erlaubt ist: '*' oder eine eigene IP/CIDR, z. B. 203.0.113.7/32."}},
            "repoUrl": {"type": "string", "defaultValue": REPO_URL,
                        "metadata": {"description": "Git-Repository des Gateways."}},
        },
        "variables": {
            "armGroessen": GROESSEN_ARM64,
            "imageSku": f"[if(contains(variables('armGroessen'), {p('vmSize')}), '12-arm64', '12-gen2')]",
            "ipName": f"[concat({p('vmName')}, '-ip')]",
            "nsgName": f"[concat({p('vmName')}, '-nsg')]",
            "vnetName": f"[concat({p('vmName')}, '-vnet')]",
            "nicName": f"[concat({p('vmName')}, '-nic')]",
            "cloudInit": cloud_init,
            "customData": (f"[base64(replace(replace(variables('cloudInit'), '__ADMIN_USER__', "
                           f"{p('adminUsername')}), '__REPO_URL__', {p('repoUrl')}))]"),
        },
        "resources": [
            {
                "type": "Microsoft.Network/publicIPAddresses", "apiVersion": NET,
                "name": "[variables('ipName')]", "location": f"[{p('location')}]",
                "sku": {"name": "Standard"},
                "properties": {"publicIPAllocationMethod": "Static"},
            },
            {
                "type": "Microsoft.Network/networkSecurityGroups", "apiVersion": NET,
                "name": "[variables('nsgName')]", "location": f"[{p('location')}]",
                "properties": {"securityRules": regeln},
            },
            {
                "type": "Microsoft.Network/virtualNetworks", "apiVersion": NET,
                "name": "[variables('vnetName')]", "location": f"[{p('location')}]",
                "dependsOn": ["[resourceId('Microsoft.Network/networkSecurityGroups', variables('nsgName'))]"],
                "properties": {
                    "addressSpace": {"addressPrefixes": ["10.20.0.0/24"]},
                    "subnets": [{"name": "default", "properties": {
                        "addressPrefix": "10.20.0.0/26",
                        "networkSecurityGroup": {"id": "[resourceId('Microsoft.Network/networkSecurityGroups', variables('nsgName'))]"},
                    }}],
                },
            },
            {
                "type": "Microsoft.Network/networkInterfaces", "apiVersion": NET,
                "name": "[variables('nicName')]", "location": f"[{p('location')}]",
                "dependsOn": [
                    "[resourceId('Microsoft.Network/publicIPAddresses', variables('ipName'))]",
                    "[resourceId('Microsoft.Network/virtualNetworks', variables('vnetName'))]",
                ],
                "properties": {"ipConfigurations": [{"name": "ipconfig1", "properties": {
                    "privateIPAllocationMethod": "Dynamic",
                    "publicIPAddress": {"id": "[resourceId('Microsoft.Network/publicIPAddresses', variables('ipName'))]"},
                    "subnet": {"id": "[resourceId('Microsoft.Network/virtualNetworks/subnets', variables('vnetName'), 'default')]"},
                }}]},
            },
            {
                "type": "Microsoft.Compute/virtualMachines", "apiVersion": COMPUTE,
                "name": f"[{p('vmName')}]", "location": f"[{p('location')}]",
                "dependsOn": ["[resourceId('Microsoft.Network/networkInterfaces', variables('nicName'))]"],
                "properties": {
                    "hardwareProfile": {"vmSize": f"[{p('vmSize')}]"},
                    "storageProfile": {
                        "imageReference": {"publisher": "Debian", "offer": "debian-12",
                                           "sku": "[variables('imageSku')]", "version": "latest"},
                        "osDisk": {"createOption": "FromImage", "diskSizeGB": 32,
                                   "managedDisk": {"storageAccountType": "StandardSSD_LRS"}},
                    },
                    "osProfile": {
                        "computerName": f"[{p('vmName')}]",
                        "adminUsername": f"[{p('adminUsername')}]",
                        "customData": "[variables('customData')]",
                        "linuxConfiguration": {
                            "disablePasswordAuthentication": True,
                            "ssh": {"publicKeys": [{
                                "path": f"[concat('/home/', {p('adminUsername')}, '/.ssh/authorized_keys')]",
                                "keyData": f"[{p('adminSshPublicKey')}]",
                            }]},
                        },
                    },
                    "networkProfile": {"networkInterfaces": [
                        {"id": "[resourceId('Microsoft.Network/networkInterfaces', variables('nicName'))]"}]},
                },
            },
        ],
        "outputs": {
            "publicIp": {"type": "string",
                         "value": "[reference(resourceId('Microsoft.Network/publicIPAddresses', variables('ipName'))).ipAddress]"},
            "einrichtung": {"type": "string",
                            "value": "[concat('http://', reference(resourceId('Microsoft.Network/publicIPAddresses', variables('ipName'))).ipAddress)]"},
            "ssh": {"type": "string",
                    "value": f"[concat('ssh ', {p('adminUsername')}, '@', reference(resourceId('Microsoft.Network/publicIPAddresses', variables('ipName'))).ipAddress)]"},
        },
    }


def erzeugen() -> str:
    text = QUELLE.read_text(encoding="utf-8")
    # Ein ARM-String, der mit "[" beginnt, wäre ein Ausdruck.
    assert not text.startswith("["), "cloud-init.yaml darf nicht mit '[' beginnen"
    return json.dumps(vorlage(text), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str]) -> int:
    soll = erzeugen()
    ist = ZIEL.read_text(encoding="utf-8") if ZIEL.exists() else ""
    if ist == soll:
        print(f"ok  {ZIEL.relative_to(GATEWAY)} ist auf dem Stand von {QUELLE.name}")
        return 0
    if "--fix" in argv:
        ZIEL.write_text(soll, encoding="utf-8")
        print(f"++ erzeugt: {ZIEL.relative_to(GATEWAY)}")
        return 0
    print(f"!! {ZIEL.relative_to(GATEWAY)} weicht von {QUELLE.name} ab  → python3 tools/azure-vorlage.py --fix")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
