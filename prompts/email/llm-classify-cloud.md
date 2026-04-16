---
prompt_id: starter_email_classifier_cloud
version: "1"
description: Cloud-model starter email classifier.
---
Classify the latest email message only.

Use the same starter taxonomy:
- `newsletter`
- `general`
- `other`

Use the same starter intent taxonomy:
- `informational`
- `action_required`
- `others`

Prefer precision over over-classification.
If the email looks like a mailing-list, digest, marketing broadcast, or subscription update, prefer `newsletter`.
If it is regular person-to-person communication, prefer `general`.
Return one JSON object matching the schema exactly.
---
