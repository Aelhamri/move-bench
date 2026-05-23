"""
RQ1-FINAL — Charts matplotlib pour chapitre Results du mémoire
================================================================
Synthèse des recommandations de 3 agents (data viz / academic standards / narrative).

5 figures produites en PDF vectoriel + PNG :
  1. fig1_matrix.pdf       — Grouped bar log-scale 6×2 (LE chart de défense)
  2. fig2_speedup.pdf      — Lollipop horizontal speedup vs MOVE upstream
  3. fig3_pareto.pdf       — Scatter trade-off perf/RAM/CPU
  4. fig4_distribution.pdf — IQR min/max/median par cellule
  5. fig5_slope.pdf        — Slope chart cross-dataset (stability)

Setup style mémoire ULB :
  - LaTeX-friendly serif font 10pt
  - largeur 15 cm (single column A4)
  - B&W safe palettes (hatching + grey levels)
  - PDF vectoriel + PNG 300 DPI fallback

Usage : python3 /home/osboxes/rq1/bench5_charts.py
Output : /home/osboxes/rq1/charts/*.pdf,png
"""

import csv
from pathlib import Path
import matplotlib.pyplot as plt
import matplotlib as mpl
import numpy as np

# ----- Style global mémoire -----
mpl.rcParams.update({
    'font.family':       'serif',
    'font.size':         10,
    'axes.labelsize':    10,
    'axes.titlesize':    11,
    'xtick.labelsize':   9,
    'ytick.labelsize':   9,
    'legend.fontsize':   9,
    'pdf.fonttype':      42,    # TrueType embedding for LaTeX
    'ps.fonttype':       42,
    'axes.grid':         True,
    'grid.alpha':        0.3,
    'grid.linestyle':    ':',
})

CSV_PATH = Path('/home/osboxes/rq1/bench5_cross_matrix.csv')
OUT_DIR  = Path('/home/osboxes/rq1/charts')
OUT_DIR.mkdir(exist_ok=True)

CONDITIONS = [
    ('c1_ali_naive',     'Ali naïve'),
    ('c2_ali_optim',     'Ali optim'),
    ('c3_move_fast',     'MOVE Fast'),
    ('c4_columnar',      'Columnar'),
    ('c5_move_upstream', 'MOVE upstream'),
    ('c6_move_upgrade',  'MOVE upgrade'),
]

# B&W safe palette + hatch patterns
PATTERNS_STIB = ['',    '///', '\\\\\\', 'xxx', '...', '+++']
PATTERNS_AIS  = ['ooo', '|||', '---',    'OOO', '***', '///']
GREY_STIB = '#cccccc'
GREY_AIS  = '#666666'

ACCENT_WINNER   = '#1a7a3a'   # vert foncé pour C2 (winner)
ACCENT_BASELINE = '#cc3333'   # rouge pour C5 (baseline)


# -----------------------------------------------------------------------------
# Load CSV into dict
# -----------------------------------------------------------------------------
def load_data():
    data = {}
    with open(CSV_PATH) as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            key = (row['condition_id'], row['dataset'])
            try:
                data[key] = {
                    'label':    row['condition_label'],
                    'median':   float(row['median_ms']),
                    'ci_lo':    float(row['ci95_lo']),
                    'ci_hi':    float(row['ci95_hi']),
                    'iqr':      float(row['iqr_ms']),
                    'min':      float(row['min_ms']),
                    'max':      float(row['max_ms']),
                    'fps':      float(row['fps']),
                    'cpu':      float(row['cpu_percent_avg']) if row.get('cpu_percent_avg') else None,
                    'ram_peak': float(row['ram_peak_mb']) if row.get('ram_peak_mb') else None,
                    'ram_delta':float(row['ram_peak_delta_mb']) if row.get('ram_peak_delta_mb') else None,
                }
            except (ValueError, KeyError) as e:
                print(f"Skipping {key}: {e}")
    return data


data = load_data()
print(f"Loaded {len(data)} cells from CSV")


# -----------------------------------------------------------------------------
# Fig 1 — Grouped bar log-scale (THE chart de défense)
# -----------------------------------------------------------------------------
def fig1_matrix():
    fig, ax = plt.subplots(figsize=(15/2.54, 9/2.54))
    x = np.arange(len(CONDITIONS))
    w = 0.38

    stib_med = []; stib_lo = []; stib_hi = []
    ais_med  = []; ais_lo  = []; ais_hi  = []
    labels   = []
    for cid, lab in CONDITIONS:
        labels.append(lab)
        s = data.get((cid, 'stib'))
        a = data.get((cid, 'ais'))
        stib_med.append(s['median'] if s else 0)
        stib_lo.append(s['median'] - s['ci_lo'] if s else 0)
        stib_hi.append(s['ci_hi']  - s['median'] if s else 0)
        ais_med.append(a['median']  if a else 0)
        ais_lo.append(a['median']   - a['ci_lo'] if a else 0)
        ais_hi.append(a['ci_hi']    - a['median'] if a else 0)

    bars_stib = ax.bar(x - w/2, stib_med, w, yerr=[stib_lo, stib_hi],
                       capsize=2, color=GREY_STIB, edgecolor='black',
                       linewidth=0.8, label='STIB (14 vertices/trip)')
    bars_ais  = ax.bar(x + w/2, ais_med, w, yerr=[ais_lo, ais_hi],
                       capsize=2, color=GREY_AIS, edgecolor='black',
                       hatch='///', linewidth=0.8, label='AIS (1230 vertices/trip)')

    # Highlight winner (C2 = index 1) with accent edge
    for i, (cid, _) in enumerate(CONDITIONS):
        if cid == 'c2_ali_optim':
            for bar in (bars_stib[i], bars_ais[i]):
                bar.set_edgecolor(ACCENT_WINNER)
                bar.set_linewidth(2.0)
        elif cid == 'c5_move_upstream':
            for bar in (bars_stib[i], bars_ais[i]):
                bar.set_edgecolor(ACCENT_BASELINE)
                bar.set_linewidth(1.5)

    # 30/60 FPS guide lines
    ax.axhline(16.7, ls='--', c='black', lw=0.7, alpha=0.6)
    ax.text(len(CONDITIONS)-0.3, 17.5, '60 FPS (16.7 ms)', fontsize=8,
            ha='right', va='bottom', alpha=0.7)
    ax.axhline(33.3, ls=':',  c='black', lw=0.7, alpha=0.6)
    ax.text(len(CONDITIONS)-0.3, 35, '30 FPS (33.3 ms)', fontsize=8,
            ha='right', va='bottom', alpha=0.7)

    ax.set_yscale('log')
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=15, ha='right')
    ax.set_ylabel('Median frame time (ms, log scale)')
    ax.set_title('Median per-frame rendering time across 6 implementation strategies')
    ax.legend(loc='upper left', framealpha=0.9)
    ax.set_axisbelow(True)

    # Value labels above each bar
    for bar in list(bars_stib) + list(bars_ais):
        h = bar.get_height()
        if h > 0:
            ax.text(bar.get_x() + bar.get_width()/2, h * 1.15,
                    f'{h:.1f}', ha='center', va='bottom', fontsize=7)

    plt.tight_layout()
    out_pdf = OUT_DIR / 'fig1_matrix.pdf'
    out_png = OUT_DIR / 'fig1_matrix.png'
    plt.savefig(out_pdf, bbox_inches='tight')
    plt.savefig(out_png, bbox_inches='tight', dpi=300)
    plt.close()
    print(f"  Saved {out_pdf.name} + .png")


# -----------------------------------------------------------------------------
# Fig 2 — Speedup lollipop (vs C5 MOVE upstream)
# -----------------------------------------------------------------------------
def fig2_speedup():
    fig, ax = plt.subplots(figsize=(15/2.54, 7/2.54))

    baseline_stib = data[('c5_move_upstream', 'stib')]['median']
    baseline_ais  = data[('c5_move_upstream', 'ais')]['median']

    # Conditions excluding baseline, ordered by AIS speedup descending
    others = [(cid, lab) for cid, lab in CONDITIONS if cid != 'c5_move_upstream']
    others_with_speedup = []
    for cid, lab in others:
        s = data.get((cid, 'stib'))
        a = data.get((cid, 'ais'))
        sp_stib = baseline_stib / s['median'] if s else 0
        sp_ais  = baseline_ais  / a['median'] if a else 0
        others_with_speedup.append((cid, lab, sp_stib, sp_ais))
    others_with_speedup.sort(key=lambda x: x[3], reverse=True)

    y = np.arange(len(others_with_speedup))
    sp_stib_arr = [o[2] for o in others_with_speedup]
    sp_ais_arr  = [o[3] for o in others_with_speedup]
    labels = [o[1] for o in others_with_speedup]

    for i, (cid, lab, st, ai) in enumerate(others_with_speedup):
        # Stem grey
        ax.plot([1, max(st, ai)], [i, i], color='#cccccc', lw=1.5, zorder=1)
        # Markers
        ax.plot(st, i, marker='o', markersize=10, markerfacecolor='black',
                markeredgecolor='black', zorder=2,
                label='STIB' if i == 0 else None)
        ax.plot(ai, i, marker='s', markersize=10, markerfacecolor='white',
                markeredgecolor='black', markeredgewidth=1.5, zorder=2,
                label='AIS' if i == 0 else None)
        # Highlight winner C2
        if cid == 'c2_ali_optim':
            ax.plot(st, i, marker='o', markersize=14, markerfacecolor='none',
                    markeredgecolor=ACCENT_WINNER, markeredgewidth=2.5, zorder=3)
            ax.plot(ai, i, marker='s', markersize=14, markerfacecolor='none',
                    markeredgecolor=ACCENT_WINNER, markeredgewidth=2.5, zorder=3)
        # Value labels
        ax.text(st + 1.5, i + 0.15, f'{st:.1f}×', fontsize=8, va='center')
        ax.text(ai + 1.5, i - 0.15, f'{ai:.1f}×', fontsize=8, va='center')

    # Baseline reference line
    ax.axvline(1, color=ACCENT_BASELINE, linestyle='--', lw=1, alpha=0.7)
    ax.text(1.1, len(y) - 0.5, 'C5 baseline = 1×', fontsize=8,
            color=ACCENT_BASELINE, va='center')

    ax.set_yticks(y)
    ax.set_yticklabels(labels)
    ax.set_xlabel('Speedup factor vs MOVE upstream (C5)')
    ax.set_title('Speedup ratios across datasets (sorted by AIS performance)')
    ax.legend(loc='lower right', framealpha=0.9)
    ax.set_xlim(left=0)
    ax.invert_yaxis()
    ax.set_axisbelow(True)

    plt.tight_layout()
    out_pdf = OUT_DIR / 'fig2_speedup.pdf'
    out_png = OUT_DIR / 'fig2_speedup.png'
    plt.savefig(out_pdf, bbox_inches='tight')
    plt.savefig(out_png, bbox_inches='tight', dpi=300)
    plt.close()
    print(f"  Saved {out_pdf.name} + .png")


# -----------------------------------------------------------------------------
# Fig 3 — Pareto scatter perf/RAM/CPU
# -----------------------------------------------------------------------------
def fig3_pareto():
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15/2.54, 7/2.54), sharey=True)

    markers = {'c1_ali_naive':'o', 'c2_ali_optim':'D', 'c3_move_fast':'^',
               'c4_columnar':'s', 'c5_move_upstream':'X', 'c6_move_upgrade':'v'}

    for ax, dataset, title in [(ax1, 'stib', '(a) STIB'),
                                (ax2, 'ais', '(b) AIS')]:
        for cid, lab in CONDITIONS:
            d = data.get((cid, dataset))
            if d is None or d.get('ram_delta') is None:
                continue
            ram = max(1, d['ram_delta'])  # avoid log(0)
            cpu = d.get('cpu') or 50
            size = 50 + (cpu / 100) * 200   # CPU% → marker size
            color = ACCENT_WINNER if cid == 'c2_ali_optim' else (
                    ACCENT_BASELINE if cid == 'c5_move_upstream' else 'black')
            facecolor = color if cid in ('c2_ali_optim', 'c5_move_upstream') else 'white'
            ax.scatter(d['median'], ram, s=size, marker=markers[cid],
                       facecolor=facecolor, edgecolor=color, linewidth=1.5,
                       label=lab if dataset == 'stib' else None,
                       zorder=3, alpha=0.85)
            # Label
            ax.annotate(lab, (d['median'], ram),
                        xytext=(7, 7), textcoords='offset points',
                        fontsize=7, alpha=0.8)

        ax.set_xscale('log')
        ax.set_yscale('log')
        ax.set_xlabel('Median frame time (ms, log)')
        ax.set_title(title)
        ax.set_axisbelow(True)
        # Highlight Pareto-ideal region (bottom-left)
        ax.axvspan(0.1, 16.7, alpha=0.08, color='green')
        ax.axhspan(0.1, 100, alpha=0.08, color='green')

    ax1.set_ylabel('RAM peak Δ during run (MB, log)')
    ax1.legend(loc='upper right', framealpha=0.9, fontsize=7)

    fig.suptitle('Performance vs. memory trade-off (marker size ∝ CPU %)',
                 fontsize=11, y=0.99)

    plt.tight_layout()
    out_pdf = OUT_DIR / 'fig3_pareto.pdf'
    out_png = OUT_DIR / 'fig3_pareto.png'
    plt.savefig(out_pdf, bbox_inches='tight')
    plt.savefig(out_png, bbox_inches='tight', dpi=300)
    plt.close()
    print(f"  Saved {out_pdf.name} + .png")


# -----------------------------------------------------------------------------
# Fig 4 — Distribution IQR (min/max/median)
# -----------------------------------------------------------------------------
def fig4_distribution():
    fig, ax = plt.subplots(figsize=(15/2.54, 9/2.54))

    y_labels = []
    y_positions = []
    for i, (cid, lab) in enumerate(CONDITIONS):
        for j, dataset in enumerate(['stib', 'ais']):
            pos = i * 2.5 + (1 if dataset == 'stib' else 1.7)
            y_labels.append(f'{lab} ({dataset.upper()})')
            y_positions.append(pos)
            d = data.get((cid, dataset))
            if d is None:
                continue
            color = ACCENT_WINNER if cid == 'c2_ali_optim' else (
                    ACCENT_BASELINE if cid == 'c5_move_upstream' else 'black')
            face = '#dddddd' if dataset == 'stib' else '#999999'
            # min-max range
            ax.hlines(pos, d['min'], d['max'], color='#bbbbbb', lw=1.5, zorder=1)
            # IQR centered on median (approx since iqr is range)
            ax.hlines(pos, d['median'] - d['iqr']/2, d['median'] + d['iqr']/2,
                      color=face, lw=6, zorder=2)
            # Median point
            ax.plot(d['median'], pos, marker='o', markersize=7,
                    markerfacecolor='white', markeredgecolor=color,
                    markeredgewidth=1.8, zorder=3)
            # Label value
            ax.text(d['max'] * 1.08, pos, f'{d["median"]:.1f} ms',
                    fontsize=7, va='center', color=color)

    # FPS guide
    ax.axvspan(0, 16.7, alpha=0.06, color='green', zorder=0)
    ax.axvline(33.3, ls=':', color='black', lw=0.7, alpha=0.5)
    ax.text(34, max(y_positions), '30 FPS', fontsize=7, va='top', alpha=0.7)

    ax.set_yticks(y_positions)
    ax.set_yticklabels(y_labels, fontsize=8)
    ax.set_xscale('log')
    ax.set_xlabel('Per-frame time distribution (ms, log scale)')
    ax.set_title('Performance distribution per condition + dataset\n'
                 '(thin line = min/max, thick = IQR, white dot = median)')
    ax.invert_yaxis()
    ax.set_axisbelow(True)

    plt.tight_layout()
    out_pdf = OUT_DIR / 'fig4_distribution.pdf'
    out_png = OUT_DIR / 'fig4_distribution.png'
    plt.savefig(out_pdf, bbox_inches='tight')
    plt.savefig(out_png, bbox_inches='tight', dpi=300)
    plt.close()
    print(f"  Saved {out_pdf.name} + .png")


# -----------------------------------------------------------------------------
# Fig 5 — Slope chart cross-dataset (stability)
# -----------------------------------------------------------------------------
def fig5_slope():
    fig, ax = plt.subplots(figsize=(15/2.54, 8/2.54))

    for cid, lab in CONDITIONS:
        s = data.get((cid, 'stib'))
        a = data.get((cid, 'ais'))
        if s is None or a is None:
            continue
        color = ACCENT_WINNER if cid == 'c2_ali_optim' else (
                ACCENT_BASELINE if cid == 'c5_move_upstream' else '#888888')
        lw = 2.5 if cid in ('c2_ali_optim', 'c5_move_upstream') else 1.3
        ax.plot([0, 1], [s['median'], a['median']], '-o',
                color=color, lw=lw, markersize=8,
                markerfacecolor='white', markeredgecolor=color,
                markeredgewidth=1.8)
        # Labels at both ends
        ax.text(-0.05, s['median'], f"{lab}\n{s['median']:.1f} ms",
                fontsize=8, ha='right', va='center', color=color)
        ax.text(1.05, a['median'], f"{lab}\n{a['median']:.1f} ms",
                fontsize=8, ha='left', va='center', color=color)

    ax.set_xticks([0, 1])
    ax.set_xticklabels(['STIB (14v/trip)', 'AIS (1230v/trip)'])
    ax.set_yscale('log')
    ax.set_ylabel('Median frame time (ms, log)')
    ax.set_xlim(-0.4, 1.4)
    ax.set_title('Cross-dataset performance stability\n'
                 '(flat line = robust to vertex complexity)')
    ax.set_axisbelow(True)
    ax.grid(True, alpha=0.3, axis='y')
    ax.grid(False, axis='x')

    plt.tight_layout()
    out_pdf = OUT_DIR / 'fig5_slope.pdf'
    out_png = OUT_DIR / 'fig5_slope.png'
    plt.savefig(out_pdf, bbox_inches='tight')
    plt.savefig(out_png, bbox_inches='tight', dpi=300)
    plt.close()
    print(f"  Saved {out_pdf.name} + .png")


# -----------------------------------------------------------------------------
# Generate all
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    print(f"Generating charts in {OUT_DIR}/")
    fig1_matrix()
    fig2_speedup()
    fig3_pareto()
    fig4_distribution()
    fig5_slope()
    print(f"Done. {len(list(OUT_DIR.glob('*.pdf')))} PDF files in {OUT_DIR}/")
