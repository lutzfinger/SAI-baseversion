"""Canonical-memory loaders + validators.

Public mechanisms; values live in operator's private overlay under
``config/``. Per PRINCIPLES.md §17 the schemas + loaders + matchers
ship in public; the data + names + emails ship in private.

Modules:
  * courses               — operator's courses + late-work policies
  * teaching_assistants   — TA roster with per-course active terms
  * sender_validation     — From / Reply-To / forward-detection
  * text_sanitization     — strip control chars, cap length, URL-mask
  * crisis_patterns       — hard-stop matcher (patterns private)
  * reply_validation      — structured ReplyDraft + safety validators
"""
