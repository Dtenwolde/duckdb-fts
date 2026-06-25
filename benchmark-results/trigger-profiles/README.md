# FTS Trigger Profile Runs

This directory stores first-pass trigger maintenance profiles produced by:

```sh
python3 scripts/fts_trigger_profile.py \
  --data benchmark-results/datagen-500k/items.jsonl \
  --batch-size 500 \
  --operation mixed \
  --statement-timeout 900 \
  --recreate-db
```

The profiler rebuilds one indexed source table, creates `fts_new_rows` and
`fts_old_rows` batches, and executes trigger-equivalent maintenance statements
one by one. It records wall-clock component timing in
`fts_trigger_profile_results.csv` and writes DuckDB JSON profiles under
`profiles/<run_id>/`.

## Current First-Pass Results

100K insert 1:

| Component | Time | Share |
|---|---:|---:|
| `insert_00_docs` | 0.010 s | 9.2% |
| `insert_10_dict_insert` | 0.010 s | 9.1% |
| `insert_20_terms` | 0.011 s | 9.9% |
| `insert_30_dict_df` | 0.077 s | 68.7% |
| `insert_40_stats` | 0.004 s | 3.2% |

500K mixed 500+500, no JSON profiling:

| Component | Time | Share |
|---|---:|---:|
| `insert_00_docs` | 4.297 s | 80.9% |
| `insert_10_dict_insert` | 0.080 s | 1.5% |
| `insert_20_terms` | 0.084 s | 1.6% |
| `insert_30_dict_df` | 0.465 s | 8.7% |
| `insert_40_stats` | 0.002 s | 0.0% |
| `delete_00_terms` | 0.007 s | 0.1% |
| `delete_10_docs` | 0.015 s | 0.3% |
| `delete_20_dict_df` | 0.357 s | 6.7% |
| `delete_30_dict_prune` | 0.001 s | 0.0% |
| `delete_40_stats` | 0.002 s | 0.0% |

## Initial Interpretation

- `dict.df` recomputation is a clear hot spot for small updates.
- 500K mixed profiling points at `insert_00_docs`, which computes inserted
  document lengths by tokenizing the inserted batch, plus `dict.df`
  recomputation.
- The component reproduction is investigative, not a perfect additive model of
  trigger execution. A one-shot actual 500K mixed 500+500 trigger benchmark in
  the same session measured 3.24 s, while component reproduction summed to
  5.31 s. Treat the component ranking as guidance for optimization targets, not
  as exact end-to-end attribution.

## Optimization Hypotheses

- Replace global `dict.df` recomputation with delta updates for affected terms.
- Replace global `stats` recomputation with delta maintenance of `num_docs` and
  total document length.
- Avoid repeated tokenization of inserted rows across insert-side maintenance
  statements.
- Test helper indexes such as `dict(term)` and `terms(docid)` and measure the
  maintenance tradeoff.
