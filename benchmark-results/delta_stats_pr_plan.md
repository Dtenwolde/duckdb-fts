# Delta-Maintained FTS Stats PR Plan

## Goal

Replace global recomputation of the FTS `stats` table during incremental
`INSERT`/`DELETE` maintenance with delta updates.

This is intended as a small, low-risk PR before optimizing the larger hot spots
such as `dict.df` recomputation and repeated insert tokenization.

## Current Behavior

The FTS `stats` table currently stores:

| Column | Meaning |
|---|---|
| `num_docs` | Number of indexed documents. |
| `avgdl` | Average indexed document length. Used by BM25 length normalization. |

Initial build creates it with:

```sql
CREATE TABLE fts_schema.stats AS (
  SELECT
    COUNT(docs.docid) AS num_docs,
    SUM(docs.len) / COUNT(docs.len) AS avgdl
  FROM fts_schema.docs AS docs
);
```

Incremental insert/delete triggers currently recompute it globally:

```sql
UPDATE fts_schema.stats
SET num_docs = (SELECT COUNT(docid) FROM fts_schema.docs),
    avgdl = (SELECT SUM(len) / COUNT(len) FROM fts_schema.docs);
```

This scans all `docs` after every update statement.

## Proposed Change

Store enough state to update stats incrementally:

| Column | Meaning |
|---|---|
| `num_docs` | Number of indexed documents. |
| `total_doc_len` | Sum of `docs.len` across indexed documents. |
| `avgdl` | `total_doc_len / num_docs`. |

`match_bm25` can continue reading `num_docs` and `avgdl` exactly as it does
today. `total_doc_len` is internal maintenance state.

## Initial Build SQL

Change stats creation to:

```sql
CREATE TABLE fts_schema.stats AS (
  SELECT
    COUNT(docs.docid) AS num_docs,
    SUM(docs.len) AS total_doc_len,
    SUM(docs.len) / COUNT(docs.len) AS avgdl
  FROM fts_schema.docs AS docs
);
```

Consider whether empty indexes are possible. If yes, use:

```sql
CASE
  WHEN COUNT(docs.len) = 0 THEN NULL
  ELSE SUM(docs.len) / COUNT(docs.len)
END AS avgdl
```

## Insert Trigger SQL Shape

The stats trigger runs after inserted docs have been added to `fts_schema.docs`.

Use a CTE so `avgdl` is calculated from the new values explicitly:

```sql
WITH delta AS (
  SELECT
    COUNT(*) AS inserted_docs,
    COALESCE(SUM(d.len), 0) AS inserted_len
  FROM fts_schema.docs AS d
  JOIN fts_new_rows AS new_rows ON d.name = new_rows.id
)
UPDATE fts_schema.stats
SET
  num_docs = num_docs + delta.inserted_docs,
  total_doc_len = total_doc_len + delta.inserted_len,
  avgdl = CASE
    WHEN num_docs + delta.inserted_docs = 0 THEN NULL
    ELSE (total_doc_len + delta.inserted_len) / (num_docs + delta.inserted_docs)
  END
FROM delta;
```

In generated C++ SQL, replace `new_rows.id` with the configured document id
column placeholder.

## Delete Trigger SQL Shape

The delete-side stats update must see deleted document lengths before docs rows
are removed.

Preferred trigger order:

1. Delete terms for old docs.
2. Delta-update stats using existing `docs.len`.
3. Delete docs.
4. Recompute/decrement/prune dict metadata.

SQL shape:

```sql
WITH delta AS (
  SELECT
    COUNT(*) AS deleted_docs,
    COALESCE(SUM(d.len), 0) AS deleted_len
  FROM fts_schema.docs AS d
  JOIN fts_old_rows AS old_rows ON d.name = old_rows.id
)
UPDATE fts_schema.stats
SET
  num_docs = num_docs - delta.deleted_docs,
  total_doc_len = total_doc_len - delta.deleted_len,
  avgdl = CASE
    WHEN num_docs - delta.deleted_docs = 0 THEN NULL
    ELSE (total_doc_len - delta.deleted_len) / (num_docs - delta.deleted_docs)
  END
FROM delta;
```

Then delete rows from `fts_schema.docs`.

Again, replace `old_rows.id` with the configured document id column placeholder.

## Implementation Steps

1. Update `IndexTablesScript` in `src/fts_indexing.cpp`:
   - Add `total_doc_len` to `stats`.
   - Preserve existing `num_docs` and `avgdl` column names.

2. Update insert stats trigger in `InsertTriggerScript`:
   - Replace full `COUNT`/`SUM` scan over all docs with a delta CTE joined
     against `fts_new_rows`.

3. Update delete trigger ordering in `DeleteTriggerScript`:
   - Move stats maintenance before deleting from `docs`.
   - Replace full `COUNT`/`SUM` scan over all docs with a delta CTE joined
     against `fts_old_rows`.
   - Rename trigger numbering if needed to preserve execution order.

4. Review any tests or assertions that expect exact stats schema shape:
   - Existing tests query `num_docs, avgdl`, so they should continue to work.
   - If tests use `SELECT * FROM stats`, update expected output for the new
     `total_doc_len` column or avoid `SELECT *`.

5. Run focused tests:

```sh
make unittest_reldebug
```

and targeted SQL tests if available.

6. Run a small benchmark/profile sanity check:

```sh
python3 scripts/fts_trigger_profile.py \
  --data benchmark-results/datagen-100k/items.jsonl \
  --batch-size 1 \
  --operation insert \
  --statement-timeout 900 \
  --recreate-db
```

Then compare `insert_40_stats` / `delete_40_stats` timing before and after.

## Correctness Checks

For each test case, compare delta-maintained stats against a full recompute:

```sql
SELECT
  stats.num_docs,
  stats.avgdl,
  recomputed.num_docs,
  recomputed.avgdl
FROM fts_schema.stats AS stats,
(
  SELECT
    COUNT(*) AS num_docs,
    SUM(len) / COUNT(len) AS avgdl
  FROM fts_schema.docs
) AS recomputed;
```

Recommended cases:

- Insert one document.
- Insert multiple documents in one statement.
- Delete one document.
- Delete multiple documents in one statement.
- Mixed delete+insert transaction.
- Delete all documents if this is a supported state.

## Expected Performance Impact

This is not expected to be the biggest benchmark win.

The trigger profile showed stats recomputation was small in sampled runs:

| Scenario | Stats component |
|---|---:|
| 100K insert 1 | ~0.004 s |
| 500K mixed 500+500 insert-side stats | ~0.002 s |
| 500K mixed 500+500 delete-side stats | ~0.002 s |

Still, this change is useful because:

- It removes one global scan from every incremental update.
- It establishes the delta-maintenance pattern needed for `dict.df`.
- It is easy to reason about and test.
- It reduces the amount of full-index work inside the trigger path.

## Risks / Open Questions

- Confirm trigger execution order after renumbering delete triggers.
- Decide `avgdl` behavior when `num_docs = 0`.
- Confirm `SUM(len)` type and division behavior remain compatible with current
  `avgdl` results.
- Verify no external code assumes `stats` has exactly two columns.

## Follow-Up PRs

After delta stats:

1. Delta-maintain `dict.df` for affected terms only.
2. Avoid repeated tokenization on insert.
3. Test helper indexes such as `dict(term)` and `terms(docid)`.
4. Add more targeted mixed delete+insert profiling.
