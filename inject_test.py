import config, settings_store
settings_store.init(config._ENV_SEEDS)
import graph_client, httpx, smtplib, email as _em

token = graph_client._acquire_token()
h = {"Authorization": f"Bearer {token}"}

# $search durchsucht alle Ordner
resp = httpx.get(
    "https://graph.microsoft.com/v1.0/users/alexander@zarenko.net/messages"
    "?$search=\"leniart OR marek OR vivawest\"&$select=id,subject,from,internetMessageHeaders&$top=25",
    headers=h, timeout=30,
)

target_id = None
for m in resp.json().get("value", []):
    addr = m["from"]["emailAddress"]["address"]
    hdrs = {x["name"].lower(): x["value"] for x in m.get("internetMessageHeaders", [])}
    ct = hdrs.get("content-type", "")
    signed = "signed" in ct.lower() or "pkcs7" in ct.lower()
    print(f"signed={signed} | {addr} | {m['subject'][:55]}")
    if signed and not target_id:
        target_id = m["id"]
        print(f"  --> Ziel-Mail!")

if not target_id:
    print("\nKeine signierte Mail. Versuche direkt '$value' fuer erste Marek-Mail...")
    resp2 = httpx.get(
        "https://graph.microsoft.com/v1.0/users/alexander@zarenko.net/messages"
        "?$search=\"leniart OR marek OR vivawest\"&$select=id,subject,from&$top=5",
        headers=h, timeout=30,
    )
    mails = [m for m in resp2.json().get("value", [])
             if any(x in m["from"]["emailAddress"]["address"].lower()
                    for x in ("vivawest", "leniart", "marek"))]
    if mails:
        target_id = mails[0]["id"]
        print(f"Versuche: {mails[0]['from']['emailAddress']['address']} | {mails[0]['subject'][:50]}")

if not target_id:
    print("Keine Mail gefunden")
    raise SystemExit(1)

raw_resp = httpx.get(
    f"https://graph.microsoft.com/v1.0/users/alexander@zarenko.net/messages/{target_id}/$value",
    headers={**h, "Accept": "text/plain"},
    timeout=30,
)
print(f"\nHTTP {raw_resp.status_code}, {len(raw_resp.content)} Bytes")
if raw_resp.status_code != 200:
    print(raw_resp.text[:300])
    raise SystemExit(1)

msg = _em.message_from_bytes(raw_resp.content)
mail_from = _em.utils.parseaddr(msg.get("From", ""))[1]
ct_actual = msg.get_content_type()
print(f"From: {mail_from}")
print(f"Content-Type: {ct_actual}")
print(f"Subject: {msg.get('Subject', '')}")

if "signed" not in ct_actual and "pkcs7" not in ct_actual:
    print("WARNUNG: Mail ist nicht mehr signiert (wurde evtl. schon durch den Proxy verarbeitet)")

print("\nSende an Proxy localhost:25 ...")
with smtplib.SMTP("localhost", 25, timeout=15) as smtp:
    smtp.ehlo()
    smtp.starttls()
    smtp.ehlo()
    smtp.sendmail(mail_from or "marek@leniart.eu", ["alexander@zarenko.net"], raw_resp.content)
print("Injiziert! Prüfe Logs mit: docker logs <container> --since=1m")
