---
name: sample-alpha
description: Synthetic skill for sai-overlay deploy tests. Not a real skill.
---

# sample-alpha

This is a fixture used by `tests/runtime/test_overlay_deploy.py` to verify
that `sai-overlay deploy --target claude_code` writes a Claude-Code skill
correctly and that `--target cowork` produces a valid ZIP package.

The body is intentionally short so test diffs stay readable.
