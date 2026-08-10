"""Routen rund um S/MIME — Zertifikate, Erneuerung, Key Vault.

Drittes Modul der Aufteilung von `app.py`. Muster siehe `addin.py`.

WARUM DIESER SCHNITT ANDERS WAR
-------------------------------
Add-in und Portal waren zusammenhängende Blöcke. S/MIME nicht: 28 Endpunkte,
verteilt über gut 2.800 Zeilen, mit 17 fremden Routen dazwischen (Sicherung,
Protokollansicht, Key-Vault-Einrichtung). Ein Zeilenbereich ließ sich nicht
ausschneiden — die Endpunkte wurden über den Syntaxbaum eingesammelt, Anfang
bei der ersten Dekoratorzeile, Ende bei `end_lineno`.

Der Filter ist bewusst eng gefasst:

    p == "/smime" or p.startswith("/smime/") or p.startswith("/api/smime")

Ein schlichtes `"/smime" in p` hätte `/api/setup/smime-rules`,
`/api/setup/verify/smime` und `/settings/smime` mitgenommen. Die gehören zum
Einrichtungsassistenten und zu den Einstellungen, nicht hierher.

ZU DEN HILFSFUNKTIONEN
----------------------
Erwartet worden waren geteilte Helfer für Key Vault, Zertifikatsprüfung und
Erneuerung. Es gibt keine: Diese Endpunkte holen sich `keyvault`, `acme_state`,
`ca_backends` und `hub_catalog` durchweg als lokalen Import in der Funktion.
Geteilt wird allein `_cert_expiry` — und das betrifft das TLS-Zertifikat des
SMTP-Lauschers, nicht S/MIME; es liegt deshalb in `hilfen.py`.

`smime_store` kommt dagegen als Modulimport (`_smime_store`), wie schon in
`app.py`: Drei dieser Endpunkte hatten den Pfad zum Zertifikatsverzeichnis
früher je einzeln verdrahtet.
"""
from __future__ import annotations

import asyncio
import subprocess
import urllib.parse
from datetime import datetime, timezone

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse

import settings_store
import smime_store as _smime_store

from webui.deps import templates, log, _gateway_name, _check_auth, _require_admin
from webui.hilfen import _cert_expiry

router = APIRouter()


@router.post("/api/smime/key-password")
async def api_smime_key_password(request: Request, user: str = Depends(_check_auth)):
    import smime_store as _smime
    data = await request.json()
    old_pw = data.get("old_password") or ""
    new_pw = data.get("new_password") or ""
    stored = settings_store.get("SMIME_KEY_PASSWORD") or ""
    if stored and old_pw != stored:
        raise HTTPException(400, "Aktuelles Passwort falsch")
    settings_store.update({"SMIME_KEY_PASSWORD": new_pw})
    failed = _smime.reencrypt_all_keys(old_password=stored)
    log.info("SMIME key password changed by %s; re-encrypted keys, failed: %s", user, failed)
    if failed:
        return JSONResponse({"ok": True, "warnings": failed})
    return JSONResponse({"ok": True})


@router.post("/api/smime/upload")
async def api_smime_upload(
    request: Request,
    user: str = Depends(_check_auth),
    email: str = Form(...),
    p12_file: UploadFile = File(...),
    password: str = Form(""),
):
    import smime_store
    p12_bytes = await p12_file.read()
    try:
        info = smime_store.store_p12_slot(email.lower().strip(), p12_bytes, password)
        log.info("S/MIME cert uploaded for %s by %s (slot %s)", email, user, info.get("slot_id"))
        return JSONResponse({"ok": True, "info": info})
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.error("S/MIME upload error: %s", exc)
        raise HTTPException(500, str(exc))


@router.post("/api/smime/delete/{cert_email}")
async def api_smime_delete(cert_email: str, user: str = Depends(_check_auth)):
    import smime_store
    smime_store.delete_cert(cert_email)
    log.info("S/MIME certs deleted for %s by %s", cert_email, user)
    return JSONResponse({"ok": True})


@router.post("/api/smime/delete-slot/{cert_email}/{slot_id}")
async def api_smime_delete_slot(cert_email: str, slot_id: str, user: str = Depends(_check_auth)):
    import smime_store
    try:
        smime_store.delete_cert_slot(cert_email, slot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("S/MIME cert slot %s deleted for %s by %s", slot_id, cert_email, user)
    return JSONResponse({"ok": True})


@router.post("/api/smime/set-default/{cert_email}/{slot_id}")
async def api_smime_set_default(cert_email: str, slot_id: str, user: str = Depends(_check_auth)):
    import smime_store
    try:
        smime_store.set_default_slot(cert_email, slot_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    log.info("S/MIME default cert set to slot %s for %s by %s", slot_id, cert_email, user)
    return JSONResponse({"ok": True})


@router.get("/smime", response_class=HTMLResponse)
async def smime_page_v2(request: Request, user: str = Depends(_require_admin)):
    import smime_store
    import ca_backends as _ca
    import acme_state as _acme_state
    import keyvault as _kv
    import hub_catalog as _hub_cat
    try:
        # Anbieter/Preise kommen dynamisch vom Hub — vor dem Rendern auffrischen
        await _hub_cat.refresh()
    except Exception:
        pass
    config_map: dict = settings_store.get("MAILBOX_CONFIG") or {}
    smime_from_config = {
        (key.lower() if "@" in key else (cfg.get("primary") or "").lower())
        for key, cfg in config_map.items() if cfg.get("smime")
    } - {""}
    smime_from_certs = {c["email"] for c in smime_store.list_certs()}
    all_emails = sorted(smime_from_config | smime_from_certs)
    smime_users = [{"email": email, "certs": smime_store.list_user_certs(email)} for email in all_emails]
    acme_orders = {em: _acme_state.get_order(em) for em in all_emails if _acme_state.get_order(em)}
    # Enrich certs with next_renewal date (expiry − first renewal threshold)
    from datetime import datetime as _dt, timedelta as _td
    _first_threshold = min(settings_store.get("CERT_RENEWAL_THRESHOLDS") or [30, 14, 7, 1])
    for u in smime_users:
        for c in u["certs"]:
            if c.get("expiry") and not c.get("error"):
                try:
                    c["next_renewal"] = (_dt.strptime(c["expiry"], "%d.%m.%Y") - _td(days=_first_threshold)).strftime("%d.%m.%Y")
                except Exception:
                    c["next_renewal"] = None
            else:
                c["next_renewal"] = None
    # Key Vault status per email (only if configured) — use cached status, not live queries
    kv_configured = _kv.is_configured()
    kv_status: dict = settings_store.get("KV_KEY_STATUS") or {}
    kv_keys: dict[str, bool | None] = {
        em: kv_status.get(em, {}).get("exists", None) for em in all_emails
    }
    kv_mode = settings_store.get("KV_KEY_MODE") or "fallback"
    has_any_local_key = any(
        c.get("has_local_key") or c.get("has_kv_backup")
        for u in smime_users for c in u["certs"]
    )
    has_any_unmigrated_key = any(
        c.get("has_local_key")
        for u in smime_users for c in u["certs"]
    )
    return templates.TemplateResponse(
        request=request, name="smime.html",
        context={
            "smime_users": smime_users,
            "ca_user_config": settings_store.get("CA_USER_CONFIG") or {},
            "backends": _ca.list_backends(),
            "recipient_certs": smime_store.list_recipient_certs(),
            "active": "smime",
            "cert_expiry": _cert_expiry(),
            "acme_orders": acme_orders,
            "kv_configured": kv_configured,
            "kv_keys": kv_keys,
            "kv_url": _kv.vault_url(),
            "kv_mode": kv_mode,
            "has_any_local_key": has_any_local_key,
            "has_any_unmigrated_key": has_any_unmigrated_key,
            "gateway_name": _gateway_name(),
        },
    )


@router.post("/api/smime/kv-status/refresh")
async def api_smime_kv_status_refresh(_=Depends(_check_auth)):
    """Refresh Azure Key Vault key-existence status for all S/MIME users (parallel queries)."""
    import smime_store
    import keyvault as _kv
    if not _kv.is_configured():
        return JSONResponse({"ok": False, "detail": "Key Vault nicht konfiguriert"}, status_code=400)
    config_map: dict = settings_store.get("MAILBOX_CONFIG") or {}
    smime_from_config = {
        (key.lower() if "@" in key else (cfg.get("primary") or "").lower())
        for key, cfg in config_map.items() if cfg.get("smime")
    } - {""}
    smime_from_certs = {c["email"] for c in smime_store.list_certs()}
    all_emails = sorted(smime_from_config | smime_from_certs)
    if not all_emails:
        return JSONResponse({"ok": True, "results": {}})
    results_list = await asyncio.gather(*[_kv.key_exists(em) for em in all_emails])
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    new_status = {em: {"exists": bool(ex), "checked": now_iso}
                  for em, ex in zip(all_emails, results_list)}
    settings_store.update({"KV_KEY_STATUS": new_status})
    log.info("KV key status refreshed for %d emails", len(all_emails))
    return JSONResponse({"ok": True, "results": new_status})


@router.get("/api/smime/cert/download/{email}/{slot_id}")
async def api_smime_cert_download(email: str, slot_id: str, _=Depends(_check_auth)):
    """Download a signing certificate as DER-encoded .cer file."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from fastapi.responses import Response as _Response
    import smime_store
    email = urllib.parse.unquote(email).lower().strip()
    cert_path = smime_store.SMIME_DIR / email / "certs" / slot_id / "cert.pem"
    if not cert_path.exists():
        raise HTTPException(404, "Zertifikat nicht gefunden")
    try:
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        der_bytes = cert.public_bytes(serialization.Encoding.DER)
        safe_email = email.replace("@", "_").replace(".", "_")
        filename = f"{safe_email}_{slot_id}.cer"
        return _Response(
            content=der_bytes,
            media_type="application/pkix-cert",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as exc:
        log.error("Cert download error for %s/%s: %s", email, slot_id, exc)
        raise HTTPException(500, str(exc))


@router.post("/api/smime/recipient/upload")
async def api_smime_recipient_upload(
    request: Request,
    user: str = Depends(_check_auth),
    email: str = Form(...),
    cert_file: UploadFile = File(...),
):
    import smime_store
    cert_bytes = await cert_file.read()
    # Accept PEM directly; also try DER → PEM conversion via openssl
    if not cert_bytes.strip().startswith(b"-----"):
        result = subprocess.run(
            ["openssl", "x509", "-inform", "DER", "-outform", "PEM"],
            input=cert_bytes, capture_output=True, timeout=10,
        )
        if result.returncode != 0:
            raise HTTPException(400, "Ungültige Zertifikatsdatei (weder PEM noch DER)")
        cert_bytes = result.stdout
    try:
        info = smime_store.store_recipient_cert(email.lower().strip(), cert_bytes)
        log.info("Recipient S/MIME cert uploaded for %s by %s", email, user)
        return JSONResponse({"ok": True, "info": info})
    except Exception as exc:
        log.error("Recipient cert upload error: %s", exc)
        raise HTTPException(400, str(exc))


@router.post("/api/smime/recipient/delete/{cert_email}")
async def api_smime_recipient_delete(cert_email: str, user: str = Depends(_check_auth)):
    import smime_store
    smime_store.delete_recipient_cert(cert_email)
    log.info("Recipient S/MIME cert deleted for %s by %s", cert_email, user)
    return JSONResponse({"ok": True})


@router.get("/api/smime/cert/details")
async def api_smime_cert_details(
    email: str, kind: str = "recipient", slot: str = "",
    user: str = Depends(_check_auth),
):
    """Return human-readable cert details for the detail modal (no download)."""
    import smime_store
    from cryptography import x509 as _x509
    from cryptography.hazmat.primitives.asymmetric import rsa, ec
    from cryptography.x509.oid import NameOID, ExtensionOID
    import hashlib

    if kind == "signing":
        if slot:
            cert_path = smime_store.get_signing_cert_path_for_slot(email, slot)
        else:
            cert_path = smime_store.get_signing_cert_path(email)
    else:
        cert_path = smime_store.get_recipient_cert_path(email) or (smime_store.RECIPIENT_DIR / "nope")

    if not cert_path or not cert_path.exists():
        raise HTTPException(404, "Zertifikat nicht gefunden")

    cert = _x509.load_pem_x509_certificate(cert_path.read_bytes())

    def _dn(name) -> str:
        parts = []
        for oid in (NameOID.COMMON_NAME, NameOID.EMAIL_ADDRESS,
                    NameOID.ORGANIZATION_NAME, NameOID.COUNTRY_NAME):
            attrs = name.get_attributes_for_oid(oid)
            if attrs:
                parts.append(attrs[0].value)
        return ", ".join(parts) if parts else name.rfc4514_string()

    pub = cert.public_key()
    if isinstance(pub, rsa.RSAPublicKey):
        key_info = f"RSA {pub.key_size} bit"
    elif isinstance(pub, ec.EllipticCurvePublicKey):
        key_info = f"EC {pub.curve.name}"
    else:
        key_info = type(pub).__name__

    der = cert.public_bytes(__import__("cryptography").hazmat.primitives.serialization.Encoding.DER)
    sha1 = ":".join(f"{b:02X}" for b in hashlib.sha1(der).digest())

    san_emails: list[str] = []
    try:
        san = cert.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        san_emails = san.value.get_values_for_type(_x509.RFC822Name)
    except Exception:
        pass

    from datetime import timezone
    try:
        not_after = cert.not_valid_after_utc
        not_before = cert.not_valid_before_utc
    except AttributeError:
        not_after = cert.not_valid_after.replace(tzinfo=timezone.utc)
        not_before = cert.not_valid_before.replace(tzinfo=timezone.utc)

    return JSONResponse({
        "subject":  _dn(cert.subject),
        "issuer":   smime_store._friendly_issuer(cert, full=True),
        "san":      san_emails,
        "serial":   format(cert.serial_number, "X"),
        "not_before": not_before.strftime("%d.%m.%Y"),
        "not_after":  not_after.strftime("%d.%m.%Y"),
        "key":      key_info,
        "sha1":     sha1,
    })


@router.get("/api/smime/ca-config")
async def api_ca_config_get(user: str = Depends(_check_auth)):
    import ca_backends as _ca
    import hub_catalog as _hub_cat
    try:
        await _hub_cat.refresh()
    except Exception:
        pass
    return JSONResponse({
        "config": settings_store.get("CA_USER_CONFIG") or {},
        "backends": _ca.list_backends(),
    })


@router.post("/api/smime/ca-config/{email}")
async def api_ca_config_save(email: str, request: Request, user: str = Depends(_check_auth)):
    data = await request.json()
    cfg: dict = settings_store.get("CA_USER_CONFIG") or {}
    cfg[email.lower().strip()] = {
        "backend": data.get("backend", "assisted_manual"),
        "portal_url": (data.get("portal_url") or "").strip(),
        "notify_user": bool(data.get("notify_user", False)),
        "auto_renew": bool(data.get("auto_renew", False)),
        "staging": bool(data.get("staging", False)),
    }
    settings_store.update({"CA_USER_CONFIG": cfg})
    return JSONResponse({"ok": True})


@router.post("/api/smime/renewal/token/{email}")
async def api_renewal_token_generate(email: str, user: str = Depends(_check_auth)):
    import selfservice, scheduler
    token = selfservice.generate_token(email)
    gw_url = scheduler._get_gateway_url()
    return JSONResponse({
        "ok": True,
        "token": token,
        "url": f"{gw_url}/smime/renew/{token}",
        "expires_days": selfservice.TOKEN_TTL_DAYS,
    })


@router.get("/api/smime/renewal/token-info/{email}")
async def api_renewal_token_info(email: str, user: str = Depends(_check_auth)):
    import selfservice, scheduler
    info = selfservice.get_token_info(email)
    if not info:
        return JSONResponse({"exists": False})
    gw_url = scheduler._get_gateway_url()
    return JSONResponse({
        "exists": True,
        "expires": info["expires"],
        "url": f"{gw_url}/smime/renew/{info['token']}",
    })


@router.post("/api/smime/renewal/notify/{email}")
async def api_renewal_notify(email: str, user: str = Depends(_check_auth)):
    import smime_store, selfservice, notification, scheduler
    certs = smime_store.list_user_certs(email)
    if not certs:
        raise HTTPException(400, "Kein Zertifikat für diesen Benutzer vorhanden")
    c = next((x for x in certs if x.get("is_default")), certs[0])
    ca_cfg: dict = (settings_store.get("CA_USER_CONFIG") or {}).get(email, {})
    token = selfservice.generate_token(email)
    gw_url = scheduler._get_gateway_url()
    upload_url = f"{gw_url}/smime/renew/{token}"
    ok = notification.send_renewal_notification_to_user(
        user_email=email,
        cert_info=c,
        upload_url=upload_url,
        backend_name=ca_cfg.get("backend", "assisted_manual"),
        user_config=ca_cfg,
    )
    if not ok:
        raise HTTPException(500, "Benachrichtigung konnte nicht gesendet werden")
    return JSONResponse({"ok": True, "upload_url": upload_url})


@router.get("/smime/renew/{token}", response_class=HTMLResponse)
async def smime_selfservice_page(token: str, request: Request):
    import selfservice, smime_store
    email = selfservice.validate_token(token)
    if not email:
        return HTMLResponse(
            "<html><body style='font-family:sans-serif;padding:40px'>"
            "<h2 style='color:#e74c3c'>Link abgelaufen oder ungültig</h2>"
            "<p>Bitte fordern Sie beim Administrator einen neuen Link an.</p>"
            "</body></html>",
            status_code=403,
        )
    certs = smime_store.list_user_certs(email)
    current_cert = next((c for c in certs if c.get("is_default")), certs[0] if certs else None)
    return templates.TemplateResponse(
        request=request, name="smime_selfservice.html",
        context={"email": email, "token": token, "current_cert": current_cert},
    )


@router.post("/api/smime/renew/{token}")
async def api_smime_selfservice_upload(
    token: str,
    request: Request,
    p12_file: UploadFile = File(...),
    password: str = Form(""),
):
    import selfservice, smime_store, notification
    email = selfservice.validate_token(token)
    if not email:
        raise HTTPException(403, "Link abgelaufen oder ungültig")
    try:
        p12_bytes = await p12_file.read()
        info = smime_store.store_p12_slot(email, p12_bytes, password)
        log.info("Self-service cert upload for %s (slot %s)", email, info.get("slot_id"))
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            notification.send_cert_renewal_success(email, info)
        return JSONResponse({"ok": True, "info": info})
    except ValueError as exc:
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            notification.send_cert_renewal_failure(email, str(exc))
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.error("Self-service upload error for %s: %s", email, exc)
        if settings_store.get("NOTIFY_CERT_RENEWAL") is not False:
            notification.send_cert_renewal_failure(email, str(exc))
        raise HTTPException(500, str(exc))


@router.get("/api/smime/renewal/status/{email}")
async def api_acme_status(email: str, user: str = Depends(_check_auth)):
    import acme_state
    order = acme_state.get_order(email.lower().strip())
    if not order:
        return JSONResponse({"active": False})
    return JSONResponse({
        "active": True,
        "status": order.get("status"),
        "error": order.get("error"),
        "created": order.get("created"),
    })


@router.post("/api/smime/renewal/clear/{email}")
async def api_acme_clear_order(email: str, user: str = Depends(_check_auth)):
    import acme_state
    acme_state.clear_order(email)
    log.info("ACME order cleared for %s by %s", email, user)
    return JSONResponse({"ok": True})


@router.post("/api/smime/renewal/initiate/{email}")
async def api_acme_initiate(request: Request, email: str, user: str = Depends(_check_auth)):
    import ca_backends as _ca
    import acme_state as _acme_state
    email = email.lower().strip()
    try:
        body = await request.json()
    except Exception:
        body = {}
    ca_cfg: dict = (settings_store.get("CA_USER_CONFIG") or {}).get(email, {})
    backend_name = ca_cfg.get("backend", "assisted_manual")
    backend = _ca.get_backend(backend_name)
    if not backend.can_auto_renew():
        raise HTTPException(400, f"Backend '{backend_name}' unterstützt kein Auto-Enroll")

    # Guard: a mailbox must be activated for S/MIME before it may obtain a cert
    # (clean 400 here; also enforced in initiate_acme_order so nothing bypasses it).
    import mailbox_match
    if not mailbox_match.match_sender(settings_store.get("MAILBOX_CONFIG") or {}, email).get("smime"):
        raise HTTPException(400, f"Postfach {email} ist nicht für S/MIME aktiviert — "
                                 f"erst das Postfach für S/MIME aktivieren, dann Zertifikat beziehen.")

    # If there's already a waiting_challenge order, restart the mailbox poller
    # instead of creating a redundant CASTLE order.
    existing = _acme_state.get_order(email)
    if existing and existing.get("status") == "waiting_challenge":
        import asyncio
        asyncio.create_task(_acme_state._poll_mailbox_for_challenge(email))
        log.info("ACME mailbox poll restarted for %s by %s (order already placed)", email, user)
        return JSONResponse({"ok": True, "resumed": True})

    try:
        await backend.initiate_renewal(email, ca_cfg, extra=body)
        log.info("ACME renewal initiated for %s by %s", email, user)
        return JSONResponse({"ok": True})
    except _acme_state.EnrollmentNotAllowed as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        log.error("ACME initiate failed for %s: %s", email, exc)
        raise HTTPException(500, str(exc))


@router.post("/api/smime/keyvault/migrate/{email}")
async def api_keyvault_migrate(email: str, _: str = Depends(_require_admin)):
    """Migrate active S/MIME private key slot to Azure Key Vault."""
    import smime_store
    email = email.lower().strip()
    result = await smime_store.migrate_key_to_keyvault(email)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    log.info("S/MIME key migrated to Key Vault for %s slot %s (key_id=%s)", email, result.get("slot_id"), result.get("key_id"))
    return JSONResponse(result)


@router.post("/api/smime/keyvault/migrate/{email}/{slot_id}")
async def api_keyvault_migrate_slot(email: str, slot_id: str, _: str = Depends(_require_admin)):
    """Migrate a specific S/MIME cert slot's private key to Azure Key Vault."""
    import smime_store
    email = email.lower().strip()
    result = await smime_store.migrate_key_to_keyvault(email, slot_id=slot_id)
    if not result["ok"]:
        raise HTTPException(400, result["error"])
    log.info("S/MIME key migrated to Key Vault for %s slot %s (key_id=%s)", email, slot_id, result.get("key_id"))
    return JSONResponse(result)


@router.get("/api/smime/backup-key/{email}/{slot_id}")
async def api_smime_backup_key_download(
    email: str, slot_id: str, _: str = Depends(_require_admin)
):
    """Download the local backup key (key.pem.bak or key.pem) for a cert slot.
    If the key is unencrypted and SMIME_KEY_PASSWORD is set, encrypt on the fly before download."""
    from fastapi.responses import Response as _Resp
    import config as _cfg

    if settings_store.get("KV_KEY_MODE") == "strict":
        raise HTTPException(403, "Backup-Download im Strict-Modus deaktiviert")

    email = email.lower().strip()
    # Pfad aus dem Modul, nicht erneut verdrahtet: smime_store.SMIME_DIR ist
    # die eine Quelle. Drei Endpunkte hatten hier ein eigenes Literal.
    smime_dir = _smime_store.SMIME_DIR
    slot_dir = smime_dir / email / "certs" / slot_id

    bak = slot_dir / "key.pem.bak"
    key = slot_dir / "key.pem"
    if bak.exists():
        key_bytes = bak.read_bytes()
    elif key.exists():
        key_bytes = key.read_bytes()
    else:
        raise HTTPException(404, "Kein lokaler Schlüssel für diesen Slot vorhanden")

    # Encrypt on-the-fly if the file is plaintext PEM and a password is configured
    pw = settings_store.get("SMIME_KEY_PASSWORD") or _cfg.SMIME_KEY_PASSWORD or ""
    if pw and b"ENCRYPTED" not in key_bytes:
        try:
            from cryptography.hazmat.primitives.serialization import (
                load_pem_private_key, Encoding, PrivateFormat, BestAvailableEncryption
            )
            loaded = load_pem_private_key(key_bytes, password=None)
            key_bytes = loaded.private_bytes(
                Encoding.PEM, PrivateFormat.TraditionalOpenSSL,
                BestAvailableEncryption(pw.encode())
            )
        except Exception as exc:
            log.warning("Could not encrypt backup key for %s/%s on download: %s", email, slot_id, exc)

    safe_email = email.replace("@", "_at_").replace(".", "_")
    filename = f"smime-backup-{safe_email}-{slot_id[:8]}.pem"
    log.info("Backup key downloaded for %s slot %s by admin", email, slot_id)
    return _Resp(
        content=key_bytes,
        media_type="application/x-pem-file",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/api/smime/backup-all-keys")
async def api_smime_backup_all_keys(_: str = Depends(_require_admin)):
    """Download a ZIP of all local key files (key.pem + key.pem.bak), encrypted where needed."""
    import io
    import zipfile
    import config as _cfg
    from fastapi.responses import Response as _Resp

    if settings_store.get("KV_KEY_MODE") == "strict":
        raise HTTPException(403, "Backup-Download im Strict-Modus deaktiviert")

    # Pfad aus dem Modul, nicht erneut verdrahtet: smime_store.SMIME_DIR ist
    # die eine Quelle. Drei Endpunkte hatten hier ein eigenes Literal.
    smime_dir = _smime_store.SMIME_DIR
    pw = settings_store.get("SMIME_KEY_PASSWORD") or _cfg.SMIME_KEY_PASSWORD or ""

    buf = io.BytesIO()
    count = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for user_dir in sorted(smime_dir.iterdir()):
            if not user_dir.is_dir() or user_dir.name == "recipients":
                continue
            certs_dir = user_dir / "certs"
            if not certs_dir.exists():
                continue
            for slot_dir in sorted(certs_dir.iterdir()):
                if not slot_dir.is_dir():
                    continue
                for fname in ("key.pem", "key.pem.bak"):
                    key_path = slot_dir / fname
                    if not key_path.exists():
                        continue
                    key_bytes = key_path.read_bytes()
                    if pw and b"ENCRYPTED" not in key_bytes:
                        try:
                            from cryptography.hazmat.primitives.serialization import (
                                load_pem_private_key, Encoding, PrivateFormat, BestAvailableEncryption,
                            )
                            loaded = load_pem_private_key(key_bytes, password=None)
                            key_bytes = loaded.private_bytes(
                                Encoding.PEM, PrivateFormat.TraditionalOpenSSL,
                                BestAvailableEncryption(pw.encode()),
                            )
                        except Exception as exc:
                            log.warning("backup-all: could not encrypt %s: %s", key_path, exc)
                    safe_email = user_dir.name.replace("@", "_at_")
                    zf.writestr(f"{safe_email}/{slot_dir.name}/{fname}", key_bytes)
                    count += 1

    if count == 0:
        raise HTTPException(404, "Keine lokalen Schlüsseldateien vorhanden")

    buf.seek(0)
    log.info("Bulk key backup downloaded by admin (%d files)", count)
    return _Resp(
        content=buf.read(),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="smime-key-backup.zip"'},
    )


@router.post("/api/smime/migrate-all-to-keyvault")
async def api_smime_migrate_all(request: Request, _: str = Depends(_require_admin)):
    """Migrate all local key.pem files to Azure Key Vault (creates key.pem.bak in fallback mode)."""
    import smime_store
    import keyvault as _kv

    if not _kv.is_configured():
        raise HTTPException(400, "Azure Key Vault ist nicht konfiguriert")

    # Pfad aus dem Modul, nicht erneut verdrahtet: smime_store.SMIME_DIR ist
    # die eine Quelle. Drei Endpunkte hatten hier ein eigenes Literal.
    smime_dir = _smime_store.SMIME_DIR
    results = []
    for user_dir in sorted(smime_dir.iterdir()):
        if not user_dir.is_dir() or user_dir.name == "recipients":
            continue
        certs_dir = user_dir / "certs"
        if not certs_dir.exists():
            continue
        email = user_dir.name
        for slot_dir in sorted(certs_dir.iterdir()):
            if not slot_dir.is_dir():
                continue
            if not (slot_dir / "key.pem").exists():
                continue
            slot_id = slot_dir.name
            result = await smime_store.migrate_key_to_keyvault(email, slot_id=slot_id)
            results.append({"email": email, "slot_id": slot_id, **result})
            log.info("bulk migrate: %s/%s → %s", email, slot_id, "ok" if result["ok"] else result.get("error"))

    ok_count = sum(1 for r in results if r["ok"])
    return JSONResponse({"ok": True, "migrated": ok_count, "total": len(results), "results": results})


@router.get("/api/smime/keyvault/status")
async def api_keyvault_status(_: str = Depends(_require_admin)):
    """Return per-mailbox Key Vault key presence status."""
    import keyvault
    import smime_store
    if not keyvault.is_configured():
        return JSONResponse({"configured": False, "keys": {}})
    import mailbox_match
    config_map: dict = settings_store.get("MAILBOX_CONFIG") or {}
    cert_emails = {c["email"] for c in smime_store.list_certs()}
    all_emails = sorted(set(mailbox_match.configured_addresses(config_map)) | cert_emails)
    keys: dict[str, bool] = {}
    for em in all_emails:
        keys[em] = await keyvault.key_exists(em)
    return JSONResponse({"configured": True, "vault_url": keyvault.vault_url(), "keys": keys})


@router.get("/api/smime/recipient/download/{cert_email}")
async def api_smime_recipient_download(cert_email: str, user: str = Depends(_check_auth)):
    import smime_store
    from fastapi.responses import Response
    from cryptography import x509
    from cryptography.hazmat.primitives.serialization import Encoding
    p = smime_store.get_recipient_cert_path(cert_email)
    if not p or not p.exists():
        raise HTTPException(404, "Zertifikat nicht gefunden")
    cert = x509.load_pem_x509_certificate(p.read_bytes())
    der_bytes = cert.public_bytes(Encoding.DER)
    safe_name = cert_email.replace("/", "_").replace("..", "_")
    return Response(
        content=der_bytes,
        media_type="application/pkix-cert",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.cer"'},
    )
