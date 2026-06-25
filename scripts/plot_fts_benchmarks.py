#!/usr/bin/env python3
import csv
import html
import math
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESULTS_CSV = ROOT / "benchmark-results/fts_update_results.csv"
PROFILE_RESULTS_CSV = ROOT / "benchmark-results/trigger-profiles/fts_trigger_profile_results.csv"
PLOTS_DIR = ROOT / "benchmark-results/plots"


COLORS = {
    "default": "#b84a42",
    "incremental": "#227c67",
    "tail_p50": "#7a9cc6",
    "tail_p95": "#f0a35e",
    "tail_p99": "#b84a42",
    "profile_insert": "#227c67",
    "profile_delete": "#7a9cc6",
    "profile_hot": "#b84a42",
    "profile_other": "#8a94a6",
    "grid": "#d9dee7",
    "text": "#1f2933",
    "muted": "#52606d",
}


GENERATED_MEDIANS = {
    "100K insert 1": {"full": 7.1262, "incremental": 0.1076, "speedup": 66.22},
    "100K mixed 500+500": {"full": 7.0271, "incremental": 0.4086, "speedup": 17.20},
    "500K insert 1": {"full": 12.4283, "incremental": 0.7925, "speedup": 15.68},
    "500K delete 1": {"full": 12.3313, "incremental": 0.7321, "speedup": 16.84},
    "500K mixed 500+500": {"full": 13.8688, "incremental": 2.1843, "speedup": 6.35},
}


TENANT_SCALING = [
    ("626", "insert 1", 0.0364, 0.0138),
    ("2.7K", "insert 1", 0.1026, 0.0164),
    ("100K", "insert 1", 7.1262, 0.1076),
    ("500K", "insert 1", 12.4283, 0.7925),
    ("626", "mixed 500+500", 0.0395, 0.0489),
    ("2.7K", "mixed 500+500", 0.1053, 0.0637),
    ("100K", "mixed 500+500", 7.0271, 0.4086),
    ("500K", "mixed 500+500", 13.8688, 2.1843),
]


MIXED_DIAGNOSTIC_500K = {
    "mixed": {"1+1": 1.1765, "10+10": 1.2030, "100+100": 1.2402, "500+500": 1.4859},
    "mixed_no_txn": {"1+1": 1.1742, "10+10": 1.2465, "100+100": 1.4453, "500+500": 1.4811},
    "mixed_delete_insert": {"1+1": 1.4099, "10+10": 1.5936, "100+100": 1.4811, "500+500": 1.7521},
}


TAIL_RUNS = {
    "20260618-103615": "100K insert 1",
    "20260618-104032": "100K mixed 500+500",
    "20260618-104449": "500K insert 1",
    "20260618-105251": "500K delete 1",
    "20260618-111120": "500K mixed 500+500",
}


def esc(value):
    return html.escape(str(value), quote=True)


def percentile(values, p):
    values = sorted(values)
    idx = max(0, math.ceil(p / 100 * len(values)) - 1)
    return values[idx]


def read_tail_values():
    values = {label: [] for label in TAIL_RUNS.values()}
    if not RESULTS_CSV.exists():
        return values
    with RESULTS_CSV.open() as handle:
        for row in csv.DictReader(handle):
            label = TAIL_RUNS.get(row["run_id"])
            if label:
                values[label].append(float(row["seconds"]))
    return values


def read_profile_rows():
    if not PROFILE_RESULTS_CSV.exists():
        return []
    with PROFILE_RESULTS_CSV.open() as handle:
        return list(csv.DictReader(handle))


def profile_rows_for(run_id):
    return [row for row in read_profile_rows() if row["run_id"] == run_id]


def latest_profile_run(base_rows, operation, batch_size, no_json_profile=False):
    rows = [
        row
        for row in read_profile_rows()
        if row["base_rows"] == str(base_rows)
        and row["operation"] == operation
        and row["batch_size"] == str(batch_size)
    ]
    if no_json_profile:
        rows = [row for row in rows if not row["plan_file"]]
    if not rows:
        return None
    return sorted({row["run_id"] for row in rows})[-1]


def svg_doc(width, height, body):
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<rect width="100%" height="100%" fill="white"/>
<style>
text {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; fill: {COLORS['text']}; }}
.title {{ font-size: 24px; font-weight: 700; }}
.subtitle {{ font-size: 13px; fill: {COLORS['muted']}; }}
.axis {{ font-size: 12px; fill: {COLORS['muted']}; }}
.label {{ font-size: 13px; }}
.small {{ font-size: 11px; fill: {COLORS['muted']}; }}
.value {{ font-size: 12px; font-weight: 600; }}
.barlabel {{ font-size: 11px; fill: white; font-weight: 600; }}
</style>
{body}
</svg>
"""


def write_svg(name, width, height, body):
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = PLOTS_DIR / name
    path.write_text(svg_doc(width, height, body), encoding="utf-8")
    return path


def log_x(value, x0, width, min_v=0.05, max_v=20.0):
    value = max(value, min_v)
    return x0 + (math.log10(value) - math.log10(min_v)) / (math.log10(max_v) - math.log10(min_v)) * width


def draw_log_axis(x0, y0, width):
    ticks = [0.05, 0.1, 0.5, 1, 5, 10, 20]
    parts = [f'<line x1="{x0}" y1="{y0}" x2="{x0 + width}" y2="{y0}" stroke="{COLORS["grid"]}"/>']
    for tick in ticks:
        x = log_x(tick, x0, width)
        label = f"{tick:g}s"
        parts.append(f'<line x1="{x:.1f}" y1="{y0 - 6}" x2="{x:.1f}" y2="{y0 + 6}" stroke="{COLORS["grid"]}"/>')
        parts.append(f'<text class="axis" x="{x:.1f}" y="{y0 + 22}" text-anchor="middle">{label}</text>')
    return "\n".join(parts)


def plot_headline_medians():
    width, height = 1180, 640
    x0, chart_w = 300, 740
    y0, row_h = 112, 92
    bar_h = 18
    rows = list(GENERATED_MEDIANS.items())
    parts = [
        '<text class="title" x="40" y="42">Generated Tenant Update Latency</text>',
        '<text class="subtitle" x="40" y="66">Median update freshness cost; x-axis is logarithmic seconds</text>',
        draw_log_axis(x0, height - 70, chart_w),
        f'<rect x="870" y="30" width="18" height="12" fill="{COLORS["default"]}"/><text class="axis" x="896" y="41">full rebuild</text>',
        f'<rect x="990" y="30" width="18" height="12" fill="{COLORS["incremental"]}"/><text class="axis" x="1016" y="41">incremental</text>',
    ]
    for i, (label, data) in enumerate(rows):
        y = y0 + i * row_h
        parts.append(f'<text class="label" x="40" y="{y + 16}">{esc(label)}</text>')
        parts.append(f'<text class="small" x="40" y="{y + 36}">{data["speedup"]:.1f}x speedup</text>')
        for offset, key, color in [(0, "full", COLORS["default"]), (28, "incremental", COLORS["incremental"])]:
            v = data[key]
            x1 = log_x(v, x0, chart_w)
            parts.append(f'<line x1="{x0}" y1="{y + offset}" x2="{x1:.1f}" y2="{y + offset}" stroke="{color}" stroke-width="{bar_h}" stroke-linecap="round"/>')
            parts.append(f'<text class="value" x="{x1 + 10:.1f}" y="{y + offset + 4}">{v:.3g}s</text>')
    write_svg("headline_generated_latency.svg", width, height, "\n".join(parts))


def plot_tail_latency():
    tail_values = read_tail_values()
    stats = []
    for label in GENERATED_MEDIANS:
        vals = tail_values.get(label, [])
        if vals:
            stats.append((label, percentile(vals, 50), percentile(vals, 95), percentile(vals, 99)))
    width, height = 1260, 640
    x0, chart_w = 300, 640
    value_x = 990
    y0, row_h = 120, 88
    parts = [
        '<text class="title" x="40" y="42">Incremental Tail-Latency Spot Checks</text>',
        '<text class="subtitle" x="40" y="66">30 repetitions per scenario; nearest-rank percentiles; x-axis is logarithmic seconds</text>',
        draw_log_axis(x0, height - 70, chart_w),
        f'<circle cx="880" cy="38" r="6" fill="{COLORS["tail_p50"]}"/><text class="axis" x="894" y="42">p50</text>',
        f'<circle cx="950" cy="38" r="6" fill="{COLORS["tail_p95"]}"/><text class="axis" x="964" y="42">p95</text>',
        f'<circle cx="1020" cy="38" r="6" fill="{COLORS["tail_p99"]}"/><text class="axis" x="1034" y="42">p99</text>',
        f'<text class="axis" x="{value_x}" y="96">p50</text>',
        f'<text class="axis" x="{value_x + 70}" y="96">p95</text>',
        f'<text class="axis" x="{value_x + 140}" y="96">p99</text>',
    ]
    for i, (label, p50, p95, p99) in enumerate(stats):
        y = y0 + i * row_h
        parts.append(f'<text class="label" x="40" y="{y + 5}">{esc(label)}</text>')
        parts.append(f'<line x1="{log_x(p50, x0, chart_w):.1f}" y1="{y}" x2="{log_x(p99, x0, chart_w):.1f}" y2="{y}" stroke="{COLORS["grid"]}" stroke-width="4"/>')
        for val, color in [
            (p50, COLORS["tail_p50"]),
            (p95, COLORS["tail_p95"]),
            (p99, COLORS["tail_p99"]),
        ]:
            x = log_x(val, x0, chart_w)
            parts.append(f'<circle cx="{x:.1f}" cy="{y}" r="7" fill="{color}"/>')
        parts.append(f'<text class="value" x="{value_x}" y="{y + 5}">{p50:.2f}s</text>')
        parts.append(f'<text class="value" x="{value_x + 70}" y="{y + 5}">{p95:.2f}s</text>')
        parts.append(f'<text class="value" x="{value_x + 140}" y="{y + 5}">{p99:.2f}s</text>')
    write_svg("incremental_tail_latency.svg", width, height, "\n".join(parts))


def plot_tenant_scaling():
    width, height = 1180, 620
    x0, chart_w = 310, 730
    y0, row_h = 112, 58
    parts = [
        '<text class="title" x="40" y="42">Latency Scaling by Tenant Size</text>',
        '<text class="subtitle" x="40" y="66">Representative median scenarios from eval and generated tenants; x-axis is logarithmic seconds</text>',
        draw_log_axis(x0, height - 60, chart_w),
        f'<rect x="860" y="30" width="18" height="12" fill="{COLORS["default"]}"/><text class="axis" x="886" y="41">full rebuild</text>',
        f'<rect x="990" y="30" width="18" height="12" fill="{COLORS["incremental"]}"/><text class="axis" x="1016" y="41">incremental</text>',
    ]
    for i, (tenant, scenario, full, incr) in enumerate(TENANT_SCALING):
        y = y0 + i * row_h
        parts.append(f'<text class="label" x="40" y="{y + 5}">{tenant} docs</text>')
        parts.append(f'<text class="small" x="140" y="{y + 5}">{esc(scenario)}</text>')
        for offset, value, color in [(0, full, COLORS["default"]), (22, incr, COLORS["incremental"])]:
            x1 = log_x(value, x0, chart_w)
            parts.append(f'<line x1="{x0}" y1="{y + offset}" x2="{x1:.1f}" y2="{y + offset}" stroke="{color}" stroke-width="14" stroke-linecap="round"/>')
            parts.append(f'<text class="value" x="{x1 + 8:.1f}" y="{y + offset + 4}">{value:.3g}s</text>')
    write_svg("tenant_size_scaling.svg", width, height, "\n".join(parts))


def plot_mixed_diagnostic():
    width, height = 1180, 620
    margin_left, margin_bottom = 95, 85
    chart_w, chart_h = 960, 420
    x0, y0 = margin_left, 90
    batches = ["1+1", "10+10", "100+100", "500+500"]
    variants = list(MIXED_DIAGNOSTIC_500K.keys())
    colors = ["#227c67", "#7a9cc6", "#f0a35e"]
    max_v = 2.0
    parts = [
        '<text class="title" x="40" y="42">500K Mixed-Path Diagnostic</text>',
        '<text class="subtitle" x="40" y="66">Incremental-only medians; testing transaction wrapper and operation order</text>',
    ]
    for tick in [0, 0.5, 1.0, 1.5, 2.0]:
        y = y0 + chart_h - tick / max_v * chart_h
        parts.append(f'<line x1="{x0}" y1="{y:.1f}" x2="{x0 + chart_w}" y2="{y:.1f}" stroke="{COLORS["grid"]}"/>')
        parts.append(f'<text class="axis" x="{x0 - 12}" y="{y + 4:.1f}" text-anchor="end">{tick:g}s</text>')
    group_w = chart_w / len(batches)
    bar_w = 42
    for i, batch in enumerate(batches):
        gx = x0 + i * group_w + 45
        parts.append(f'<text class="axis" x="{gx + 70}" y="{y0 + chart_h + 34}" text-anchor="middle">{batch}</text>')
        for j, variant in enumerate(variants):
            value = MIXED_DIAGNOSTIC_500K[variant][batch]
            h = value / max_v * chart_h
            x = gx + j * (bar_w + 8)
            y = y0 + chart_h - h
            parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w}" height="{h:.1f}" fill="{colors[j]}"/>')
            parts.append(f'<text class="small" x="{x + bar_w / 2:.1f}" y="{y - 6:.1f}" text-anchor="middle">{value:.2f}s</text>')
    for j, variant in enumerate(variants):
        lx = 760 + j * 135
        parts.append(f'<rect x="{lx}" y="32" width="16" height="12" fill="{colors[j]}"/>')
        parts.append(f'<text class="axis" x="{lx + 22}" y="43">{esc(variant)}</text>')
    write_svg("mixed_path_diagnostic_500k.svg", width, height, "\n".join(parts))


def component_label(component):
    labels = {
        "insert_00_docs": "insert docs + lengths",
        "insert_10_dict_insert": "insert new dict terms",
        "insert_20_terms": "insert postings",
        "insert_30_dict_df": "insert: recompute dict df",
        "insert_40_stats": "insert: recompute stats",
        "delete_00_terms": "delete postings",
        "delete_10_docs": "delete docs",
        "delete_20_dict_df": "delete: recompute dict df",
        "delete_30_dict_prune": "prune dict",
        "delete_40_stats": "delete: recompute stats",
    }
    return labels.get(component, component)


def profile_bar_color(component):
    if component in {"insert_00_docs", "insert_30_dict_df", "delete_20_dict_df"}:
        return COLORS["profile_hot"]
    if component.startswith("insert"):
        return COLORS["profile_insert"]
    if component.startswith("delete"):
        return COLORS["profile_delete"]
    return COLORS["profile_other"]


def plot_trigger_component_breakdown():
    run_100k = latest_profile_run(100000, "insert", 1)
    run_500k = latest_profile_run(500000, "mixed", 500, no_json_profile=True)
    scenarios = []
    if run_100k:
        scenarios.append(("100K insert 1", profile_rows_for(run_100k)))
    if run_500k:
        scenarios.append(("500K mixed 500+500", profile_rows_for(run_500k)))
    if not scenarios:
        return

    width, height = 1260, 700
    parts = [
        '<text class="title" x="40" y="42">Trigger Maintenance Component Breakdown</text>',
        '<text class="subtitle" x="40" y="66">Trigger-equivalent statement timings; bars show share of summed component time</text>',
    ]
    panel_w = 560
    panel_gap = 70
    for panel_idx, (title, rows) in enumerate(scenarios):
        x = 40 + panel_idx * (panel_w + panel_gap)
        y = 115
        rows = sorted(rows, key=lambda row: float(row["seconds"]), reverse=True)
        total = sum(float(row["seconds"]) for row in rows)
        parts.append(f'<text class="label" x="{x}" y="{y - 25}">{esc(title)}</text>')
        parts.append(f'<text class="small" x="{x}" y="{y - 7}">sum of components: {total:.3f}s</text>')
        for i, row in enumerate(rows):
            sec = float(row["seconds"])
            share = sec / total if total else 0
            by = y + i * 48
            bar_w = max(2, share * 260)
            color = profile_bar_color(row["component"])
            parts.append(f'<text class="small" x="{x}" y="{by + 13}">{esc(component_label(row["component"]))}</text>')
            parts.append(f'<rect x="{x + 205}" y="{by}" width="{bar_w:.1f}" height="18" fill="{color}"/>')
            parts.append(f'<text class="value" x="{x + 205 + bar_w + 8:.1f}" y="{by + 14}">{sec:.3f}s ({share * 100:.0f}%)</text>')
    parts.append(f'<rect x="40" y="650" width="14" height="10" fill="{COLORS["profile_hot"]}"/><text class="axis" x="62" y="659">current likely optimization target</text>')
    write_svg("trigger_component_breakdown.svg", width, height, "\n".join(parts))


def plot_trigger_optimization_targets():
    run_100k = latest_profile_run(100000, "insert", 1)
    run_500k = latest_profile_run(500000, "mixed", 500, no_json_profile=True)
    rows = []
    if run_100k:
        data = profile_rows_for(run_100k)
        total = sum(float(row["seconds"]) for row in data)
        dict_df = sum(float(row["seconds"]) for row in data if row["component"].endswith("dict_df"))
        token_len = sum(float(row["seconds"]) for row in data if row["component"] == "insert_00_docs")
        other = max(0, total - dict_df - token_len)
        rows.append(("100K insert 1", token_len, dict_df, other, total))
    if run_500k:
        data = profile_rows_for(run_500k)
        total = sum(float(row["seconds"]) for row in data)
        dict_df = sum(float(row["seconds"]) for row in data if row["component"].endswith("dict_df"))
        token_len = sum(float(row["seconds"]) for row in data if row["component"] == "insert_00_docs")
        other = max(0, total - dict_df - token_len)
        rows.append(("500K mixed 500+500", token_len, dict_df, other, total))
    if not rows:
        return

    width, height = 1120, 520
    x0, y0 = 290, 140
    chart_w = 620
    row_h = 120
    max_total = max(row[-1] for row in rows)
    parts = [
        '<text class="title" x="40" y="42">Where Trigger Update Time Is Going</text>',
        '<text class="subtitle" x="40" y="66">Grouped from trigger-equivalent component timings; highlights first optimization targets</text>',
        f'<rect x="635" y="32" width="16" height="12" fill="{COLORS["profile_insert"]}"/><text class="axis" x="657" y="43">insert tokenization / length</text>',
        f'<rect x="825" y="32" width="16" height="12" fill="{COLORS["profile_hot"]}"/><text class="axis" x="847" y="43">dict df recompute</text>',
        f'<rect x="975" y="32" width="16" height="12" fill="{COLORS["profile_other"]}"/><text class="axis" x="997" y="43">other</text>',
    ]
    for i, (label, token_len, dict_df, other, total) in enumerate(rows):
        y = y0 + i * row_h
        parts.append(f'<text class="label" x="40" y="{y + 18}">{esc(label)}</text>')
        parts.append(f'<text class="small" x="40" y="{y + 38}">component sum {total:.3f}s</text>')
        x = x0
        for value, color in [
            (token_len, COLORS["profile_insert"]),
            (dict_df, COLORS["profile_hot"]),
            (other, COLORS["profile_other"]),
        ]:
            w = value / max_total * chart_w if max_total else 0
            if w > 0:
                parts.append(f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="34" fill="{color}"/>')
                if w > 58:
                    parts.append(f'<text class="barlabel" x="{x + w / 2:.1f}" y="{y + 22}" text-anchor="middle">{value:.2f}s</text>')
            x += w
        parts.append(f'<text class="value" x="{x + 8:.1f}" y="{y + 22}">{total:.2f}s</text>')
    write_svg("trigger_optimization_targets.svg", width, height, "\n".join(parts))


def write_readme():
    content = """# FTS Benchmark Plots

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
"""
    PLOTS_DIR.mkdir(parents=True, exist_ok=True)
    (PLOTS_DIR / "README.md").write_text(content, encoding="utf-8")


def main():
    plot_headline_medians()
    plot_tail_latency()
    plot_tenant_scaling()
    plot_mixed_diagnostic()
    plot_trigger_component_breakdown()
    plot_trigger_optimization_targets()
    write_readme()
    for path in sorted(PLOTS_DIR.glob("*")):
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
