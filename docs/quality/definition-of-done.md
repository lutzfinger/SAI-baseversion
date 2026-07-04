# Definition of done: when a workflow may become autonomous

This repo separates two gates. The mechanical gate proves the code is sound. The
human gate decides whether a workflow is trusted enough to run on its own.

## The mechanical gate

`scripts/verify.sh` runs the same checks CI runs (boundary linter, framework
regression, sample-skill integrity and cascade end to end). It must pass before
anything is pushed. Passing it means the code works; it does not by itself mean a
workflow should run without a human.

## The human gate: graduation to an autonomous email flow

A workflow may be promoted from manual (run inside Claude Code with a person
watching) to autonomous (triggered by email, running on its own) only when all
three are true:

1. **It has a data eval set.** There is a dataset of real cases with known
   correct outcomes that the workflow is scored against, so quality is measured,
   not assumed.
2. **It has run manually with a human in the loop who approved the process.**
   The workflow has been exercised on real work with a person reviewing each run,
   and that person judged the process sound.
3. **A human explicitly decides to promote it.** Promotion is a deliberate human
   decision, not an automatic consequence of the first two. No workflow promotes
   itself.

Until all three hold, the workflow stays manual in Claude Code. The mechanical
gate being green is necessary but not sufficient; graduation is the human gate on
top of it.

## Future note

Today this rule is read and applied by a person. When an autonomous loop needs to
check graduation as part of deciding what it may run, this rule will also need a
machine-readable form (for example a per-workflow manifest field). That is a
future increment, not part of the rule itself.
