---
prompt_id: starter_email_classifier_local
version: "1"
description: Local-model starter email classifier.
---
Classify the latest email message only.

Use this small starter taxonomy:
- `newsletter`: recurring, broadcast-style, digest-like, list-like, or promotional senders
- `general`: ordinary human communication, invitations, follow-ups, coordination, notes
- `other`: neither label is supportable

Use this starter intent taxonomy:
- `informational`: update, FYI, note, finished state, receipt of information
- `action_required`: the latest message asks the recipient to do something
- `others`: intent is not supportable

Prioritize the latest message, not the whole historical topic.
Treat sender style, unsubscribe signals, and list-like formatting as strong evidence for `newsletter`.
Return one JSON object matching the schema exactly.
---
