# Fixtures for `sai-overlay deploy` and friends

Per `docs/PLAN-UNIFIED-SKILL-SYNC.md` (in SAI overlay). Per-PR breakdown:

| PR | Subcommand | Fixtures used |
|---|---|---|
| 1 | schema validator | `manifests/{valid-*.yaml, invalid-*.yaml}` |
| 2 | `deploy --target claude_code` | `skills/sample-alpha/` (synthetic) |
| 3 | `deploy-status` | `deploy-logs/{clean,drifted}.jsonl` |
| 4 | `deploy --target cowork` | `cowork/expected/sample-alpha-v0.1.0.skill` (golden) |
| 5 | launchd visibility | (none — ops smoke test) |
| 6 | `import-skill` | `import/sample-alpha-v0.1.0.skill` |
| 7 | `scaffold-claude-code-skill` | `scaffold/input/sample-incoming/` |

All fixture names are SYNTHETIC per #17 — these tests ship in the public
framework and must not contain operator-specific data. For real-skill
regression coverage, see `SAI/eval/skill_sync_inventory.jsonl` (private
overlay).

## Conventions

- Synthetic skill IDs: `sample-alpha`, `sample-beta`, `sample-incoming`.
- Synthetic version: `0.1.0`.
- Synthetic owner: `framework-tests`.
- All file content is the minimum needed to exercise the validator —
  not realistic skill bodies.
