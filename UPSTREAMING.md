# Upstreaming receiver patches

How to turn a `receiver/<topic>` branch into a pull request to `mikebrady/shairport-sync`,
once Walter has said to open it.
The rules come from upstream's PR template plus a survey of every PR closed upstream from
2026-04-17 to 2026-10-17; the PR numbers cited are the evidence. Upstream has one reviewer,
Mike Brady, and he merges a **focused** PR in 0–3 days. When he turns a PR down, the reason
has been its scope or the maintenance it adds, never its line count. If a rule here disagrees
with what upstream does now, re-survey the last 30 days of closed PRs and update this file.

## Steps

1. **Check the topic is one concern.** One bug or one feature per PR, with nothing else riding
   along: no nits, no reformatting, nothing from `cast-bridge/`. Done when you can state the
   change in a one-line PR title without "and". If the topic carries two concerns, split it
   into two topic branches first (#2309–2311 each merged the same day; the bundled #2173 died
   while a focused #2297 merged in a day).
2. **Bring the topic up to date with `upstream/development`.** Run `git fetch upstream` and
   rebase the topic onto `upstream/development` if upstream has moved. Done when
   `git log upstream/development..receiver/<topic>` shows only the topic's commits. A branch
   cut from `master` or a release tag reverts unreleased upstream work when the PR is
   retargeted (#2217).
3. **Build and test on standard Linux.** Build with the flags you'll report and run
   `make check`. Upstream's CI builds both classic AirPlay and AirPlay 2, so build both. Done
   when both build with no new warnings, `make check` passes, and you've reproduced the bug
   before the change and seen it fixed after, on a real device where the bug involves one.
4. **Generalise.** If the change touches one codec, transport or platform, check whether its
   siblings (AAC/ALAC, AirPlay 1/2, Linux/BSD/macOS) need the same change. Either cover them
   or say in the PR why they don't apply. Mike asks this every time (#2305).
5. **Write the PR body in upstream's template** (`.github/PULL_REQUEST_TEMPLATE.md`), with
   every section filled in or deleted. The description carries the **root cause**: the
   mechanism, the exact numbers (e.g. "overflows at 2^21 packets, 12 h 25 min at 48 kHz"),
   and a reference for any claim about a library's behaviour (#2298 answered Mike's doubt
   with links to Apple's libpthread source). Under Testing, give the hardware, OS, build flags,
   the reproduction steps and the before/after results (#2315 is the model). Link the upstream
   issue if there is one.
6. **Show Walter, then open the PR.** Opening it publishes under Walter's account, so show
   him the final branch and PR body first. Push `receiver/<topic>` to `origin` and open the
   PR against base `development`.

## After opening

- Answer every review comment within a few days. Upstream's stale bot labels a PR after 28
  days without activity and closes it 7 days later.
- When Mike proposes another way to fix the problem (#2307), check whether it really works in
  the current code, and answer with evidence for or against.
- When he asks for part of the change only, open a new, smaller PR for that part rather than
  arguing for the whole (#2173).

## What upstream declines

- **New backends or platforms** that existing pieces can already cover. Upstream keeps the
  codebase small so it can change AirPlay handling quickly, and has been deprecating backends.
  A VBAN backend was declined because the `pipe`/`stdout` backends can already feed a VBAN
  sender (#2228). A Windows port was declined for its scope and maintenance load (#2227).
- **Experimental changes to NQPTP**, or conditional compilation added to support them (#2173).
- **Anything that breaks the standard Linux build** (#2227).

AI-assisted PRs are accepted when the change is small and clean. On a sprawling PR, Mike took
the AI disclaimer as a sign that a much simpler approach existed (#2227).
