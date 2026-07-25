# Privacy Policy for EXO Signature Gateway and EXO Signature Hub

**Version 1.0 — 25 July 2026**

This notice informs you pursuant to Articles 13 and 14 GDPR about the processing
of personal data in connection with the software **EXO Signature Gateway**
(the "Gateway") and the service **EXO Signature Hub** (the "Hub").

A separate privacy policy applies to visits to our website:
https://blog.zarenko.net/datenschutzerklaerung-2/.

---

## 1. Controller

Alexander Zarenko – IT Consulting
Triebelsstraße 4, 52066 Aachen, Germany
Phone: +49 241 93688172
Email: alexander@zarenko.net

---

## 2. Allocation of Roles

2.1 The Gateway is **operated by the Customer in the Customer's own
infrastructure**. For the email content and traffic data processed there, the
Customer alone is the controller within the meaning of Art. 4(7) GDPR. We have
no access to that data.

2.2 We are the controller only for data that reaches us via the Hub. That data is
described exhaustively in Section 4.

2.3 **Private cryptographic keys** are generated and stored exclusively locally
in the Customer's Gateway. They never leave the Customer's infrastructure and are
not accessible to us.

---

## 3. No Automated Transmission

3.1 The Gateway does **not transmit data of its own accord** to the Hub —
neither once nor on an ongoing basis. In particular, **no usage statistics, no
mail flow data, no information about processed messages, and no measurement of
the number of activated mailboxes** are transmitted.

3.2 No automated monitoring of usage takes place. Every transmission described in
Section 4 requires an action by the Customer.

---

## 4. Processing Activities in Detail

### 4.1 Connecting a Gateway to the Hub

When a Customer connects their Gateway to the Hub, the following is transmitted
once and stored permanently by us:

- the Customer's email address,
- name or company designation, if provided (optional field),
- the domain of the Customer's Microsoft 365 tenant,
- the Gateway version number at the time of connection,
- a single-use technical token for retrieving the access key,
- for each accepted contractual document: document identifier, version number,
  cryptographic hash (SHA-256) of the document text, and the timestamp of
  acceptance.

**Purpose:** performance of the contractual relationship and verifiable evidence
of which contract versions the Customer accepted.
**Legal basis:** Art. 6(1)(b) GDPR (performance of a contract) and Art. 6(1)(f)
GDPR (legitimate interest in documenting the conclusion of the contract for
evidentiary purposes).

### 4.2 Certificate Orders

When a Customer orders an S/MIME certificate via the Hub, the following is
transmitted: the **email address of the mailbox** for which the certificate is to
be issued, the corresponding certificate signing request (CSR), the selected
certificate authority, and the timestamp of acceptance of that authority's terms.

This data is **passed on to the certificate authority** selected by the Customer,
as issuance is not possible without it. We are not a certificate authority
ourselves; we act solely as an intermediary. Depending on the selection, the
following may be involved: Certum (Asseco Data Systems S.A., Poland), SwissSign
AG (Switzerland), Sectigo, DigiCert, and SSL.com. The authority actually selected
is shown during the ordering process, where its terms must be accepted.

Where a certificate authority is established outside the EU/EEA and no adequacy
decision of the European Commission exists for that country, the transfer is
based on Art. 49(1)(b) GDPR, as it is necessary for the performance of the
contract initiated by the Customer. An adequacy decision exists for Switzerland.

**Legal basis:** Art. 6(1)(b) GDPR.

### 4.3 License Purchases

When a Customer purchases paid licenses, the identifier and domain of their
tenant are transmitted together with the **number of mailboxes to be licensed**.
This number states the **quantity ordered**; it is not a usage figure measured by
the Gateway (see Section 3).

**Legal basis:** Art. 6(1)(b) GDPR.

### 4.4 Invoice Purchase and Credit Assessment

If a Customer applies for payment by invoice, we process company name, billing
address, VAT identification number, contact person, and website, insofar as
provided.

**Legal basis:** Art. 6(1)(b) GDPR and, with regard to retention under
commercial and tax law, Art. 6(1)(c) GDPR.

We are entitled to obtain **information from credit agencies** before and during
the granting of invoice purchase. The legal basis is Art. 6(1)(f) GDPR; our
legitimate interest lies in mitigating the risk of default. Data subjects may
object to this processing pursuant to Art. 21 GDPR.

### 4.5 Diagnostic Bundles in Support Requests

For troubleshooting, Customers may voluntarily send us a **diagnostic bundle**
from their Gateway. Whether and when this happens is decided solely by the
Customer; no automatic transmission takes place.

Such a bundle may contain:

- configuration data of the Gateway (credentials and secrets are masked),
- technical log files,
- status information on the configured mailboxes,
- a **log of mail processing over the last seven days**. For each operation this
  contains the timestamp, **sender address, recipient addresses, subject line**,
  message identifier, size, and the processing result. **Message content is not
  included.**

**Note for our Customers:** A diagnostic bundle may therefore contain personal
data of your employees and of your correspondents. We process such data
exclusively on your instructions in order to handle your request. For this
processing we conclude a **data processing agreement pursuant to Art. 28 GDPR**
with you. Please check before uploading whether the transmission is necessary in
the specific case.

Diagnostic bundles are deleted once processing of the request is complete, and at
the latest after 90 days.

---

## 5. Processors Engaged

**Email delivery.** Confirmation and notification emails from the Hub are sent
via *Azure Communication Services* operated by Microsoft Ireland Operations
Limited, One Microsoft Place, South County Business Park, Leopardstown,
Dublin 18, Ireland. The recipient address, subject, and content of the respective
message are transmitted.

**Availability of the Hub.** The Hub is made publicly reachable via *Microsoft
Entra Application Proxy* operated by Microsoft Ireland Operations Limited. In
doing so, Microsoft processes connection data (including IP address and time of
access) on our behalf. Processing takes place via data centres within the
European Union.

Data processing agreements pursuant to Art. 28 GDPR are in place with both
services. The **legal basis** for their use is Art. 6(1)(b) GDPR and Art. 6(1)(f)
GDPR (legitimate interest in reliable and secure operation).

---

## 6. Overview of Recipients

| Recipient | Occasion | Data transmitted |
|---|---|---|
| Certificate authority (Certum, SwissSign, Sectigo, DigiCert, SSL.com) | Certificate order placed by the Customer | Mailbox address, certificate signing request |
| Microsoft Ireland Operations Limited | Email delivery, availability of the Hub | Recipient address and message content; connection data |
| Credit agency | Application for invoice purchase | Company and address data |
| Tax advisors, tax authorities | Statutory retention and reporting obligations | Invoice and contract data |

---

## 7. Retention Periods

7.1 Customer account master data, records of consent, and order data are retained
for the duration of the contractual relationship and beyond that within the
statutory retention periods (generally six or ten years pursuant to § 257 HGB and
§ 147 AO).

7.2 Records of consent are retained until the limitation periods for possible
claims arising from the contractual relationship have expired.

7.3 Diagnostic bundles are deleted in accordance with Section 4.5.

---

## 8. Rights of Data Subjects

8.1 You have the right of **access** to the data stored about you (Art. 15
GDPR), to **rectification** of inaccurate data (Art. 16 GDPR), to **erasure**
(Art. 17 GDPR), to **restriction of processing** (Art. 18 GDPR), and to **data
portability** (Art. 20 GDPR).

8.2 Where we process data on the basis of Art. 6(1)(f) GDPR, you have the right
to **object** on grounds relating to your particular situation (Art. 21 GDPR).

8.3 You may **withdraw** any consent given at any time with effect for the
future. The lawfulness of processing carried out until withdrawal remains
unaffected.

8.4 You have the right to **lodge a complaint with a supervisory authority**, in
particular in the Member State of your habitual residence, place of work, or the
place of the alleged infringement. The authority responsible for us is the State
Commissioner for Data Protection and Freedom of Information of North
Rhine-Westphalia.

8.5 Data subjects whose data reached us via a diagnostic bundle under Section 4.5
should primarily contact the respective Customer as the controller; we support
the Customer in responding.

---

## 9. Changes to This Notice

We update this notice when the processing activities described change. The
version in force is available via the Hub and in the Gateway interface under
"Legal documents". The version stated above is authoritative.

---

*Version 1.0 — 25 July 2026*
