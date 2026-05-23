"""
RQ2 — Charts pour le mémoire (style RQ1, palette Okabe-Ito).
=============================================================

4 figures :
  fig1_fps_vs_n     : FPS médian = f(N_trips) pour QGIS et Flask, IQR shading
  fig2_load_phase   : Load time (matview, materialize, fetch+parse) par système
  fig3_two_panel      : 2 sous-figures côte à côte : FPS soutenu + RAM peak vs N (log X)
  fig4_speedup_curve: Ratio Flask/QGIS = f(N) avec CI95 interval arithmetic

Inputs : bench6_cross_matrix.csv (produit par bench6_aggregate.py).

Author: Ayoub El Hamri
"""

import csv
from pathlib import Path
import numpy as np
import matplotlib.pyplot as plt
import matplotlib as mpl


mpl.rcParams.update({
    'font.family':       ['Inter', 'IBM Plex Sans', 'Source Sans 3', 'DejaVu Sans'],
    'font.size':         10.5,
    'axes.labelsize':    11,
    'axes.titlesize':    12,
    'axes.titleweight':  'bold',
    'axes.titlepad':     16,
    'axes.labelweight':  'medium',
    'xtick.labelsize':   10,
    'ytick.labelsize':   10,
    'legend.fontsize':   9.5,
    'legend.frameon':    False,
    'pdf.fonttype':      42,
    'ps.fonttype':       42,
    'axes.spines.top':   False,
    'axes.spines.right': False,
    'axes.grid':         True,
    'grid.alpha':        0.22,
    'grid.linestyle':    '-',
    'grid.linewidth':    0.5,
    'axes.axisbelow':    True,
    'figure.facecolor':  'white',
    'axes.facecolor':    'white',
})

_HERE     = Path(__file__).resolve().parent
CSV_PATH  = _HERE / 'bench6_cross_matrix.csv'
OUT_DIR   = _HERE / 'charts'
OUT_DIR.mkdir(exist_ok=True)

# Palette : QGIS (rouge Okabe-Ito vermillion), Flask (bleu)
COLOR_QGIS  = '#D55E00'    # Okabe-Ito vermillion
COLOR_FLASK = '#0072B2'    # Okabe-Ito blue
COLOR_60FPS = '#009E73'    # Okabe-Ito green
COLOR_30FPS = '#666666'
COLOR_TEXT  = '#222222'
HATCH_FLASK = '///'


def load_matrix() -> dict:
    """Returns {(system, limit): row_dict}"""
    out = {}
    with open(CSV_PATH) as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                key = (row['system'], int(row['limit']))
                out[key] = {
                    'n_trips':         int(row['n_trips']) if row['n_trips'] else 0,
                    'n_runs_kept':     int(row['n_runs_kept']) if row['n_runs_kept'] else 0,
                    # v2 : métrique principale = FPS soutenu (pas median_ms)
                    'fps':             float(row['fps_sustained_med']) if row['fps_sustained_med'] else 0,
                    'fps_lo':          float(row['fps_tlog_lo']) if row['fps_tlog_lo'] else 0,
                    'fps_hi':          float(row['fps_tlog_hi']) if row['fps_tlog_hi'] else 0,
                    'fps_min':         float(row['fps_min']) if row['fps_min'] else 0,
                    'fps_max':         float(row['fps_max']) if row['fps_max'] else 0,
                    'median_ms':       float(row['median_ms_med']) if row['median_ms_med'] else 0,
                    'p95_ms':          float(row['p95_ms_med']) if row['p95_ms_med'] else 0,
                    'load_s':          float(row['load_seconds_med']) if row['load_seconds_med'] else 0,
                    'first_frame_ms':  float(row['first_frame_ms_med']) if row['first_frame_ms_med'] else 0,
                    'rss_mb':          float(row['rss_peak_mb_med']) if row['rss_peak_mb_med'] else 0,
                    'limit_label':     row['limit_label'],
                    # legacy alias pour compat
                    'ci95_lo':         float(row['fps_tlog_lo']) if row['fps_tlog_lo'] else 0,
                    'ci95_hi':         float(row['fps_tlog_hi']) if row['fps_tlog_hi'] else 0,
                }
            except (ValueError, KeyError):
                continue
    return out


def add_title(ax, title, subtitle=None):
    """Style RQ1 v6 : both title + subtitle via ax.text in axes coords."""
    if subtitle:
        ax.text(0, 1.14, title, transform=ax.transAxes,
                fontsize=12.5, fontweight='bold', color=COLOR_TEXT,
                ha='left', va='bottom')
        ax.text(0, 1.04, subtitle, transform=ax.transAxes,
                fontsize=10, color='#555555', style='italic',
                ha='left', va='bottom')
    else:
        ax.text(0, 1.04, title, transform=ax.transAxes,
                fontsize=12.5, fontweight='bold', color=COLOR_TEXT,
                ha='left', va='bottom')


def save(fig, name):
    pdf = OUT_DIR / f'{name}.pdf'
    png = OUT_DIR / f'{name}.png'
    fig.savefig(pdf, bbox_inches='tight', pad_inches=0.2)
    fig.savefig(png, bbox_inches='tight', pad_inches=0.2, dpi=200)
    plt.close(fig)
    print(f"  ✓ {name}.pdf + .png")


# -----------------------------------------------------------------------------
# Load
# -----------------------------------------------------------------------------
data = load_matrix()
print(f"Loaded {len(data)} cells from {CSV_PATH.name}")

# Limits sortées (0 = all_day en dernier)
all_limits = sorted({l for (_, l) in data.keys()}, key=lambda x: (x == 0, x))
# Effective N pour les axes log : on remplace 0 par max*1.5
def eff_n(limit):
    if limit > 0:
        return limit
    other_max = max((l for l in all_limits if l > 0), default=1)
    return other_max * 1.5


# -----------------------------------------------------------------------------
# Fig 1 — FPS = f(N), median + CI95 envelope
# -----------------------------------------------------------------------------
def fig1_fps_vs_n():
    fig, ax = plt.subplots(figsize=(12, 5.5))

    for system, color, label, hatch in [
        ('qgis',  COLOR_QGIS,  'QGIS C6 (memory layer)',                          None),
        ('flask', COLOR_FLASK, 'Flask + MapLibre WebGL (browser, 60 FPS cap rAF)', HATCH_FLASK),
    ]:
        xs, meds, lo, hi = [], [], [], []
        for limit in all_limits:
            m = data.get((system, limit))
            if not m or m['fps'] == 0:
                continue
            xs.append(eff_n(limit))
            meds.append(m['fps'])
            lo.append(m['fps_lo'] if m['fps_lo'] > 0 else m['fps'])
            hi.append(m['fps_hi'] if m['fps_hi'] > 0 else m['fps'])
        if not xs:
            continue
        ax.plot(xs, meds, marker='o', linewidth=2.2, markersize=8,
                color=color, label=label, zorder=3)
        ax.fill_between(xs, lo, hi, color=color, alpha=0.18, zorder=2)

    # Annotate Flask all_day OOM (cellule manquante)
    flask_alldays = data.get(('flask', 0))
    if not flask_alldays or flask_alldays['fps'] == 0:
        # Place le marker OOM à eff_n(0) sur la ligne du 60 FPS étendue
        x_oom = eff_n(0)
        ax.scatter([x_oom], [60], marker='X', s=200, color=COLOR_FLASK,
                   edgecolor='black', linewidth=1.5, zorder=5)
        ax.annotate('Flask OOM\n(payload ~200 MB JSON)', xy=(x_oom, 60),
                    xytext=(-80, -40), textcoords='offset points',
                    fontsize=9, color=COLOR_FLASK, fontweight='bold',
                    arrowprops=dict(arrowstyle='->', color=COLOR_FLASK, lw=1.2))

    ax.axhline(60, ls='-', color=COLOR_60FPS, lw=1.5, alpha=0.55, zorder=0)
    ax.text(eff_n(all_limits[-1]) * 1.05, 60, '60 FPS', fontsize=9,
            color=COLOR_60FPS, fontweight='bold', va='center')
    ax.axhline(30, ls='--', color=COLOR_30FPS, lw=1.2, alpha=0.5, zorder=0)
    ax.text(eff_n(all_limits[-1]) * 1.05, 30, '30 FPS', fontsize=9,
            color=COLOR_30FPS, va='center')

    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_xlabel('Number of trips animated (log)', fontsize=11)
    ax.set_ylabel('FPS median (log, with CI95 envelope)', fontsize=11)

    add_title(ax,
              'FPS scaling: QGIS C6 (memory layer) vs Flask + MapLibre WebGL',
              'STIB Brussels transit, 60s wall-clock per run, drop run 1, bootstrap CI95 (N=1000)')

    ax.legend(loc='lower left', fontsize=10.5)
    ax.set_xlim(min(eff_n(l) for l in all_limits) * 0.7,
                eff_n(all_limits[-1]) * 1.5)
    ax.grid(True, which='both', alpha=0.22)

    plt.tight_layout()
    plt.subplots_adjust(top=0.85)
    save(fig, 'fig1_fps_vs_n')


# -----------------------------------------------------------------------------
# Fig 2 — Load phase comparison (median par limit, bars groupées)
# -----------------------------------------------------------------------------
def fig2_load_phase():
    fig, ax = plt.subplots(figsize=(12, 5.5))

    common_limits = [l for l in all_limits
                     if data.get(('qgis', l)) and data.get(('flask', l))]
    if not common_limits:
        print("  fig2: no common limits, skipping")
        return

    x = np.arange(len(common_limits))
    w = 0.36
    qgis_load  = [data[('qgis',  l)]['load_s'] for l in common_limits]
    flask_load = [data[('flask', l)]['load_s'] for l in common_limits]

    ax.bar(x - w/2, qgis_load,  w, color=COLOR_QGIS, edgecolor='white', linewidth=0.6,
           label='QGIS C6 (matview build + materialize)')
    ax.bar(x + w/2, flask_load, w, color=COLOR_FLASK, edgecolor='white', linewidth=0.6,
           hatch=HATCH_FLASK, label='Flask (SQL fetch + parse JSON)')

    for i, (q, f) in enumerate(zip(qgis_load, flask_load)):
        if q > 0:
            ax.text(i - w/2, q * 1.05, f"{q:.1f}s", ha='center', va='bottom',
                    fontsize=9, color='#333')
        if f > 0:
            ax.text(i + w/2, f * 1.05, f"{f:.1f}s", ha='center', va='bottom',
                    fontsize=9, color='#333')

    ax.set_xticks(x)
    ax.set_xticklabels([data[('qgis', l)]['limit_label'] for l in common_limits])
    ax.set_xlabel('Number of trips', fontsize=11)
    ax.set_ylabel('Load phase total (seconds)', fontsize=11)

    add_title(ax,
              'Load phase: one-shot setup cost before animation can start',
              'QGIS = matview build + indexes + materialize; Flask = SQL fetch + JSON parse client')
    ax.legend(loc='upper left', fontsize=10.5)
    ax.grid(axis='y', alpha=0.22)
    ax.grid(axis='x', visible=False)

    plt.tight_layout()
    plt.subplots_adjust(top=0.85)
    save(fig, 'fig2_load_phase')


# -----------------------------------------------------------------------------
# Fig 3 — Two panels: Sustained FPS + Aggregated RAM peak vs N (log X)
# -----------------------------------------------------------------------------
def fig3_two_panel():
    """Two panels side-by-side: Sustained FPS (linear Y) + RAM peak (linear Y) vs N (log X)."""
    limits_ordered = [100, 250, 500, 1000, 2000, 5000, 10000, 15000, 0]

    def collect(system):
        ns, fps, flo, fhi, rss = [], [], [], [], []
        for lim in limits_ordered:
            m = data.get((system, lim))
            if not m:
                continue
            ns.append(m['n_trips'])
            fps.append(m['fps'])
            flo.append(m['fps_min'])
            fhi.append(m['fps_max'])
            rss.append(m['rss_mb'])
        return (np.array(ns, dtype=float), np.array(fps),
                np.array(flo), np.array(fhi), np.array(rss))

    qn, qfps, qlo, qhi, qrss         = collect('qgis')
    fn, ffps, fflo_a, ffhi_a, frss_a  = collect('flask')

    # Split Flask: monolithic JSON (100–15000) vs all_day (NDJSON streaming, §5.9)
    fn_n,  ffps_n,  fflo_n,  ffhi_n,  frss_n  = (fn[:-1], ffps[:-1],
                                                    fflo_a[:-1], ffhi_a[:-1], frss_a[:-1])
    fn_ad, ffps_ad, fflo_ad, ffhi_ad, frss_ad  = (fn[-1], ffps[-1],
                                                    fflo_a[-1], ffhi_a[-1], frss_a[-1])

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 5.8))

    # ── Panel 1: Sustained FPS ───────────────────────────────────────────────

    # QGIS — line + [fps_min, fps_max] shaded range
    ax1.plot(qn, qfps, '-o', color=COLOR_QGIS, lw=2.5, ms=7,
             mec='white', mew=1.3, label='QGIS C6', zorder=3)
    ax1.fill_between(qn, qlo, qhi, color=COLOR_QGIS, alpha=0.15, zorder=2)

    # Flask — solid segment (monolithic JSON)
    ax1.plot(fn_n, ffps_n, '-o', color=COLOR_FLASK, lw=2.5, ms=7,
             mec='white', mew=1.3, label='Flask + MapLibre WebGL', zorder=3)
    ax1.fill_between(fn_n, fflo_n, ffhi_n, color=COLOR_FLASK, alpha=0.12, zorder=2)

    # Flask — dashed segment to all_day (architectural break)
    ax1.plot([fn_n[-1], fn_ad], [ffps_n[-1], ffps_ad],
             '--', color=COLOR_FLASK, lw=2.0, zorder=3)
    ax1.fill_between([fn_n[-1], fn_ad],
                     [ffhi_n[-1], ffhi_ad], [fflo_n[-1], fflo_ad],
                     color=COLOR_FLASK, alpha=0.10, zorder=2)

    # Star marker for Flask all_day
    ax1.scatter([fn_ad], [ffps_ad], marker='*', s=320,
                color=COLOR_FLASK, zorder=4, ec='white', lw=0.8)

    # Arrow annotation "NDJSON Streaming"
    ax1.annotate('NDJSON Streaming',
                 xy=(fn_ad, ffps_ad),
                 xytext=(fn_ad * 0.38, ffps_ad + 24),
                 fontsize=9, color=COLOR_FLASK, fontweight='bold',
                 ha='center', va='bottom',
                 arrowprops=dict(arrowstyle='->', color=COLOR_FLASK,
                                 lw=1.5, shrinkA=0, shrinkB=6))

    # 60 FPS budget line
    ax1.axhline(60, color=COLOR_60FPS, ls='--', lw=1.5, alpha=0.85,
                zorder=1, label='60 FPS Budget')

    ax1.set_xscale('log')
    ax1.set_xlabel('Animated Trips (N, log scale)', fontsize=11)
    ax1.set_ylabel('Sustained FPS (median, 4 runs)', fontsize=11)
    ax1.legend(fontsize=10, loc='upper right', frameon=False)
    ax1.grid(True, which='both', alpha=0.22)
    add_title(ax1, 'Performance: Sustained FPS vs N')

    # ── Panel 2: Aggregated RAM ───────────────────────────────────────────────

    # QGIS
    ax2.plot(qn, qrss, '-o', color=COLOR_QGIS, lw=2.5, ms=7,
             mec='white', mew=1.3, label='QGIS C6', zorder=3)

    # Flask — solid segment
    ax2.plot(fn_n, frss_n, '-o', color=COLOR_FLASK, lw=2.5, ms=7,
             mec='white', mew=1.3, label='Flask + MapLibre WebGL', zorder=3)

    # Flask — dashed segment to all_day
    ax2.plot([fn_n[-1], fn_ad], [frss_n[-1], frss_ad],
             '--', color=COLOR_FLASK, lw=2.0, zorder=3)

    # Star marker for Flask all_day
    ax2.scatter([fn_ad], [frss_ad], marker='*', s=320,
                color=COLOR_FLASK, zorder=4, ec='white', lw=0.8)

    # Arrow annotation "NDJSON Streaming"
    _rss_ceil = max(qrss.max(), frss_a.max())
    _y_ann    = max(frss_ad + _rss_ceil * 0.08, _rss_ceil * 0.10)
    ax2.annotate('NDJSON Streaming',
                 xy=(fn_ad, frss_ad),
                 xytext=(fn_n[-1] * 0.60, _y_ann),
                 fontsize=9, color=COLOR_FLASK, fontweight='bold',
                 ha='center', va='bottom',
                 arrowprops=dict(arrowstyle='->', color=COLOR_FLASK,
                                 lw=1.5, shrinkA=0, shrinkB=6))

    ax2.set_xscale('log')
    ax2.set_ylim(bottom=0)
    ax2.set_xlabel('Animated Trips (N, log scale)', fontsize=11)
    ax2.set_ylabel('Aggregated Peak RSS (MB)', fontsize=11)
    ax2.legend(fontsize=10, loc='upper left', frameon=False)
    ax2.grid(True, which='both', alpha=0.22)

    # Methodology note (psutil only — no claim about what is or isn't captured)
    ax2.text(0.97, 0.97,
             'Note: RAM averaged\n(psutil sampling @ 500 ms)',
             transform=ax2.transAxes, fontsize=8, color='#555',
             va='top', ha='right', style='italic',
             bbox=dict(boxstyle='round,pad=0.35', fc='white',
                       ec='#ccc', alpha=0.85))

    add_title(ax2, 'System Cost: Aggregated RAM vs N')

    # Figure-level footer
    fig.text(0.5, 0.01,
             'Shaded area = [min, max] over 4 runs  |  ★ = NDJSON streaming transport',
             ha='center', va='bottom', fontsize=9, color='#555', style='italic')

    plt.tight_layout()
    plt.subplots_adjust(top=0.88, bottom=0.10, wspace=0.30)
    save(fig, 'fig3_two_panel')


# -----------------------------------------------------------------------------
# Fig 4 — Speedup ratio Flask/QGIS = f(N)
# -----------------------------------------------------------------------------
def fig4_speedup_curve():
    fig, ax = plt.subplots(figsize=(12, 5.5))

    xs, ratios, lo, hi, labs = [], [], [], [], []
    for limit in all_limits:
        q = data.get(('qgis', limit))
        f = data.get(('flask', limit))
        if not q or not f or q['fps'] == 0 or f['fps'] == 0:
            continue
        xs.append(eff_n(limit))
        ratio = f['fps'] / q['fps']
        ratios.append(ratio)
        # CI on ratio via interval arithmetic on ms (frame time)
        # ratio_FPS = (1000/f_ms) / (1000/q_ms) = q_ms / f_ms
        if q['ci95_lo'] > 0 and f['ci95_lo'] > 0:
            lo.append(q['ci95_lo'] / f['ci95_hi'])
            hi.append(q['ci95_hi'] / f['ci95_lo'])
        else:
            lo.append(ratio); hi.append(ratio)
        labs.append(q['limit_label'])

    if not xs:
        print("  fig4: no points, skipping")
        return

    ax.plot(xs, ratios, '-o', color=COLOR_FLASK, linewidth=2.5, markersize=9,
            markeredgecolor='white', markeredgewidth=1.5, zorder=3)
    ax.fill_between(xs, lo, hi, color=COLOR_FLASK, alpha=0.20, zorder=2)

    ax.axhline(1, color=COLOR_30FPS, linestyle='--', lw=1.2, alpha=0.6, zorder=0)
    ax.text(xs[0] * 0.7, 1.05, 'parity\n(same FPS)', fontsize=9, color=COLOR_30FPS,
            style='italic', va='bottom', ha='left')

    for x, r, lab in zip(xs, ratios, labs):
        ax.annotate(f"{r:.1f}×", (x, r * 1.10), fontsize=9.5, color=COLOR_FLASK,
                    fontweight='bold', ha='center', va='bottom')

    ax.set_xscale('log')
    ax.set_yscale('log')
    ax.set_xlabel('Number of trips animated (log)', fontsize=11)
    ax.set_ylabel('Flask FPS / QGIS C6 FPS  (>1 = Flask faster)', fontsize=11)

    add_title(ax,
              'Speedup curve: Flask vs QGIS C6 across dataset sizes',
              'Above 1× = Flask wins ; envelope = CI95 interval arithmetic')

    ax.grid(True, which='both', alpha=0.22)
    ax.set_xlim(min(xs) * 0.7, max(xs) * 1.5)

    plt.tight_layout()
    plt.subplots_adjust(top=0.85)
    save(fig, 'fig4_speedup_curve')


# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
if __name__ == '__main__':
    print(f"\nGenerating RQ2 charts in {OUT_DIR}/")
    print("-" * 60)
    fig1_fps_vs_n()
    fig2_load_phase()
    fig3_two_panel()
    fig4_speedup_curve()
    print("-" * 60)
    print(f"Done.\n")
