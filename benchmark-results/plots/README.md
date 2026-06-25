# FTS Benchmark Plots

Generated SVG charts for the updatable FTS benchmark report.

- `headline_generated_latency.svg`: representative full rebuild vs incremental medians.
- `incremental_tail_latency.svg`: p50/p95/p99 incremental spot checks.
- `tenant_size_scaling.svg`: representative scaling across eval and generated tenants.
- `mixed_path_diagnostic_500k.svg`: 500K mixed-operation diagnostic variants.
- `trigger_component_breakdown.svg`: first-pass trigger maintenance component timings.
- `trigger_optimization_targets.svg`: grouped trigger hot spots for optimization planning.

Regenerate:

```sh
python3 scripts/plot_fts_benchmarks.py
```
