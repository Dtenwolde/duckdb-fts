# Reducing Repeated Tokenization in Incremental FTS Insert Triggers

## Goal

Reduce incremental `INSERT` latency by avoiding repeated tokenization of the
same newly inserted rows during trigger-maintained FTS index updates.

This is a separate optimization from delta-maintained `stats` and
delta-maintained `dict.df`. It targets the insert side of the trigger path,
especially larger insert batches and mixed delete+insert workloads.

## Current Behavior

The insert trigger path effectively tokenizes inserted rows multiple times:

1. Insert into `docs` and compute document length.
2. Discover new dictionary terms.
3. Insert postings into `terms`.
4. Recompute or update dictionary document frequency.
5. Update statistics.

The expensive part is that `docs.len`, dictionary discovery, and postings
insertion each need tokenized text. Today these are separate trigger statements,
so a token stream produced in one statement cannot be reused by the next one.

## Why Optimize This

Trigger profiling showed the insert-side document insertion / length path can
dominate larger mixed updates:

| Scenario | Component | Time / Share |
|---|---|---:|
| 500K mixed 500+500 | `insert_00_docs` | 4.297 s / ~81% of component sum |

The component profile is investigative rather than perfectly additive, but it
strongly suggests that insert-side tokenization and document-length computation
deserve attention after the lower-risk delta stats and `dict.df` fixes.

This optimization should help:

- insert batches
- mixed delete+insert updates
- generated larger tenants
- tail latency for write-heavy tenants

It should not materially help delete-only workloads.

## Main Constraint

DuckDB trigger actions are separate SQL statements. A CTE or intermediate
tokenized result from one trigger statement is not naturally reusable by another
statement.

That makes the ideal shape difficult:

```text
tokenize new rows once
reuse token stream for docs.len, dict insert, terms insert, and df delta
```

The practical first PR should avoid introducing complex shared staging state
inside triggers unless profiling proves it is necessary.

## Proposed First Step: Derive `docs.len` From Inserted Postings

Instead of tokenizing once just to compute `docs.len`, insert the new docs with a
temporary length and fill the length after postings have been inserted.

Proposed insert order:

1. Insert new docs with `len = 0`.
2. Insert new dictionary rows.
3. Insert new postings into `terms`.
4. Update `docs.len` for only inserted docs by counting inserted postings.
5. Apply delta `dict.df`.
6. Update stats.

Sketch:

```sql
UPDATE fts_schema.docs AS d
SET len = (
  SELECT count(*)
  FROM fts_schema.terms AS t
  WHERE t.docid = d.docid
)
WHERE d.name IN (
  SELECT new_rows.id
  FROM fts_new_rows AS new_rows
);
```

In generated C++ SQL, replace `d.name` and `new_rows.id` with the configured
document id column placeholders.

This does not eliminate all repeated tokenization, because dictionary discovery
and postings insertion still tokenize. It removes one full tokenization pass
from the profiled `insert_00_docs` component.

## Interaction With Delta Stats

If delta-maintained stats are implemented, stats must run after `docs.len` has
been filled.

For inserted docs, the stats delta should use the final length values:

```sql
SELECT count(*) AS inserted_docs, sum(len) AS inserted_len
FROM fts_schema.docs AS d
JOIN fts_new_rows AS new_rows ON d.name = new_rows.id;
```

If stats are still globally recomputed, the same ordering still matters because
`avgdl` depends on `docs.len`.

## Interaction With Delta `dict.df`

This optimization combines naturally with delta-maintained `dict.df`:

```text
insert docs with len=0
insert dict rows
insert terms
fill docs.len from terms
increment dict.df from affected terms
update stats
```

Recommendation: implement and validate delta `dict.df` first, then make this
change as a follow-up PR. Both changes touch insert trigger ordering, so keeping
them separate makes correctness and performance attribution clearer.

## Alternative: Materialize Inserted Tokens

A larger optimization would materialize a per-trigger token table, for example:

```text
inserted_tokens(docid, fieldid, term)
```

Then reuse it for:

- document length
- dictionary insertion
- postings insertion
- dictionary document-frequency deltas

This is closer to the ideal shape, but it is riskier because it needs careful
answers for:

- where the staging table lives
- cleanup after failures
- concurrent writes
- nested or repeated trigger execution
- schema naming and lifecycle

Recommendation: do not start here. Use this only if the simpler `docs.len`
change leaves repeated tokenization as a major bottleneck.

## Correctness Requirements

The optimized insert path must preserve:

- identical `docs` rows and final `len` values
- identical `terms` postings
- identical `dict.term` and `dict.df`
- identical `stats.num_docs` and `stats.avgdl`
- identical search results for representative queries

The `documentid` column must still be `NOT NULL`, as required by the updatable
FTS trigger design.

## Validation Plan

1. Add focused SQL or unit coverage for insert triggers:
   - single inserted row
   - multiple inserted rows
   - repeated term in one document
   - multiple indexed fields
   - inserted row with empty or stopword-only text

2. Compare against a full rebuild:
   - build FTS index
   - apply insert batch through triggers
   - separately rebuild index from the same final source table
   - compare `docs`, `terms`, `dict`, and `stats`

3. Run existing update benchmark scenarios:
   - 100K insert 1
   - 100K mixed 500+500
   - 500K insert 1
   - 500K mixed 500+500

4. Re-run trigger component profiling:
   - confirm `insert_00_docs` drops materially
   - check whether dictionary discovery or postings insertion becomes the next
     dominant insert-side component

5. Re-run tail-latency spot checks for incremental only:
   - 100K insert 1
   - 500K insert 1
   - 500K mixed 500+500

## Expected Result

This should reduce insert-side latency, but it is not expected to remove all
insert cost. After the change, the likely remaining hot spots are:

- dictionary discovery tokenization
- postings insertion tokenization
- joins from inserted docs to `terms`
- any remaining global maintenance work not replaced by deltas

The useful success criterion is not that inserts become free; it is that the
trigger path stops tokenizing once solely to compute document length.
