# Documentation

Update maintained documentation directly owned by, or materially contradicted by, changed behavior or
contracts. Documentation drift is a defect, but unrelated roadmaps, ledgers, ADRs, release notes, and
handoffs need not be reread for a scoped change.

Repository code and maintained current documents override historical evidence. Do not rewrite historical
records merely because they cite an old path or workflow; mark a current document superseded when it would
otherwise remain contradictory. Keep one authoritative home per concern.

Version and release-note changes are required only for an explicit release, a maintained release/program
gate, or a genuine runtime/distribution version change. Normal feature, fix, and governance work does not
create release churn solely because it is user-visible or architectural.
