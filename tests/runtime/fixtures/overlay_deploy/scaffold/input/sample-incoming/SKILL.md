---
name: sample-incoming
description: |
  Fixture for PR 7 (scaffold-claude-code-skill). Simulates a Claude-Code
  skill discovered in the plugin-install path with no skill.yaml manifest.
  The scaffold subcommand reads this dir and emits a starter skill.yaml v2
  with a single claude_code profile.
---

# sample-incoming

This skill exists only to verify scaffolding. The presence of this SKILL.md
plus the sibling `scripts/runner.py` should produce a starter skill.yaml
that:

1. declares `schema_version: "2"`
2. fills `identity.skill_id: sample-incoming`
3. enables a single `claude_code` profile
4. lists files: `["SKILL.md", "scripts/runner.py"]`
5. sets `deploy_to: [claude_code, cowork]` by default
6. emits an empty `profiles/claude_code/canaries.jsonl` with a TODO row
   that fails the validator until filled (per #33 hard-fail).
