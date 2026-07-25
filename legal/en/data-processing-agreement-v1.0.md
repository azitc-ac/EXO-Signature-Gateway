# Data Processing Agreement (DPA)

**Version 1.0 — 25 July 2026**

Between the **Customer** (the "Controller") and

Alexander Zarenko – IT Consulting, Triebelsstraße 4, 52066 Aachen, Germany
(the "Processor")

the following is agreed pursuant to Art. 28 GDPR.

**The German version of this document is legally binding. This English
translation is provided for information purposes only. In the event of any
discrepancy, the German version shall prevail.**

---

## 1. Subject Matter and Occasion

1.1 This agreement applies **exclusively to diagnostic bundles** that the
Controller voluntarily transmits from their EXO Signature Gateway to the
Processor in order to have a support request handled.

1.2 **It does not apply to ongoing operation of the Gateway.** The Gateway is
operated by the Controller in their own infrastructure; the Processor has no
access to the email traffic processed there. No processing on behalf of the
Controller takes place in that respect (Section 2.5 of the Terms of Use for the
EXO Signature Hub).

1.3 Nor does it apply to data that the Processor processes as controller in its
own right — such as customer account master data, records of consent, or order
data. The Privacy Policy for EXO Signature Gateway and EXO Signature Hub applies
to those.

---

## 2. Nature, Purpose and Duration of Processing

2.1 **Nature of processing:** storing, reading, analysing and deleting the data
contained in the diagnostic bundle.

2.2 **Purpose:** exclusively the analysis and remediation of the malfunction
described by the Controller.

2.3 **Duration:** until the support request has been handled, and at the latest
**90 days** from receipt of the diagnostic bundle. Deletion then follows under
Section 9.

---

## 3. Categories of Data Subjects and Types of Data

3.1 **Data subjects:** employees of the Controller and their external
correspondents.

3.2 **Types of data.** A diagnostic bundle may contain:

- configuration data of the Gateway; credentials and secrets are masked,
- technical log files,
- status information on configured mailboxes (email addresses, certificate
  status),
- a **log of mail processing over the last seven days**, containing the
  timestamp, **sender address, recipient addresses, subject line**, message
  identifier, message size, and processing result.

3.3 **Message content and private cryptographic keys are not included.**

3.4 Special categories of personal data under Art. 9 GDPR are not the subject of
the processing. The Controller shall ensure that no such data is transmitted.
Subject lines may nevertheless contain sensitive information in individual
cases; the Controller checks this on their own responsibility before
transmission.

---

## 4. Instructions

4.1 The Processor processes the data **exclusively on documented instructions**
from the Controller. Transmitting a diagnostic bundle together with the
associated request constitutes an instruction to process the data contained
therein for the purpose stated in Section 2.2.

4.2 Further or differing instructions shall be issued by the Controller in text
form.

4.3 The Processor shall inform the Controller without delay if it considers that
an instruction infringes data protection law. It is entitled to suspend
execution until the Controller confirms or amends the instruction.

4.4 Processing for the Processor's own purposes — in particular product
improvement, statistics, or advertising — does not take place.

---

## 5. Confidentiality

5.1 The Processor shall commit persons involved in the processing to
confidentiality, unless they are already subject to a statutory duty of
confidentiality. The commitment continues beyond the end of their activity.

5.2 Access to diagnostic bundles is granted only to persons who require it to
handle the respective request.

---

## 6. Technical and Organisational Measures (Art. 32 GDPR)

6.1 The Processor implements the following measures:

- **Transmission:** upload takes place exclusively over a TLS-encrypted
  connection.
- **Access control (system):** access to the Hub is protected by
  authentication; administrative access is restricted to the Processor.
- **Access control (data):** diagnostic bundles are assigned to a customer
  account and are accessible only via that account and by the Processor.
- **Masking:** credentials and secrets are masked when the bundle is created in
  the Controller's Gateway and do not reach the Processor in plain text.
- **Storage limitation:** automatic deletion under Section 2.3.
- **Separation:** data of different customers is stored separately.
- **Logging:** access to diagnostic bundles is logged.

6.2 The Processor reviews these measures regularly and adapts them to the state
of the art. Changes must not reduce the level of protection.

---

## 7. Sub-processors

7.1 The Controller grants **general authorisation** for the engagement of
sub-processors pursuant to Art. 28(2) sentence 2 GDPR.

7.2 Sub-processors engaged at the time of conclusion of this agreement:

| Company | Service | Location |
|---|---|---|
| Microsoft Ireland Operations Limited | Provision of public reachability of the Hub | Ireland |

7.3 The Processor shall give notice of intended changes in text form **at least
30 days in advance**. The Controller may object within that period on data
protection grounds. In the event of an objection, the Processor may discontinue
the affected service; the Controller shall suffer no disadvantage as a result.

7.4 The Processor shall impose on sub-processors a level of protection
equivalent to that of this agreement.

---

## 8. Third-Country Transfers

8.1 No processing takes place outside the EU/EEA.

8.2 Should this become necessary in future, it will take place only on the basis
of an adequacy decision or appropriate safeguards under Art. 46 GDPR (in
particular standard contractual clauses), and will be notified under
Section 7.3.

---

## 9. Deletion and Return

9.1 Diagnostic bundles are deleted once the request has been handled, and at the
latest upon expiry of the period under Section 2.3.

9.2 The Controller may at any time request earlier deletion in text form. The
Processor shall comply without delay; this may render handling of the request
impossible.

9.3 Return of the data is not required, as the Controller holds the source data
in their own Gateway.

9.4 Statutory retention obligations remain unaffected. As matters stand, none
apply to diagnostic bundles.

---

## 10. Assistance Obligations

10.1 The Processor shall assist the Controller with appropriate measures in
fulfilling data subject rights under Art. 12 to 23 GDPR, insofar as these relate
to diagnostic bundles.

10.2 It shall assist in complying with the obligations under Art. 32 to 36
GDPR, in particular data protection impact assessments and notification duties.

10.3 **Notification of breaches.** The Processor shall notify the Controller of
a personal data breach without undue delay and at the latest **within 24 hours**
of becoming aware of it, in text form, stating the known circumstances.

---

## 11. Evidence and Audits

11.1 On request, the Processor shall make available the information necessary to
demonstrate compliance with this agreement.

11.2 The Controller may satisfy itself of compliance after prior notice with
reasonable lead time during normal business hours. Audits shall be conducted so
as not to unreasonably disrupt operations and to preserve the rights of third
parties.

11.3 As a rule, the submission of meaningful evidence in text form shall
suffice. An on-site audit shall be considered only where there are concrete
indications of an infringement.

---

## 12. Liability

12.1 Art. 82 GDPR applies.

12.2 In the internal relationship, the liability provisions of the Terms of Use
for the EXO Signature Hub apply in addition.

---

## 13. Term and Final Provisions

13.1 This agreement applies for as long as the Controller uses the support
channel or diagnostic bundles of the Controller are held by the Processor.

13.2 It terminates automatically upon deletion of all of the Controller's
diagnostic bundles.

13.3 Amendments require text form. Acceptance via the Gateway interface is
sufficient.

13.4 German law applies. The place of jurisdiction is Aachen, insofar as the
Controller is a merchant.

13.5 Should any provision be invalid, the validity of the remaining provisions
shall remain unaffected.

13.6 The German version is authoritative. Translations are provided for
information only.

---

*Version 1.0 — 25 July 2026*
