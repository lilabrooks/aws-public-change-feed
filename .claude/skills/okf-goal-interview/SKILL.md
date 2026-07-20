---
name: okf-goal-interview
description: Run the goal interview to fill docs/GOAL.md and CLAUDE.md with the owner. Use when either file still has template brackets or missing goal content, before the first milestone task — also when the owner asks to define, redefine, or bootstrap the repo goal.
---

# Goal interview

Gather what's missing as a short interview — a few questions at a time, drafting as you go — instead of asking the owner to edit templates. A running example (a design-tokens CLI) illustrates the expected shape of each answer.

Ask for, in order:

1. **What is being built and for whom** → Kind and Problem. Example: a CLI that turns Figma exports into design tokens, for the frontend team → Kind: utility.
2. **What exists when it's done, concretely** → Target state. Example: `tokens build` converts every Figma export in the fixtures folder into JSON and CSS variables.
3. **The example interactions** — 2-3 realistic examples of what a user will actually give the primary interface, including at least one messy or wrong one → spec examples and test cases. Phrase per repo kind: for an app, what users type, paste, or click; for a utility, sample command lines; for a service, sample requests. Example: designers drag in `tokens-export (3).fig.json` with spaces in the name, or point at a folder instead of a file — the folder case must produce a clear error, not a stack trace.
4. **The mechanical verification** — the test command and observable checks → Success criteria and Verification commands. Example: `npm test` passes, and the CLI run on fixture A reproduces its golden output exactly. In a new repo with no toolchain yet, the first milestone establishes these as real commands — conventionally `make test` and `make run`, or the stack-native equivalent — recorded under Verification commands. When a verification step names a tool the repo itself doesn't provide (a local mail sink, a browser driver, a database client), confirm during the interview that it is installed, or mark that step owner-gated so the loop expects to pause there for the owner's go-ahead instead of stalling mid-milestone.
5. **What this repo deliberately won't do** → Non-goals. Example: no GUI, no Sketch support, no design-tool plugins.
6. **Which stack, platform, and dependency choices are fixed up front**, and which may be decided later through proposed ADRs → Constraints. Example: Node 20 is fixed; the token-storage format is decided via a proposed ADR.
7. **The first shippable slice and how to check it** → the first milestone. Example: parse one fixture and emit JSON, verified by the parser test suite passing.

Conduct rules:

- Push back on answers that can't be checked mechanically — "migrate the API to GraphQL" is a direction, not a done state. Keep asking until each answer would let you verify progress yourself.
- In an existing codebase, propose answers from the code first — detected test commands, structure, conventions — and let the owner correct them rather than asking cold.
- Draft the remaining milestones from the answers, ending by default with a README-quickstart milestone whose verification is reproducing the quickstart on a clean checkout; the owner may drop it.
- Confirm the finished `docs/GOAL.md` with the owner before starting the loop.
- Manual editing stays a valid alternative; never overwrite goal content the owner wrote by hand.
