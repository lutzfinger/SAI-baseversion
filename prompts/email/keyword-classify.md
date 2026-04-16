---
prompt_id: keyword_email_classifier_starter
version: "1"
description: Deterministic newsletter pre-filter for the starter repo.
classifier:
  newsletter_sender_emails: []
  newsletter_sender_domains:
    - substack.com
    - beehiiv.com
    - mail.beehiiv.com
    - info.axios.com
  newsletter_subject_keywords:
    - newsletter
    - unsubscribe
    - digest
    - briefing
---
Use only the classifier config and the message metadata.

This tool is intentionally narrow:
- only obvious sender-email rules
- only obvious sender-domain rules
- only obvious unsubscribe or newsletter-style keywords

If no rule clearly matches, fall back to:
- `level1_classification = other`
- `level2_intent = others`

Do not infer nuanced human intent here.
---
