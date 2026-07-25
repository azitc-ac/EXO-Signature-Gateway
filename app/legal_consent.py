"""Consent tracking for legal documents (SQLite, append-only).

Documents live in /app/legal/{lang}/ and are baked into the image.
Consent records are stored in /app/data/legal_consent.db.

Context → required document IDs:
  hub_connect     → hub-terms, license-supplement  (Gate A)
  invoice_request → payment-invoice               (Gate B)
"""
import hashlib
import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

_LEGAL_DIR = Path("/app/legal")
_DB_PATH = Path("/app/data/legal_consent.db")

# ── Document registry ────────────────────────────────────────────────────────
CURRENT_DOCUMENTS: dict[str, dict] = {
    "hub-terms": {
        "version": "1.0",
        "label_de": "Hub-Nutzungsbedingungen",
        "label_en": "Hub Terms of Use",
        "path_de": "de/hub-nutzungsbedingungen-v1.0.md",
        "path_en": "en/hub-terms-of-use-v1.0.md",
    },
    "license-supplement": {
        "version": "1.0",
        "label_de": "Lizenzbedingungen-Ergänzung",
        "label_en": "License Terms Supplement",
        "path_de": "de/lizenzbedingungen-ergaenzung-v1.0.md",
        "path_en": "en/license-supplement-v1.0.md",
    },
    "payment-invoice": {
        "version": "1.0",
        "label_de": "Zahlungsbedingungen Rechnungskauf",
        "label_en": "Payment Terms for Invoice Purchase",
        "path_de": "de/zahlungsbedingungen-rechnung-v1.0.md",
        "path_en": "en/payment-terms-invoice-v1.0.md",
    },
    "price-list": {
        "version": "1.0",
        "label_de": "Preisliste",
        "label_en": "Price List",
        "path_de": "de/preisliste-v1.0.md",
        "path_en": "en/price-list-v1.0.md",
        "no_consent_required": True,
    },
    # Eine Datenschutzerklärung ist eine INFORMATION nach Art. 13/14 DSGVO, keine
    # Willenserklärung — sie wird zur Kenntnis genommen, nicht akzeptiert. Daher
    # no_consent_required: sie erscheint in der Dokumentenliste und ist lesbar,
    # taucht aber in keinem Consent-Gate auf.
    "product-privacy": {
        "version": "1.0",
        "label_de": "Datenschutzerklärung (Gateway & Hub)",
        "label_en": "Privacy Policy (Gateway & Hub)",
        "path_de": "de/produkt-datenschutz-v1.0.md",
        "path_en": "en/product-privacy-policy-v1.0.md",
        "no_consent_required": True,
    },
    # Art. 28 Abs. 3 DSGVO verlangt, dass die Verarbeitung durch einen Vertrag
    # GEREGELT IST — der Vertrag muss also VOR der ersten Übermittlung stehen.
    # Deshalb echte Zustimmung (kein no_consent_required) und ein eigenes Gate
    # auf dem Support-Upload (CONTEXT_DOCUMENTS["support_upload"]).
    "dpa": {
        "version": "1.0",
        "label_de": "Auftragsverarbeitungsvertrag (Diagnosepakete)",
        "label_en": "Data Processing Agreement (diagnostic bundles)",
        "path_de": "de/auftragsverarbeitung-v1.0.md",
        "path_en": "en/data-processing-agreement-v1.0.md",
    },
}

CONTEXT_DOCUMENTS: dict[str, list[str]] = {
    "hub_connect":      ["hub-terms", "license-supplement"],
    "invoice_request":  ["payment-invoice"],
    "license_purchase": ["license-supplement"],
    # Gate C — Diagnosepaket-Upload. Der AVV muss VOR der ersten Übermittlung
    # geschlossen sein (Art. 28 Abs. 3 DSGVO), nicht erst danach.
    "support_upload":   ["dpa"],
}


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(str(_DB_PATH))
    c.row_factory = sqlite3.Row
    c.execute("""
        CREATE TABLE IF NOT EXISTS consents (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id TEXT NOT NULL,
            version     TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            context     TEXT NOT NULL DEFAULT '',
            accepted_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
        )
    """)
    c.commit()
    return c


def _doc_path(doc_id: str, lang: str = "de") -> Path | None:
    doc = CURRENT_DOCUMENTS.get(doc_id)
    if not doc:
        return None
    key = "path_de" if lang != "en" else "path_en"
    return _LEGAL_DIR / doc.get(key, doc["path_de"])


def compute_document_hash(doc_id: str) -> str:
    """SHA-256 of the German (authoritative) document text."""
    p = _doc_path(doc_id, "de")
    if not p or not p.exists():
        return ""
    return hashlib.sha256(p.read_bytes()).hexdigest()


def get_document_text(doc_id: str, lang: str = "de") -> str:
    """Read Markdown text. Falls back to DE if EN not found."""
    p = _doc_path(doc_id, lang)
    if p and p.exists():
        return p.read_text(encoding="utf-8")
    if lang != "de":
        p2 = _doc_path(doc_id, "de")
        if p2 and p2.exists():
            return p2.read_text(encoding="utf-8")
    return ""


def has_valid_consent(document_id: str) -> bool:
    """True if the current version + content hash has been accepted."""
    doc = CURRENT_DOCUMENTS.get(document_id)
    if not doc:
        return False
    version = doc["version"]
    h = compute_document_hash(document_id)
    if not h:
        return False
    try:
        with _conn() as c:
            row = c.execute(
                "SELECT id FROM consents WHERE document_id=? AND version=? AND content_hash=? LIMIT 1",
                (document_id, version, h),
            ).fetchone()
            return row is not None
    except Exception as e:
        log.error("legal_consent.has_valid_consent: %s", e)
        return False


def context_consented(context: str) -> bool:
    """True if all documents required for this context have valid consent."""
    return all(has_valid_consent(d) for d in CONTEXT_DOCUMENTS.get(context, []))


def record_consent(document_id: str, version: str, content_hash: str,
                   context: str = "") -> bool:
    """Append a consent record. Returns True on success."""
    try:
        with _conn() as c:
            c.execute(
                "INSERT INTO consents (document_id, version, content_hash, context) VALUES (?,?,?,?)",
                (document_id, version, content_hash, context or ""),
            )
            return True
    except Exception as e:
        log.error("legal_consent.record_consent: %s", e)
        return False


def consent_status_all() -> dict:
    """For each document: version, labels, accepted bool, accepted_at."""
    result: dict[str, dict] = {}
    for doc_id, doc in CURRENT_DOCUMENTS.items():
        version = doc["version"]
        h = compute_document_hash(doc_id)
        accepted_at = None
        if h:
            try:
                with _conn() as c:
                    row = c.execute(
                        "SELECT accepted_at FROM consents "
                        "WHERE document_id=? AND version=? AND content_hash=? "
                        "ORDER BY id DESC LIMIT 1",
                        (doc_id, version, h),
                    ).fetchone()
                    if row:
                        accepted_at = row["accepted_at"]
            except Exception:
                pass
        result[doc_id] = {
            "version": version,
            "label_de": doc.get("label_de", doc_id),
            "label_en": doc.get("label_en", doc_id),
            "accepted": accepted_at is not None,
            "accepted_at": accepted_at,
            "no_consent_required": doc.get("no_consent_required", False),
        }
    return result


def get_consent_receipts_for_hub() -> list[dict]:
    """Return structured consent records for all hub_connect documents.
    Called by hub_client.register() to bundle receipts with the registration payload.
    Returns [{doc_id, version, content_hash, accepted_at}] for each accepted document,
    empty list if any document is not yet accepted."""
    doc_ids = CONTEXT_DOCUMENTS.get("hub_connect", [])
    receipts: list[dict] = []
    try:
        with _conn() as c:
            for doc_id in doc_ids:
                doc = CURRENT_DOCUMENTS.get(doc_id)
                if not doc:
                    continue
                version = doc["version"]
                h = compute_document_hash(doc_id)
                if not h:
                    continue
                row = c.execute(
                    "SELECT accepted_at FROM consents "
                    "WHERE document_id=? AND version=? AND content_hash=? "
                    "ORDER BY id ASC LIMIT 1",
                    (doc_id, version, h),
                ).fetchone()
                if row:
                    receipts.append({
                        "doc_id": doc_id,
                        "doc_version": version,
                        "doc_hash": h,
                        "accepted_at": row["accepted_at"],
                    })
    except Exception as e:
        log.error("legal_consent.get_consent_receipts_for_hub: %s", e)
    return receipts


def get_consent_history(limit: int = 200) -> list[dict]:
    """Recent consent records, newest first."""
    try:
        with _conn() as c:
            rows = c.execute(
                "SELECT document_id, version, context, accepted_at FROM consents "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception as e:
        log.error("legal_consent.get_consent_history: %s", e)
        return []
