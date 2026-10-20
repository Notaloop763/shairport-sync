# airplay-cast-bridge (Walter's shairport-sync fork)

This checkout is `Notaloop763/shairport-sync`, a fork of `mikebrady/shairport-sync` (remote
`upstream`). `cast-bridge/` is our AirPlay 2 → Google Cast bridge: `bridge.py`, its
`Dockerfile`, and `compose.yaml`, the Komodo stack that runs both the receiver and the bridge
on TrueNAS `192.168.77.89`.

## Branches

Every change goes on its own **topic branch**, and `bridge` is the **integration branch** that
merges them all:

- `master` and `development` track upstream; commits go on topic branches instead.
- `receiver/<topic>`: one change to the shairport-sync source, branched from
  `upstream/development` and changing only upstream's files, so it stays ready to become an
  upstream PR.
- `fork/<topic>`: one change to files only we carry (`cast-bridge/`, `CLAUDE.md`,
  `UPSTREAMING.md`), branched from `bridge`.
- `bridge`: `upstream/development` plus every topic, each merged with `git merge --no-ff`.
  Hermes builds and deploys from it. To pick up upstream changes, merge `upstream/development`
  into it.

The main checkout stays on `bridge`. Work on each topic in its own worktree:
`git worktree add .worktrees/<topic> -b <branch> <base>`.

A pull request, against `upstream` or `origin`, is opened only when Walter explicitly asks for
that one. Once he has, or when answering review on an upstream PR, follow `UPSTREAMING.md`.

## Roles

Claude owns code changes: commit on topic branches, merge them into `bridge`, and push
branches, `bridge` included, to `origin` as the work goes. `origin` is the only remote Claude
pushes to. Hermes (an agent on the NAS with read-only GitHub access) builds the images and
deploys them in Komodo from what's pushed. To change the live stack, write Hermes a deploy
request for Walter to relay: the commit to build, the image tag, the compose changes, and the
check to run afterwards.

## Safety

The Nest Hub is in Walter's bedroom. Keep `CAST_VOLUME_CAP` at `0.5` until Walter lifts it.
`cast-bridge/tools/hook_volume_test.py` changes the device volume, so run it only when Walter
asks. The other tools are read-only and only work on the home LAN.

## Resources

This PC is RAM-limited: keep total memory use under 10 GB during testing, Docker containers
and builds included.

## Git

Make one commit per logical change. The subject says what changed and the body says why; when
the change carries one of Walter's decisions, name it as his.
