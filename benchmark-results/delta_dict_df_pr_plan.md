# Delta-Maintained FTS Dictionary Document Frequency PR Plan

## Goal

Replace global recomputation of `dict.df` during incremental `INSERT`/`DELETE`
maintenance with delta updates over only the affected terms.

This should be a higher-impact optimization than delta-maintained `stats`, but
it is also more correctness-sensitive because BM25 scoring depends directly on
accurate document frequencies.

## What `df` Means

`dict.df` stores document frequency:

```text
df(term) = number of distinct indexed documents containing that term
```

This is not token frequency. If one document contains the same term 20 times,
that document contributes `1` to `df`.

BM25 uses `df` in inverse-document-frequency scoring:

```text
log((num_docs - df + 0.5) / (df + 0.5) + 1)
```

## Current Behavior

After insert/delete, the trigger currently recomputes `df` globally:

```sql
UPDATE fts_schema.dict AS d
SET df = (
  SELECT count(DISTINCT docid)
  FROM fts_schema.terms AS t
  WHERE d.termid = t.termid
);
```

This means a one-row update can still scan the whole postings table and touch
the whole dictionary.

## Why Optimize This

Trigger profiling showed `dict.df` recomputation is a major hot spot:

| Scenario | Component | Time / Share |
|---|---|---:|
| 100K insert 1 | `insert_30_dict_df` | 0.077 s / ~69% |
| 500K mixed 500+500 | insert + delete `dict_df` | ~0.82 s in no-profile component run |

This should improve:

- single-row inserts
- single-row deletes
- mixed delete+insert updates
- tail latency variance from global recomputation

It will not solve inserted-row tokenization/document-length cost. That remains a
separate optimization target.

## Proposed Insert Delta

Insert order should be:

1. Insert new rows into `docs`.
2. Insert new dictionary rows with `df = 0`.
3. Insert new postings into `terms`.
4. Increment `df` only for terms present in inserted documents.
5. Update stats.

After new postings exist, compute inserted document-frequency deltas:

```sql
WITH inserted_df_delta AS (
  SELECT
    t.termid,
    COUNT(DISTINCT t.docid) AS df_delta
  FROM fts_schema.terms AS t
  JOIN fts_schema.docs AS d ON t.docid = d.docid
  JOIN fts_new_rows AS new_rows ON d.name = new_rows.id
  GROUP BY t.termid
)
UPDATE fts_schema.dict AS d
SET df = d.df + inserted_df_delta.df_delta
FROM inserted_df_delta
WHERE d.termid = inserted_df_delta.termid;
```

In generated C++ SQL, replace `new_rows.id` with the configured document id
column placeholder.

Important correctness detail:

```sql
COUNT(DISTINCT t.docid)
```

is required so repeated terms inside one document increment `df` only once.

## Proposed Delete Delta

Delete order should change because we need old postings before they are removed:

1. Compute/decrement `df` for terms in deleted documents.
2. Delete postings from `terms`.
3. Delete docs from `docs`.
4. Prune zero-`df` dictionary rows.
5. Update stats.

Before deleting postings:

```sql
WITH deleted_df_delta AS (
  SELECT
    t.termid,
    COUNT(DISTINCT t.docid) AS df_delta
  FROM fts_schema.terms AS t
  JOIN fts_schema.docs AS d ON t.docid = d.docid
  JOIN fts_old_rows AS old_rows ON d.name = old_rows.id
  GROUP BY t.termid
)
UPDATE fts_schema.dict AS d
SET df = d.df - deleted_df_delta.df_delta
FROM deleted_df_delta
WHERE d.termid = deleted_df_delta.termid;
```

Then delete postings and docs.

## Dictionary Pruning

After delete-side `df` decrement, terms with `df = 0` should be pruned.

Simplest first version:

```sql
DELETE FROM fts_schema.dict
WHERE df = 0;
```

This may scan the dictionary, but it is much smaller than scanning all postings
for every term. It is acceptable as a first PR unless profiling shows it becomes
costly.

More targeted version:

```sql
DELETE FROM fts_schema.dict
WHERE df = 0
  AND termid IN (SELECT termid FROM deleted_df_delta);
```

The targeted version is harder because CTE state does not cross trigger
statements. Options:

1. Repeat the affected-term CTE in the prune trigger.
2. Combine decrement and prune into a single statement if DuckDB SQL supports a
   clean shape.
3. Use an internal temporary affected-term table, though this is more complex
   inside trigger definitions.

Recommendation: start with `DELETE FROM dict WHERE df = 0`, profile it, then
target pruning only if needed.

## Trigger Ordering Changes

Current insert order is already close:

```text
insert docs
insert dict rows
insert terms
recompute dict df
update stats
```

Replace the recompute step with delta increment.

Current delete order must change:

```text
delete terms
delete docs
recompute dict df
prune dict
update stats
```

Proposed delete order:

```text
decrement dict df
delete terms
delete docs
prune dict
update stats
```

If delta stats is implemented first, the stats update must also happen before
docs deletion or otherwise preserve deleted document lengths.

## Implementation Steps

1. Update `InsertTriggerScript` in `src/fts_indexing.cpp`:
   - Replace `%trigger_30_dict_df%` SQL with affected-term delta increment.
   - Keep new dictionary rows initialized with `df = 0`.

2. Update `DeleteTriggerScript` in `src/fts_indexing.cpp`:
   - Add a new first delete-side trigger that decrements `df` using old docs.
   - Move term/doc deletion after `df` decrement.
   - Keep dictionary prune after decrement.
   - Renumber trigger names if needed to preserve order.

3. Verify trigger naming helpers:
   - `GetFTSTriggerNames`
   - `GetFTSDeleteTriggerNames`

4. Run existing incremental insert/delete SQL tests.

5. Add targeted document-frequency tests.

6. Run profile comparison:

```sh
python3 scripts/fts_trigger_profile.py \
  --data benchmark-results/datagen-100k/items.jsonl \
  --batch-size 1 \
  --operation insert \
  --statement-timeout 900 \
  --recreate-db
```

and:

```sh
python3 scripts/fts_trigger_profile.py \
  --data benchmark-results/datagen-500k/items.jsonl \
  --batch-size 500 \
  --operation mixed \
  --statement-timeout 900 \
  --recreate-db
```

Compare old/new `insert_30_dict_df` and `delete_20_dict_df` timings.

## Correctness Tests

Add tests that catch duplicate-token mistakes.

Example data:

```text
doc1: "hello hello hello"
doc2: "hello world"
```

Expected:

```text
df(hello) = 2
df(world) = 1
```

Recommended cases:

- Insert one document with a repeated term.
- Insert multiple documents that share a term.
- Delete one document with a repeated term.
- Delete multiple documents that share a term.
- Delete the last document containing a term and verify that term is pruned.
- Mixed delete+insert transaction.
- Logical update pattern: delete a document and insert the replacement document.

After each mutation, compare maintained `df` against full recomputation:

```sql
WITH recomputed AS (
  SELECT termid, count(DISTINCT docid) AS df
  FROM fts_schema.terms
  GROUP BY termid
),
diffs AS (
  SELECT 'dict-minus-recomputed' AS side, *
  FROM (
    SELECT termid, df FROM fts_schema.dict
    EXCEPT
    SELECT termid, df FROM recomputed
  )
  UNION ALL
  SELECT 'recomputed-minus-dict' AS side, *
  FROM (
    SELECT termid, df FROM recomputed
    EXCEPT
    SELECT termid, df FROM fts_schema.dict
  )
)
SELECT *
FROM diffs;
```

Expected: no rows.

Also verify query scores are unchanged relative to full rebuild for representative
queries.

## Expected Performance Impact

This should matter more than delta stats.

Expected improvements:

- Single-row insert/delete should improve substantially because global `df`
  recomputation was dominant for 100K insert 1.
- Mixed updates should improve, though 500K mixed will still have inserted-row
  tokenization/document-length cost.
- Tail latency should improve by removing a source of whole-index work.

Do not expect this PR alone to solve all 500K mixed variance.

## Risks / Open Questions

- Correctly handling repeated terms inside one document.
- Correctly handling multi-row statements where several documents contain the
  same term.
- Trigger execution order after renumbering.
- Dictionary pruning strategy: global `df = 0` prune first, or affected-term
  prune immediately.
- Interaction with future delta-stats trigger ordering.
- Whether `dict.df` can become negative due to ordering or duplicate old rows;
  tests should guard against this.

## Follow-Up PRs

After delta `dict.df`:

1. Avoid repeated insert tokenization / document-length computation.
2. Benchmark helper indexes such as `dict(term)` and `terms(docid)`.
3. Add mixed update path profiling for customer-style delete+insert logical
   updates.
