---
name: okf-acceptance-pass
description: Run the first-time-user acceptance pass before reporting the goal met. Use when every milestone is checked and the success criteria pass, before declaring the goal met — tests prove the contract, this pass proves the experience.
---

# Acceptance pass

Exercise the deliverable through its primary interface the way a first-time user would, from nothing:

1. **Clean checkout.** Fresh clone (or a pristine copy) in a new directory — not the working tree. Stale state in the working copy hides setup bugs.
2. **README quickstart, verbatim.** Run exactly the commands the README gives, in order, on the clean checkout. Every deviation you need is a finding.
3. **The goal's example interactions.** Drive each example from `docs/GOAL.md` through the real interface (typed input for an app, command lines for a utility, requests for a service).
4. **Variants and wrong inputs.** Obvious mutations of the examples plus classic breakage: a pasted URL where a bare id is expected, a missing argument, an empty value, a nonsense value in required config, an already-taken port, a file where a folder is expected. Every failure must be a clear message naming the expected format — no stack traces.
5. **Observe the real output.** If the deliverable sends, writes, or serves something, verify the artifact itself (the message in the sink, the file on disk, the page in the browser), not just an exit code.

Handling findings:

- Fix in-scope breakage before declaring the goal met; each fix lands with its docs and log entry like any other change.
- Out-of-scope findings become candidate next milestones in the goal-met proposals — do not silently expand scope.
- If a verification step needs a tool that isn't installed, that install is owner-gated: record the blocker in `docs/log.md`, leave the milestone checkbox open, and continue with what can be verified.

Record the pass in `docs/log.md`: what was exercised, what broke, what was fixed, what was deferred. The goal-met report references this entry.
