# Development storage

Octarel derives a shared development-storage root from each managed project's
canonical Git directory. When OctaScene and Octarel are installed beneath the
same external-volume project directory, this resolves to the sibling directory
`.octa-dev-storage`. An operator may set the absolute
`OCTA_DEV_STORAGE_ROOT` override for another installation.

Worker launches, delegated commands, managed-project validation, direct local
gates, and pytest sessions use that root for temporary directories and
XDG/npm/pip/Python/Playwright caches. Before use, Octarel requires at least
20 GiB free and proves directory creation, atomic rename, hard links, SQLite
WAL mode, and SQLite integrity. It fails closed instead of silently falling
back to the system volume.

Evidence stays in the existing worktree-local `.agent-output`, `.local-gate`,
and `test-results` paths. Supported new worktrees remain siblings of the
managed repository, which already places them on the external volume. Existing
repositories, worktrees, databases, virtual environments, and evidence are
not relocated.
