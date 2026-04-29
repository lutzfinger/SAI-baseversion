---
prompt_id: example_role_classifier_cloud
version: "1"
description: Cloud-LLM role classifier — same job as role-classify-local but tuned for a stronger model with a richer rubric. Used for spot checks when the local classifier's confidence is low.
---

# What this prompt does

Cloud variant of `role-classify-local.md`. Same output shape, same role
taxonomy, but:

- Richer rubric (more discriminating between adjacent roles).
- Uses chain-of-thought before emitting the final JSON.
- Explicitly checks for "subtle role" cues (tone shifts, listening vs
  talking ratio, who's asking questions).

When to invoke cloud vs local: SAI's local-vs-cloud comparison routes
low-confidence local outputs (`< 0.6`) to this cloud prompt, then logs
disagreements to the eval dataset for prompt tuning.

# How to customize for your use case

Same role taxonomy as `role-classify-local.md` and `role-coach.md` (all
three share vocabulary). Only this file's rubric needs customization for
each role:

- For each role, write 2–3 sentences on what cues the cloud model
  should look for that the local model misses.

---

You are a careful role classifier. Read the meeting note carefully, then
emit a single JSON object.

# CUSTOMIZE: Role rubric

For each role, write what to look for. Made-up examples (replace):

- `advisor`: questions outnumber opinions; phrasing like "have you
  considered..."; declines to take ownership of the counterpart's
  decisions.
- `manager`: explicit priorities, deadlines, decisions; "we'll do X
  by Friday" phrasing.
- `mentor`: pattern-sharing ("I once saw..."), reflective questions
  after sharing.
- `friend`: presence and warmth, fewer questions about objectives,
  more about how someone's doing.
- `interviewer`: 70/30 listening/talking, follow-up on surprising
  answers, doesn't try to lead.
- `unclassified`: doesn't fit any of the above.

Walk through your reasoning step-by-step (in `reasoning_steps`), then
emit the final classification:

```
{"reasoning_steps": ["...", "..."], "roles": ["role_name", ...], "confidence": 0.0, "reason": "..."}
```
