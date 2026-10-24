"""Consolidate every completed throttle point and draw the campaign curves.

High-range means/ranges come from the already published per-run statistics in
high_range_campaign_20261018/全档位数据.csv. The extreme-range points are
recomputed here from each run's own target window. Peaks are recomputed from
observed_cv.jsonl for every run that stores a target window.
"""
import csv
import json
import math
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / 'integration_runs'
OUT = Path(__file__).resolve().parent
CHART = OUT / '曲线图'
SOURCE_CSV = RUNS / 'high_range_campaign_20261018' / '全档位数据.csv'

POLICY_HIGH = '10 A / 240 W（硬 11 A / 264 W，1000 g）'
POLICY_20 = '20 A / 480 W（硬 22 A / 528 W，2500 g）'
POLICY_25 = '25 A / 600 W（硬 27 A / 648 W，2500 g）'

EXTREME_RUNS = [
    (0.70, '20261018_171131_step0.7_10s_2f1284', POLICY_20),
    (0.75, '20261018_191343_step0.75_10s_cd2fcc', POLICY_25),
    (0.80, '20261018_171300_step0.8_10s_abaa83', POLICY_20),
    (0.85, '20261018_191708_step0.85_10s_c97335', POLICY_25),
    (0.90, '20261018_172006_step0.9_10s_8d86cb', POLICY_25),
]


def target_rows(run):
    """Yield (thrust, voltage, current) inside the run's own target window."""
    folder = RUNS / run
    result = json.loads((folder / 'result.json').read_text(encoding='utf-8'))
    step = (result.get('sweep_steps') or [{}])[0]
    window = (step.get('control') or {}).get('target_window')
    if not window:
        return None
    start, end = window['started_monotonic_s'], window['deadline_monotonic_s']
    rows = []
    for line in (folder / 'observed_cv.jsonl').read_text(encoding='utf-8').splitlines():
        sample = json.loads(line)
        stamp = sample['captured_monotonic_ns'] / 1e9
        if not start <= stamp <= end:
            continue
        fields = {f['id']: f for f in sample['fields']}
        try:
            thrust = float(fields['upper']['value'])
            voltage = float(fields['voltage']['value'])
            current = float(fields['current']['value'])
        except (KeyError, TypeError, ValueError):
            continue
        if not all(fields[k].get('valid') for k in ('upper', 'voltage', 'current')):
            continue
        rows.append((thrust, voltage, current))
    return rows


def extreme_stats(run):
    rows = target_rows(run)
    thrust = [r[0] for r in rows]
    volts = [r[1] for r in rows]
    amps = [r[2] for r in rows]
    watts = [v * i for v, i in zip(volts, amps)]
    return {
        'n': len(rows),
        'T': sum(thrust) / len(thrust),
        'Tmin': min(thrust),
        'Tmax': max(thrust),
        'V': sum(volts) / len(volts),
        'I': sum(amps) / len(amps),
        'Imax': max(amps),
        'P': sum(watts) / len(watts),
        'Pmax': max(watts),
    }


def peaks(run):
    rows = target_rows(run)
    if not rows:
        return None, None
    amps = [r[2] for r in rows]
    watts = [r[1] * r[2] for r in rows]
    return max(amps), max(watts)


def load_rows():
    rows = []
    with SOURCE_CSV.open(encoding='utf-8-sig', newline='') as handle:
        for record in csv.DictReader(handle):
            throttle = float(record['throttle'])
            peak_i, peak_p = peaks(record['run'])
            target = target_rows(record['run'])
            rows.append({
                'throttle': throttle,
                'segment': '高油门段' if throttle >= 0.15 else '低油门段',
                'run': record['run'],
                'n': None if target is None else len(target),
                'hold': 20.0 if throttle == 0.025 else 15.0,
                'T': float(record['thrust_mean_g']),
                'Tmin': float(record['thrust_min_g']),
                'Tmax': float(record['thrust_max_g']),
                'V': float(record['voltage_V']),
                'I': float(record['current_A']),
                'Imax': peak_i,
                'P': float(record['power_W']),
                'Pmax': peak_p,
                'eff': float(record['thrust_per_power_g_per_W']),
                'policy': POLICY_HIGH,
                'note': '',
            })
    for throttle, run, policy in EXTREME_RUNS:
        stats = extreme_stats(run)
        rows.append({
            'throttle': throttle,
            'segment': '极高油门段',
            'run': run,
            'n': stats['n'],
            'hold': 10.0,
            'T': stats['T'], 'Tmin': stats['Tmin'], 'Tmax': stats['Tmax'],
            'V': stats['V'], 'I': stats['I'], 'Imax': stats['Imax'],
            'P': stats['P'], 'Pmax': stats['Pmax'],
            'eff': stats['T'] / stats['P'],
            'policy': policy,
            'note': '10 秒目标保持' if throttle != 0.90 else '旧门槛下曾于 22.1 A 中止；本点为提高门槛后完成',
        })
    return rows


def write_csv(rows):
    path = OUT / '全档位数据_合并.csv'
    fields = ['throttle', 'throttle_pct', 'segment', 'run', 'target_samples', 'hold_seconds',
              'thrust_mean_g', 'thrust_min_g', 'thrust_max_g', 'voltage_V',
              'current_mean_A', 'current_peak_A', 'power_mean_W', 'power_peak_W',
              'efficiency_g_per_W', 'policy', 'note']
    with path.open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                'throttle': row['throttle'],
                'throttle_pct': round(row['throttle'] * 100, 1),
                'segment': row['segment'],
                'run': row['run'],
                'target_samples': '' if row['n'] is None else row['n'],
                'hold_seconds': row['hold'],
                'thrust_mean_g': round(row['T'], 2),
                'thrust_min_g': row['Tmin'],
                'thrust_max_g': row['Tmax'],
                'voltage_V': round(row['V'], 2),
                'current_mean_A': round(row['I'], 3),
                'current_peak_A': '' if row['Imax'] is None else round(row['Imax'], 2),
                'power_mean_W': round(row['P'], 2),
                'power_peak_W': '' if row['Pmax'] is None else round(row['Pmax'], 2),
                'efficiency_g_per_W': round(row['eff'], 3),
                'policy': row['policy'],
                'note': row['note'],
            })
    return path


def markdown_table(rows):
    lines = ['| 指令 | 油门 | 拉力均值 g | 拉力范围 g | 电压 V | 电流均值 A | 电流峰值 A | 功率均值 W | 功率峰值 W | 效率 g/W | 运行 |',
             '|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|']
    for row in sorted(rows, key=lambda r: r['throttle']):
        peak_i = '—' if row['Imax'] is None else f"{row['Imax']:.2f}"
        peak_p = '—' if row['Pmax'] is None else f"{row['Pmax']:.1f}"
        lines.append('| {throttle:g} | {pct:.1f}% | {T:.2f} | {lo}–{hi} | {V:.2f} | {I:.3f} | {ip} | {P:.2f} | {pp} | {eff:.2f} | `{run}` |'.format(
            throttle=row['throttle'], pct=row['throttle'] * 100, T=row['T'],
            lo=round(row['Tmin']), hi=round(row['Tmax']), V=row['V'], I=row['I'],
            ip=peak_i, P=row['P'], pp=peak_p, eff=row['eff'], run=row['run']))
    return '\n'.join(lines)


# --------------------------------------------------------------------------- charts
plt.rcParams.update({
    'font.sans-serif': ['Microsoft YaHei', 'SimHei'],
    'axes.unicode_minus': False,
    'figure.dpi': 150,
    'savefig.dpi': 200,
    'axes.grid': True,
    'grid.alpha': 0.28,
    'grid.linestyle': '--',
    'axes.spines.top': False,
    'axes.spines.right': False,
    'font.size': 9,
})

COLORS = {'T': '#c0392b', 'I': '#1f6fb2', 'P': '#7d3c98', 'eff': '#1e8449'}
LABELS = {'T': '拉力 (g)', 'I': '电流 (A)', 'P': '功率 (W)', 'eff': '推力效率 (g/W)'}


def main_series(rows):
    """One record per throttle: the first 0.060 record, others as-is."""
    seen = {}
    for row in sorted(rows, key=lambda r: (r['throttle'], r['run'])):
        seen.setdefault(row['throttle'], row)
    return [seen[key] for key in sorted(seen)]


def draw_metric(ax, rows, metric, logx=False, limits=True):
    main = main_series(rows)
    xs = [r['throttle'] * 100 for r in main]
    ys = [r[metric] for r in main]
    ax.plot(xs, ys, color=COLORS[metric], lw=1.8, zorder=3,
            marker='o', ms=4.2, mfc='white', mec=COLORS[metric], mew=1.2)
    # Extremes as filled squares so the higher-power segment is unmistakable.
    ext = [r for r in main if r['segment'] == '极高油门段']
    ax.plot([r['throttle'] * 100 for r in ext], [r[metric] for r in ext],
            linestyle='none', marker='s', ms=6, color=COLORS[metric], zorder=4)
    if logx:
        ax.set_xscale('log')
    if limits and metric == 'I':
        ax.plot([2.5, 62.5], [10, 10], color='#95a5a6', lw=1, ls=':')
        ax.plot([62.5, 85], [20, 20], color='#95a5a6', lw=1, ls=':')
        ax.plot([85, 92], [25, 25], color='#95a5a6', lw=1, ls=':')
    if limits and metric == 'P':
        ax.plot([2.5, 62.5], [240, 240], color='#95a5a6', lw=1, ls=':')
        ax.plot([62.5, 85], [480, 480], color='#95a5a6', lw=1, ls=':')
        ax.plot([85, 92], [600, 600], color='#95a5a6', lw=1, ls=':')
    ax.set_ylabel(LABELS[metric], color='#333333')


def annotate(ax, row, metric, text, dx=1.5, dy=0):
    ax.annotate(text, (row['throttle'] * 100, row[metric]),
                textcoords='offset points', xytext=(6, 8), fontsize=8,
                color=COLORS[metric])


def policy_guides(ax):
    for x in (62.5, 85):
        ax.axvline(x, color='#b0b0b0', lw=0.8, ls=(0, (4, 4)), zorder=1)


def overview(rows):
    fig, axes = plt.subplots(2, 2, figsize=(11, 7.2), constrained_layout=True)
    for ax, metric in zip(axes.flat, ('T', 'I', 'P', 'eff')):
        draw_metric(ax, rows, metric)
        policy_guides(ax)
        ax.set_xlim(0, 93)
        ax.set_xlabel('油门指令 (%)')
        if metric == 'T':
            ax.set_ylim(0, 950)
        if metric == 'eff':
            ax.set_ylim(0, 3.5)
    main = main_series(rows)
    top = {r['throttle']: r for r in main}
    for metric, ax in zip(('T', 'I', 'P', 'eff'), axes.flat):
        row = top[0.9]
        value = row[metric]
        text = f'{value:.0f}' if metric in ('T', 'P') else f'{value:.2f}'
        ax.annotate(text, (90, value), textcoords='offset points',
                    xytext=(-16, 9), fontsize=8, color=COLORS[metric])
    fig.suptitle('当前电机 + D90S-3 三叶桨：0.025–0.900 油门实测曲线\n'
                 '虚线为各段电限（10 A/240 W → 20 A/480 W → 25 A/600 W）；方点为 0.70–0.90 极高段',
                 fontsize=12)
    path = CHART / '01_全段四联图.png'
    fig.savefig(path)
    plt.close(fig)
    return path


def single(rows, metric, filename, title, ylimit=None, logx=True):
    fig, ax = plt.subplots(figsize=(8.2, 4.8), constrained_layout=True)
    draw_metric(ax, rows, metric, logx=logx)
    policy_guides(ax)
    if logx:
        ticks = [2.5, 5, 10, 20, 30, 40, 60, 80]
        ax.set_xticks(ticks)
        ax.set_xticklabels([f'{t:g}%' for t in ticks])
        ax.set_xlim(2.2, 95)
    else:
        ax.set_xlim(0, 93)
    if ylimit:
        ax.set_ylim(*ylimit)
    ax.set_xlabel('油门指令 (%)')
    ax.set_ylabel(LABELS[metric], color='#333333')
    ax.set_title(title)
    main = main_series(rows)
    for throttle in (0.15, 0.60, 0.70, 0.80, 0.90):
        row = next(r for r in main if r['throttle'] == throttle)
        value = row[metric]
        text = f'{value:.0f}' if metric in ('T', 'P') else f'{value:.2f}'
        # 0.80 and 0.85 sit close together on the log axis; keep the 0.80
        # label to the left so the 0.85 marker cannot cover it.
        offset = (-30, 5) if throttle == 0.80 else (7, 8)
        ax.annotate(text, (row['throttle'] * 100, value),
                    textcoords='offset points', xytext=offset, fontsize=8,
                    color=COLORS[metric])
    path = CHART / filename
    fig.savefig(path)
    plt.close(fig)
    return path


def thrust_power(rows):
    fig, ax = plt.subplots(figsize=(7.4, 5.2), constrained_layout=True)
    main = main_series(rows)
    xs = [r['P'] for r in main]
    ys = [r['T'] for r in main]
    ax.plot(xs, ys, color='#555555', lw=1.2, zorder=2)
    sc = ax.scatter(xs, ys, c=[r['throttle'] * 100 for r in main], cmap='viridis',
                    s=42, zorder=3, edgecolor='white', linewidth=0.6)
    for throttle in (0.15, 0.60, 0.70, 0.80, 0.90):
        row = next(r for r in main if r['throttle'] == throttle)
        ax.annotate(f"{throttle:g}  ({row['T']:.0f} g / {row['P']:.0f} W)",
                    (row['P'], row['T']), textcoords='offset points',
                    xytext=(7, 6), fontsize=8)
    cb = fig.colorbar(sc, ax=ax, pad=0.02)
    cb.set_label('油门指令 (%)')
    ax.set_xlabel('功率 (W, V×I)')
    ax.set_ylabel('拉力 (g)')
    ax.set_title('拉力—功率关系（点色表示油门）')
    path = CHART / '06_拉力-功率.png'
    fig.savefig(path)
    plt.close(fig)
    return path


def low_zoom(rows):
    main = main_series(rows)
    low = [r for r in main if r['throttle'] <= 0.20]
    retest = next(r for r in rows if r['throttle'] == 0.06 and r['T'] > 22)
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(10.5, 4.4), constrained_layout=True)
    for ax, metric in ((ax1, 'T'), (ax2, 'eff')):
        xs = [r['throttle'] * 100 for r in low]
        ax.plot(xs, [r[metric] for r in low], color=COLORS[metric], lw=1.6,
                marker='o', ms=4, mfc='white', mec=COLORS[metric])
        ax.plot([6.0], [retest[metric]], linestyle='none', marker='D', ms=6,
                mfc='none', mec='#7f8c8d', mew=1.3, zorder=4)
        ax.annotate(f'0.060 复测 {retest[metric]:.1f}', (6.0, retest[metric]),
                    textcoords='offset points', xytext=(10, 9), fontsize=7,
                    color='#7f8c8d',
                    bbox=dict(boxstyle='round,pad=0.15', fc='white', ec='none', alpha=0.85))
        ax.set_xlabel('油门指令 (%)')
        ax.set_ylabel(LABELS[metric], color='#333333')
        ax.set_xlim(1.8, 20.5)
    ax1.set_title('低油门段拉力（0.025–0.200；菱形为 0.060 复测）')
    ax2.set_title('低油门段效率（0.025–0.200）')
    path = CHART / '07_低油门段放大.png'
    fig.savefig(path)
    plt.close(fig)
    return path


def main():
    rows = load_rows()
    csv_path = write_csv(rows)
    table = markdown_table(rows)
    (OUT / '数据表_生成.md').write_text(table + '\n', encoding='utf-8')
    paths = [
        overview(rows),
        single(rows, 'T', '02_拉力-油门.png', '拉力—油门曲线（对数横轴）', ylimit=(0, 950)),
        single(rows, 'I', '03_电流-油门.png', '电流—油门曲线（对数横轴）', ylimit=(0, 26)),
        single(rows, 'P', '04_功率-油门.png', '功率—油门曲线（对数横轴）', ylimit=(0, 650)),
        single(rows, 'eff', '05_效率-油门.png', '推力效率—油门曲线（对数横轴）', ylimit=(0, 3.5)),
        thrust_power(rows),
        low_zoom(rows),
    ]
    print('CSV:', csv_path)
    for path in paths:
        print('PNG:', path)


if __name__ == '__main__':
    main()
