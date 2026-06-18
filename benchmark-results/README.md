# FTS Update Benchmark Outputs

This directory is intentionally git-ignored except for this README. The FTS
update benchmark writes CSV output here by default.

Run a quick smoke benchmark:

```sh
python3 scripts/fts_update_benchmark.py --runs 1 --batch-sizes 1 --tenants account_31245546 --recreate-db --clear-results
```

Run the default sweep on the current small evaluation dataset:

```sh
python3 scripts/fts_update_benchmark.py --recreate-db --clear-results
```

Generate a 100K single-tenant data-generator dataset:

```sh
node scripts/generate_datagen_dataset.mjs \
  --account-sizes 100000 \
  --output-dir benchmark-results/datagen-100k
```

Benchmark that 100K tenant:

```sh
python3 scripts/fts_update_benchmark.py \
  --dataset-kind datagen-jsonl \
  --data benchmark-results/datagen-100k/items.jsonl \
  --tenants account_900001 \
  --batch-sizes 1,10,100,500 \
  --runs 5 \
  --recreate-db \
  --clear-results
```

Generate and benchmark a 500K single-tenant dataset:

```sh
node scripts/generate_datagen_dataset.mjs \
  --account-sizes 500000 \
  --output-dir benchmark-results/datagen-500k
```

```sh
python3 scripts/fts_update_benchmark.py \
  --dataset-kind datagen-jsonl \
  --data benchmark-results/datagen-500k/items.jsonl \
  --tenants account_900001 \
  --batch-sizes 1,10,100,500 \
  --runs 3 \
  --statement-timeout 900 \
  --recreate-db \
  --clear-results
```

Generate larger explicit tenant sizes later:

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

For generated JSONL, the benchmark exposes tenant labels as `account_<id>` and
also adds a `combined` tenant across all generated rows.

Detailed recorded results and analysis live in
`benchmark-results/fts_update_benchmark_report.md`.
