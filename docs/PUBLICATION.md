# Publication

`ACGE248/octarel` starts private. Issue #175 is the only authorization to
make it public, and only after every gate below is green on one exact SHA.

## Why `main` cannot be published as-is

- GitHub merge commits record a private author email.
- An earlier ENG-CP-05 blob recorded an operator filesystem path, later
  removed from `HEAD` but still reachable in the old object store.
- Merged pull-request refs on GitHub would expose that history even after a
  force-push of `main`.

Do **not** change GitHub visibility while that history is reachable from any
public ref, pull request, tag, or fork.

## Public-safe snapshot

```bash
python3 scripts/ci/prepare_public_history.py \
  --source . \
  --destination /path/to/octarel-public-safe
python3 scripts/ci/public_safety.py --root /path/to/octarel-public-safe --publication-gate
```

The script creates a **new `git init`** and imports only `git archive HEAD`.
It does not clone the unsafe object store. Attribution is
`Octarel contributors <octarel@users.noreply.github.com>`. It never pushes.

## Private history preservation

Keep the pre-public object store **off the public repository**:

1. A local `git bundle --all` outside this checkout.
2. A separate **private** GitHub repository (for example
   `ACGE248/octarel-pre-public`) that is never made public.

Do not push `archive/pre-public-history` to the repository that will become
public.

Because GitHub pull requests remain visible after a force-push, the
publication step **replaces** `ACGE248/octarel` with a new repository that
contains only the public-safe commit (rename the current private repo aside,
then create a new `ACGE248/octarel` from the snapshot). The old repo stays
private.

## Gates

1. Current tree public-safety (`--tree-only` green).
2. Publication history public-safety (`--publication-gate` green on the
   snapshot that will actually be published, including `git log --all` and
   `git rev-list --all`).
3. LICENSE / NOTICE / THIRD_PARTY.md.
4. Clean-room install from the snapshot.
5. Multi-repository acceptance (OctaScene + generic).
6. Control Center browser audit (full viewport matrix for this release).
7. Exact-tree local gate on the snapshot.
8. Independent review READY from a non-implementer provider on that SHA.
9. README, description, `octarel.example.toml`, and any public-visible
   issues/PRs/releases contain no private material.

If any item fails or is uncertain, **keep the repository private**.
