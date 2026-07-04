# Why this repo extends Ralph

This project borrows the chassis ideas from the Ralph agent-harness pattern
(deterministic hooks, evidence-backed reviews, a bounded autonomous loop) and
keeps the parts of SAI that Ralph has no concept of. This note explains what we
took, what we kept, and why.

## What we took from Ralph

1. **Deterministic hooks as enforcement, not discipline.** A hook runs in the
   agent session and cannot be skipped by forgetting a setup step. We use this to
   make the boundary check un-bypassable at author time (see the guarantee below).
2. **A different-vendor review gate.** Security-relevant changes get an
   adversarial review from a second, independent model vendor before they ship.
   This also satisfies the rule that the surface which wrote a change never
   certifies its own deployment.
3. **A bounded autonomous loop.** Repeated passes with progress kept in files and
   git rather than in one context window, with checks as the escape condition.

## The boundary guarantee is three layers

Keeping personal data out of this public repo does not rely on any single
mechanism. Three independent layers each enforce the same boundary check:

1. **CI and branch protection (primary, covers every actor).** The boundary
   workflow runs on every push and pull request to the main branch, and branch
   protection requires it to pass before a merge. This covers all contributors
   and automation, not just one machine.
2. **The git pre-commit hook (contributor-local).** Installed with one command
   (`make hooks`), it runs the boundary linter on every commit so violations are
   caught before they are recorded locally.
3. **The agent-session hook (this repo's `.claude` layer).** A pre-tool hook runs
   the boundary linter before a `git commit` or `git push` inside an agent session
   and blocks on any finding, so an agent that never ran `make hooks` is still
   covered. It fails closed: a parse error, a missing linter, or a linter that
   exits non-zero all block.

The agent-session hook is defense in depth. It protects agent sessions with this
repo open as the project; it does not replace the CI and pre-commit layers, which
remain the guarantee for every other actor.

## The different-vendor review

`scripts/cross_review.py` runs an adversarial review of a file by a different
vendor (OpenAI), the second-opinion step. Provide `OPENAI_API_KEY` through your
1Password wrapper (never hardcode a secret reference; the boundary linter blocks
those). Pass `--context` describing what the artifact IS, so the reviewer does not
mis-frame it. Before sending, the tool runs the boundary linter on the target and
refuses if it is flagged, so private data never leaves for an external vendor. It
is advisory: a human triages the findings; it is not a gate.

## The verify entry

`scripts/verify.sh` is this repo's local mirror of the CI shippability gate (the
equivalent of Ralph's per-language `verify.sh`). It runs the same checks CI runs
and is fail-closed. It duplicates CI's check list today, so if the CI workflow
changes, update the script; the proper single-source fix (CI calls the script) is
a future increment that must keep the branch-protection-required `boundary-check`
status name. The verify entry is also what a future bounded loop will use as its
escape condition.

## What SAI keeps that Ralph does not have

- **Eval-first graduation.** A workflow earns autonomy only when it has an eval
  dataset, has run manually under human review, and a human decides to promote it.
  Cheaper tiers graduate the same way. Ralph has no eval-dataset or graduation
  concept.
- **Hash-verified deployment.** A skill is not live until its deployed bytes match
  the source, verified by hash.
- **Hard human gates.** Approval to code and a separate approval to push and to
  deploy are mandatory. The bounded loop never pushes on its own.
- **Repo-specific checks.** The content-addressed prompt locks and the generated
  tool overview stay in the verification set.

## The bounded loop (v1: in-session, single task)

`scripts/bounded_loop.py` is the loop's safety contract: it runs an `--attempt-cmd`
then an `--escape-cmd` until the escape check passes, and stops on any hard cap
(iterations, wall-clock, or a stuck no-change run). It NEVER pushes; that guarantee
is about the runner. `scripts/loop_example.sh` wires the real headless agent as the
driver: `claude -p "<task>" --permission-mode acceptEdits`, run in a throwaway
worktree. `acceptEdits` keeps the PreToolUse hooks (danger_guard, boundary_guard)
blocking even mid-loop, so a loop-driven agent still cannot rewrite history or leak
private data. Run it from a plain terminal, never nested in a live session; review
the branch diff before any push. Unattended (cron), parallel worktrees, and
review-in-loop are later increments.

## Bounded-loop prerequisites (hard, before any autonomous loop is enabled)

- A token budget that caps the run.
- The eval gate as the loop's escape condition, not just an iteration limit.
- A human approval gate before any push or deploy.

Until all three are in place, the autonomous loop stays off and work runs under
the hard human gates above.
