# ENG-AO-13 — consistent structured result selection

Issue #52 identified two different interpretations of the same worker output. Typed denial detection walked every
line-anchored JSON value, while `structured_failure`, actual-model attribution, and adapter usage classification
only decoded an object at the start of the entire output. A diagnostic preamble could therefore hide a provider's
own error, cancellation, subtype, empty response, or usage record even though a denial in the same position was
still detected.

## Selection contract

`runner.first_structured_result` is now the single public selector for a worker's terminal result record. It reuses
the existing line-oriented value walker and applies this deterministic rule:

1. Decode only JSON values beginning a logical output line (or immediately following another decoded value).
2. From dictionaries carrying a recognized terminal field (`is_error`, `isError`, `result`, `response`, `text`,
   `stopReason`, or `subtype`), select the last one because transports may emit progress before the final outcome.
3. If no dictionary carries a terminal field, select the last decoded dictionary so a usage-only result remains
   observable.

JSON appearing later inside a prose line is never decoded as transport metadata. Diagnostic prose before, between,
or after records is ignored by selection. `structured_failure`, `structured_actual_model_report`, and
`adapter_contract.structural_usage_keys` now all consume this selector and cannot disagree about which record is the
result.

## Denial evidence remains separate

`worker_boundary_evidence` still scans every decoded structured value. This is intentional: permission denials may
be emitted in a separate usage record after the terminal result. Typed non-empty/empty denial fields retain their
existing `MEASURED` semantics, and prose remains `UNKNOWN`; result selection does not widen denial inference.

## Verification

Deterministic coverage includes preambled `is_error`/`isError`, `stopReason`, `subtype`, and empty-response records;
a result followed by diagnostic prose; multiple progress/stale/final records; actual-model and adapter usage reads;
denials in a second JSON object; and a JSON example embedded in model prose that must remain unparsed.
