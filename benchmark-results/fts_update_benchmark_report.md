# FTS Incremental Update Benchmark

## Summary

This benchmark compares the trigger-maintained incremental FTS index against the
default non-incremental FTS index on monday item data.

The result is positive for freshness-style updates. Incremental maintenance is
consistently faster for small and medium deltas, especially deletes. The effect
becomes much stronger as tenant size grows: on the generated 100K-document
tenant, every tested update completed incrementally in under half a second,
while full rebuild paid roughly seven seconds per update. On the generated
500K-document tenant, full rebuild rose to roughly 12-14 seconds per update,
while incremental updates stayed below 2.7 seconds for all tested cases.

At a high level:

- Initial index creation can be slightly slower with `incremental=true`,
  because it also creates trigger/index maintenance machinery. On the generated
  100K and 500K tenants, the difference was negligible.
- Full rebuild time is mostly tied to tenant size and stays almost flat across
  update batch sizes.
- Incremental update time scales with changed rows.
- Deletes are the strongest case for incremental maintenance.
- For the smallest tenant, large mixed batches can make rebuild competitive or
  faster because rebuilding 626 documents is already cheap.

## What Was Tested

Source data:

`/Users/danieltenwolde/git/duckdb-monday/search-engine/search-evaluation/evaluation-data/data-sets/master/items.json`

The benchmark normalizes items into:

```sql
id VARCHAR NOT NULL,
account_id BIGINT,
board_id BIGINT,
body VARCHAR
```

`body` is built from searchable item fields:

- `name`
- `description`
- `board_name`
- `group_name`
- joined `column_values_index`

Tenant slices:

| Tenant | Documents |
|---|---:|
| `account_31245546` | 626 |
| `account_28132365` | 2,689 |
| `combined` | 3,315 |

Operations:

| Operation | Meaning |
|---|---|
| `build_index` | Create the FTS index from the base tenant table. |
| `insert` | Insert N new documents. |
| `delete` | Delete N existing documents. |
| `mixed` | Insert N documents and delete N documents in one transaction. |

Batch sizes:

`1`, `10`, `100`, `500`

Runs:

Each scenario was run 5 times. Tables below report median wall-clock time.

## Generated Dataset Support

The benchmark also supports JSONL produced by
`/Users/danieltenwolde/git/duckdb-monday/search-engine/data-generator`.

Generate an explicit 100K tenant:

```sh
node scripts/generate_datagen_dataset.mjs \
  --account-sizes 100000 \
  --output-dir benchmark-results/datagen-100k
```

Generate multiple explicit tenant sizes later:

```sh
node scripts/generate_datagen_dataset.mjs \
  --account-sizes 100000,500000,1000000 \
  --output-dir benchmark-results/datagen-mega-laptop
```

Generate with the data-generator's native scale-factor distribution:

```sh
node scripts/generate_datagen_dataset.mjs \
  --scale-factor 0.01 \
  --output-dir benchmark-results/datagen-sf-0.01
```

Run the benchmark over generated JSONL:

```sh
python3 scripts/fts_update_benchmark.py \
  --dataset-kind datagen-jsonl \
  --data benchmark-results/datagen-100k/items.jsonl \
  --tenants account_900001 \
  --batch-sizes 1,10,100,500 \
  --runs 5 \
  --statement-timeout 300 \
  --recreate-db \
  --clear-results
```

For generated JSONL, tenant labels are exposed as `account_<id>`, plus
`combined` across all generated rows.

The 500K generated run used the same benchmark command shape with
`--account-sizes 500000`, `--runs 3`, and `--statement-timeout 900`.

## Compared Approaches

### Incremental

The table starts with:

```sql
PRAGMA create_fts_index('table', 'id', 'body', incremental=true, ...);
```

The timed update is only the source-table mutation. FTS maintenance is paid
through the insert/delete triggers.

### Default Rebuild

The table starts with the default FTS index:

```sql
PRAGMA create_fts_index('table', 'id', 'body', ...);
```

For update scenarios, the timed operation is:

1. apply the source-table mutation
2. rebuild the whole FTS index with `overwrite=true`

This is the fair default baseline because the non-incremental index does not
stay fresh after source-table mutations.

## Correctness Sanity Check

The benchmark timing loop does not compare internal FTS tables on every timed
scenario. Repeated diagnostic reads of internal FTS tables can hang in the
current build, so correctness checks are kept separate from timing.

A separate equality check was run with:

```sh
python3 -u scripts/fts_update_benchmark.py --equality-checks --skip-benchmarks --statement-timeout 30
```

It builds a default index and an incremental index from the same source state,
applies the same mutation, rebuilds only the default index, and compares:

- source rows
- normalized `docs`
- expanded term frequencies
- `dict`
- `stats`

All 36 checks passed:

- 3 tenants
- 3 update operations
- 4 batch sizes

The generated 100K and 500K benchmark runs below were timed benchmarks only.
They did not include generated-data equality sweeps yet.

## Results

Times are medians in milliseconds. Speedup is:

```text
full rebuild time / incremental time
```

Values greater than `1.0x` favor incremental maintenance.

### Combined Tenant: 3,315 Documents

| Operation | Batch | Full rebuild | Incremental | Speedup |
|---|---:|---:|---:|---:|
| build index | - | 112.2 ms | 128.7 ms | 0.87x |
| insert | 1 | 114.7 ms | 21.4 ms | 5.36x |
| insert | 10 | 115.3 ms | 18.9 ms | 6.10x |
| insert | 100 | 119.4 ms | 25.4 ms | 4.71x |
| insert | 500 | 124.1 ms | 45.2 ms | 2.75x |
| delete | 1 | 113.7 ms | 9.1 ms | 12.52x |
| delete | 10 | 112.4 ms | 10.0 ms | 11.28x |
| delete | 100 | 111.2 ms | 10.4 ms | 10.68x |
| delete | 500 | 106.7 ms | 11.3 ms | 9.43x |
| mixed | 1 + 1 | 117.3 ms | 29.1 ms | 4.04x |
| mixed | 10 + 10 | 117.8 ms | 29.8 ms | 3.95x |
| mixed | 100 + 100 | 119.3 ms | 35.4 ms | 3.37x |
| mixed | 500 + 500 | 121.4 ms | 57.9 ms | 2.10x |

This is the clearest overall result. The default rebuild path stays around
110-125 ms regardless of update size. Incremental maintenance stays much lower,
with speedups from 2.1x to 12.5x depending on operation and batch size.

### Larger Single Tenant: 2,689 Documents

| Operation | Batch | Full rebuild | Incremental | Speedup |
|---|---:|---:|---:|---:|
| build index | - | 100.6 ms | 114.2 ms | 0.88x |
| insert | 1 | 102.6 ms | 16.4 ms | 6.25x |
| insert | 10 | 100.2 ms | 17.2 ms | 5.83x |
| insert | 100 | 105.6 ms | 25.0 ms | 4.23x |
| insert | 500 | 114.1 ms | 51.0 ms | 2.24x |
| delete | 1 | 100.1 ms | 7.7 ms | 12.96x |
| delete | 10 | 100.7 ms | 8.9 ms | 11.29x |
| delete | 100 | 99.8 ms | 9.6 ms | 10.35x |
| delete | 500 | 90.0 ms | 10.2 ms | 8.78x |
| mixed | 1 + 1 | 102.4 ms | 24.0 ms | 4.26x |
| mixed | 10 + 10 | 101.1 ms | 26.1 ms | 3.87x |
| mixed | 100 + 100 | 103.4 ms | 34.7 ms | 2.98x |
| mixed | 500 + 500 | 105.3 ms | 63.7 ms | 1.65x |

This tenant shows the same shape as the combined tenant. Deletes are almost
flat for incremental maintenance, while full rebuild remains around 90-105 ms.
Large mixed batches narrow the gap, but incremental still wins.

### Smaller Single Tenant: 626 Documents

| Operation | Batch | Full rebuild | Incremental | Speedup |
|---|---:|---:|---:|---:|
| build index | - | 35.7 ms | 48.8 ms | 0.73x |
| insert | 1 | 36.4 ms | 13.8 ms | 2.64x |
| insert | 10 | 38.1 ms | 15.4 ms | 2.47x |
| insert | 100 | 38.8 ms | 20.3 ms | 1.91x |
| insert | 500 | 47.2 ms | 42.0 ms | 1.12x |
| delete | 1 | 36.1 ms | 5.3 ms | 6.82x |
| delete | 10 | 35.4 ms | 6.3 ms | 5.60x |
| delete | 100 | 33.8 ms | 6.0 ms | 5.64x |
| delete | 500 | 27.9 ms | 6.5 ms | 4.30x |
| mixed | 1 + 1 | 37.7 ms | 18.5 ms | 2.04x |
| mixed | 10 + 10 | 37.7 ms | 20.3 ms | 1.86x |
| mixed | 100 + 100 | 37.9 ms | 26.2 ms | 1.45x |
| mixed | 500 + 500 | 39.5 ms | 48.9 ms | 0.81x |

For very small tenants, the rebuild baseline is already cheap. Incremental still
wins for most update patterns, especially deletes, but the advantage narrows as
batch size approaches the tenant size. The `mixed` 500 + 500 case is the one
case where full rebuild wins.

### Generated Tenant: 100,000 Documents

This run used a generated single tenant with:

- `account_900001`
- 100,000 documents
- 38 boards
- 137.8 MiB `items.jsonl`

Times are medians in milliseconds.

| Operation | Batch | Full rebuild | Incremental | Speedup |
|---|---:|---:|---:|---:|
| build index | - | 7,400.6 ms | 7,486.4 ms | 0.99x |
| insert | 1 | 7,126.2 ms | 107.6 ms | 66.22x |
| insert | 10 | 7,018.1 ms | 103.3 ms | 67.94x |
| insert | 100 | 7,354.9 ms | 155.7 ms | 47.23x |
| insert | 500 | 7,200.0 ms | 313.2 ms | 22.99x |
| delete | 1 | 7,073.8 ms | 86.7 ms | 81.61x |
| delete | 10 | 7,092.2 ms | 92.1 ms | 77.03x |
| delete | 100 | 7,019.7 ms | 105.6 ms | 66.48x |
| delete | 500 | 7,327.3 ms | 115.3 ms | 63.52x |
| mixed | 1 + 1 | 7,138.0 ms | 173.8 ms | 41.07x |
| mixed | 10 + 10 | 7,052.5 ms | 175.5 ms | 40.19x |
| mixed | 100 + 100 | 7,056.3 ms | 238.7 ms | 29.56x |
| mixed | 500 + 500 | 7,027.1 ms | 408.6 ms | 17.20x |

At 100K documents, full rebuild is a roughly seven-second freshness cost for
every tested update size. Incremental maintenance stays below half a second for
all tested updates, including the 500 insert + 500 delete mixed batch.

### Generated Tenant: 500,000 Documents

This run used a generated single tenant with:

- `account_900001`
- 500,000 documents
- 38 boards
- 681.9 MiB `items.jsonl`
- 3 runs per scenario

Times are medians in milliseconds.

| Operation | Batch | Full rebuild | Incremental | Speedup |
|---|---:|---:|---:|---:|
| build index | - | 11,968.8 ms | 12,220.7 ms | 0.98x |
| insert | 1 | 12,428.3 ms | 792.5 ms | 15.68x |
| insert | 10 | 13,913.8 ms | 1,225.9 ms | 11.35x |
| insert | 100 | 12,989.1 ms | 848.1 ms | 15.32x |
| insert | 500 | 12,378.1 ms | 1,035.1 ms | 11.96x |
| delete | 1 | 12,331.3 ms | 732.1 ms | 16.84x |
| delete | 10 | 12,229.1 ms | 931.5 ms | 13.13x |
| delete | 100 | 12,634.5 ms | 820.6 ms | 15.40x |
| delete | 500 | 12,435.1 ms | 792.8 ms | 15.69x |
| mixed | 1 + 1 | 12,560.2 ms | 2,609.6 ms | 4.81x |
| mixed | 10 + 10 | 13,048.8 ms | 1,421.1 ms | 9.18x |
| mixed | 100 + 100 | 12,905.2 ms | 2,019.8 ms | 6.39x |
| mixed | 500 + 500 | 13,868.8 ms | 2,184.3 ms | 6.35x |

At 500K documents, full rebuild is no longer close to the soft freshness target:
even the smallest update costs more than 12 seconds when the default index must
be rebuilt. Incremental maintenance remains well below rebuild, but update
latency is materially higher than at 100K. Single-operation insert/delete
updates mostly land between 0.7 s and 1.3 s. Mixed insert+delete updates are
noisier and land between 1.4 s and 2.6 s.

### Tail-Latency Spot Checks

The main tables above report medians. To get a better view of tail behavior,
selected incremental-only scenarios were rerun with 30 repetitions each.
Percentiles below use nearest-rank calculation.

| Scenario | p50 | p95 | p99 | Max |
|---|---:|---:|---:|---:|
| 100K insert 1 | 0.104 s | 0.165 s | 0.249 s | 0.249 s |
| 100K mixed 500+500 | 0.416 s | 0.477 s | 0.531 s | 0.531 s |
| 500K insert 1 | 0.856 s | 1.310 s | 1.668 s | 1.668 s |
| 500K delete 1 | 0.788 s | 1.993 s | 2.372 s | 2.372 s |
| 500K mixed 500+500 | 1.727 s | 3.610 s | 4.153 s | 4.153 s |

The 100K tail-latency samples are tight: p99 stays below 0.54 s for both tested
scenarios. At 500K, insert/delete remain in the low-second range, while
mixed 500+500 shows the most variance. Even that mixed p99 remains below the
13.87 s full-rebuild median for the same scenario, but it should be presented
separately from insert/delete when setting latency expectations.

## Analysis

### Initial Build Cost

Incremental index creation is slightly slower on the small evaluation tenants,
which is expected. The incremental path creates the same FTS structures plus the
trigger machinery needed to keep the index fresh.

On the generated 100K tenant, initial build time was effectively the same:

- default rebuild: 7.40 s
- incremental: 7.49 s

The generated 500K tenant showed the same pattern:

- default rebuild: 11.97 s
- incremental: 12.22 s

This setup cost is not the main target of the feature. The useful question is
whether later freshness updates avoid full rebuild cost.

### Insert Performance

Inserts are faster incrementally across all combined and larger-tenant cases.
The speedup is strongest for small batches:

- combined batch 1: 5.36x
- combined batch 10: 6.10x
- larger tenant batch 1: 6.25x
- generated 100K batch 1: 66.22x
- generated 100K batch 10: 67.94x
- generated 500K batch 1: 15.68x
- generated 500K batch 500: 11.96x

As batch size grows, incremental work grows with the number of inserted tokens
and documents. Full rebuild remains comparatively flat, so the speedup narrows.
Even at 500 inserted rows on the generated 100K tenant, incremental update time
was 313 ms versus 7.20 s for rebuild.

On the generated 500K tenant, incremental insert latency was higher: 793 ms for
one row and 1.04 s for 500 rows. That is still far below the 12-14 s rebuild
baseline, but it shows that incremental update cost is not independent of tenant
size.

### Delete Performance

Deletes are the strongest result.

For the combined tenant, incremental delete speedups stay between 9.43x and
12.52x across all tested batch sizes. For the larger tenant, they stay between
8.78x and 12.96x.

For the generated 100K tenant, delete speedups ranged from 63.52x to 81.61x.
The incremental delete path stayed between 86.7 ms and 115.3 ms across all
tested batch sizes.

For the generated 500K tenant, delete speedups ranged from 13.13x to 16.84x.
Incremental delete latency stayed between 732 ms and 932 ms across tested batch
sizes.

This is a good sign for freshness workloads where deletes arrive as small deltas.

### Mixed Insert/Delete Performance

The mixed operation inserts N rows and deletes N rows in one transaction.

Incremental wins clearly for the combined and larger tenant, but the advantage
narrows at larger batch sizes:

- combined batch 1 + 1: 4.04x
- combined batch 500 + 500: 2.10x
- larger tenant batch 1 + 1: 4.26x
- larger tenant batch 500 + 500: 1.65x
- generated 100K batch 1 + 1: 41.07x
- generated 100K batch 500 + 500: 17.20x
- generated 500K batch 1 + 1: 4.81x
- generated 500K batch 500 + 500: 6.35x

For the 626-document tenant, full rebuild wins at mixed 500 + 500. That is not
surprising: changing 1,000 rows around a 626-document base is no longer a small
freshness delta.

For the generated 100K tenant, the same 500 + 500 mixed batch is still only a
1% source-table delta. Incremental maintenance stays comfortably ahead at
408.6 ms versus 7.03 s for rebuild.

For the generated 500K tenant, mixed updates are still faster incrementally, but
less clean than single-operation inserts/deletes. Incremental mixed timings range
from 1.42 s to 2.61 s, while rebuild ranges from 12.56 s to 13.87 s.

### 500K Mixed-Path Diagnostic

The first 500K full sweep showed higher and noisier incremental mixed timings,
especially:

- mixed 1 + 1: 2.61 s
- mixed 100 + 100: 2.02 s
- mixed 500 + 500: 2.18 s

To isolate this, a focused incremental-only diagnostic was run on the same 500K
dataset with three mixed variants:

| Operation | Meaning |
|---|---|
| `mixed` | Insert batch, then delete batch, inside one transaction. |
| `mixed_no_txn` | Insert batch, then delete batch, without an explicit transaction wrapper. |
| `mixed_delete_insert` | Delete batch, then insert batch, inside one transaction. |

Median timings:

| Operation | Batch 1 + 1 | Batch 10 + 10 | Batch 100 + 100 | Batch 500 + 500 |
|---|---:|---:|---:|---:|
| `mixed` | 1,176.5 ms | 1,203.0 ms | 1,240.2 ms | 1,485.9 ms |
| `mixed_no_txn` | 1,174.2 ms | 1,246.5 ms | 1,445.3 ms | 1,481.1 ms |
| `mixed_delete_insert` | 1,409.9 ms | 1,593.6 ms | 1,481.1 ms | 1,752.1 ms |

Findings:

- The explicit transaction wrapper is not the cause. `mixed` and
  `mixed_no_txn` are almost identical for small batches.
- Insert-then-delete is better than delete-then-insert in this setup.
- The focused rerun was materially faster than the first full sweep for the same
  `mixed` operation, so the earlier 2-2.6 s values include meaningful run-to-run
  variance.
- Even in the focused run, mixed updates are still slower than isolated
  insert/delete updates. At 500K, mixed appears to have a fixed cost around
  1.2 s plus some growth with batch size.

## Takeaways

The benchmark supports the incremental FTS direction for freshness updates:

- Incremental maintenance avoids paying full rebuild cost for small deltas.
- Deletes are particularly efficient.
- Inserts and mixed workloads remain favorable for realistic small batches.
- Full rebuild remains a reasonable fallback for tiny tenants with very large
  batches relative to tenant size.
- On a generated 100K tenant, full rebuild costs about seven seconds per update
  regardless of batch size, while incremental maintenance stays below half a
  second for every tested update.
- On a generated 500K tenant, full rebuild costs about 12-14 seconds per update.
  Incremental medians stay below 2.7 seconds, with mixed 500+500 tail latency
  reaching 4.15 seconds at p99 in the 30-run spot check.
- The next useful benchmark step is to run the generated-data equality sweep and
  then extend the same harness to the 1M explicit tenant.
