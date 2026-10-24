"""Offline comparison of completed, operator-confirmed low-output trials."""
import csv
import json
import os
from pathlib import Path
import sys
from window_quality import assess,summarize

root=Path(__file__).resolve().parent.parent
manifest_path=Path(sys.argv[1]).resolve()
folder=manifest_path.parent
manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
points=[]; configs=[]
def load_point(relative):
    path=root/relative
    run=json.loads((path/'result.json').read_text(encoding='utf-8'))
    observation=json.loads((path/'operator_observation.json').read_text(encoding='utf-8'))
    assert observation['physical_stop_confirmed'] and observation['clamps_secure_without_hand_restraint']
    assert run['status']=='software_checks_passed_physical_confirmation_pending'
    assert run['reported_output_returned_zero'] and run['stop_commands_acknowledged']
    assert run['other_channels_zero'] and len(run['sweep_steps'])==1
    step=run['sweep_steps'][0]; control=step['control']
    target=control.get('target_window') or {'started_monotonic_s':control['started_monotonic_s'],'seconds':control['requested_seconds']}
    assert target['seconds']>=15
    configs.append(json.loads((Path(run['cv_log_folder'])/'config.json').read_text(encoding='utf-8')))
    with (Path(run['cv_log_folder'])/'samples.csv').open(encoding='utf-8-sig',newline='') as source:
        rows=list(csv.DictReader(source))
    assert all(r['run_id']==run['run_id'] for r in rows)
    for row in rows:
        row['relative_seconds']=int(row['captured_monotonic_ns'])/1e9-target['started_monotonic_s']
    common=[r for r in rows if 5<=r['relative_seconds']<15]
    quality=assess(rows,15)
    baseline_path=path/f"step_{step['index']+1}_baseline.json"
    baseline=json.loads(baseline_path.read_text(encoding='utf-8')) if baseline_path.exists() else run['baseline']
    point={'value':step['value'],'source_run':relative,'target_seconds':target['seconds'],
           'baseline_raw_fields':{f['id']:f.get('value') for f in baseline['fields']},
           'baseline_thrust_policy':run.get('baseline_thrust_policy','legacy_exact_digit_stability'),
           'process_abort_limits':run.get('process_abort_limits'),
           'process_policy':run.get('process_policy','legacy_recorded_limits'),
           'warmup_seconds':control['requested_seconds']-target['seconds'],
           'fields':{k:summarize(common,k) for k in ('upper','voltage','current','power')},
           'quality_in_common_window':quality,
           'max_observed_cv_age_seconds':run['max_observed_cv_age_seconds']}
    # A flat zero is never accepted as a motor-performance point.
    assert point['fields']['current']['mean']>.02 and point['fields']['power']['mean']>.5
    return point
points=[load_point(relative) for relative in manifest['source_runs']]
repeats=[load_point(relative) for relative in manifest.get('repeat_runs',[])]
points.sort(key=lambda p:p['value'])
assert [p['value'] for p in points]==manifest['expected_levels']
assert all(c==configs[0] for c in configs)
hardware_path=folder/'hardware_context.json'
summary={'manifest':manifest,'analysis_window_seconds':[5,15],'points':points,
         'repeat_points':repeats,
         'hardware_context':json.loads(hardware_path.read_text(encoding='utf-8')) if hardware_path.exists() else None,
         'accuracy_established':False,'missing_values_filled':False,
         'full_range_performance_measured':False}
(folder/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
os.environ['MPLCONFIGDIR']=str(folder/'plot_cache')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
fig,axes=plt.subplots(1,3,figsize=(12,4),layout='constrained')
for ax,key,title in zip(axes,('upper','current','power'),('Thrust display (g)','Current display (A)','Power display (W)')):
    xs=[p['value'] for p in points]; means=[p['fields'][key]['mean'] for p in points]
    errors=[[m-p['fields'][key]['min'] for p,m in zip(points,means)],
            [p['fields'][key]['max']-m for p,m in zip(points,means)]]
    ax.errorbar(xs,means,yerr=errors,fmt='o-',capsize=4,label='Original points')
    for repeat_index,p in enumerate(repeats):
        f=p['fields'][key]; m=f['mean']
        ax.errorbar([p['value']],[m],yerr=[[m-f['min']],[f['max']-m]],
                    fmt='D',capsize=4,color='#c05c21',label='Repeat trial' if repeat_index==0 else '_nolegend_')
    if repeats: ax.legend(fontsize=8)
    ax.set_xticks(xs); ax.set_xlabel('Normalized output command'); ax.set_ylabel(title); ax.grid(alpha=.2)
fig.suptitle('Exploratory low-output comparison: seconds 5-15 at target; bars show observed min/max')
fig.savefig(folder/'curves.png',dpi=150); plt.close(fig)
lines=['# 缓升后的低输出测点比较','',
       '统一统计每个目标档开始后第5～15秒。预转时间不混入目标档统计，缺测不补值。0.025档采用此前第二次20秒运行；其他档采用本轮缓升后15秒运行。全部是独立会话，非连续扫档。','',
       '| 指令值 | 拉力均值 g | 拉力范围 g | 电压均值 V | 电流均值 A | 功率均值 W | 拉力/电流/功率有效样本 |',
       '|---|---:|---|---:|---:|---:|---|']
for p in points:
    f=p['fields']; t=f['upper']
    counts='；'.join(f"{f[k]['valid_samples']}/{f[k]['samples']}" for k in ('upper','current','power'))
    lines.append(f"| {p['value']:g} | {t['mean']:.2f} | {t['min']:g}～{t['max']:g} | {f['voltage']['mean']:.2f} | {f['current']['mean']:.3f} | {f['power']['mean']:.3f} | {counts} |")
lines+=['','各点均有正常停转和夹具牢固的现场确认。各轮实际过程门槛按原始记录分别列出；CV有效性和指令期限检查继续保留。各点均值与范围均为即时原始显示值，没有自动扣除启动前零点。',
        '0.030直接启动曾因0.38 A／9.39 W中止；缓升短测及长测另行保留。缓升成功只说明这几次运行在本轮门槛内完成，尚不能确定先前超限的物理原因。',
        '本表仅比较仪表显示值。相同统计窗口有助于比较，但启动路径及此前热状态并不完全相同；没有独立校准、RPM或温度记录，不作为标准性能认证或全量程外推。',
        '此前0.010～0.025的10秒试验按原报告保留；此处没有将不同时间窗口的数据静默混算。','',
        '实际电调和供电额定能力仍未确认。最大已测指令不等于设备允许上限，软件输入范围也不作为硬件额定能力证明。','',
        f"[曲线图]({(folder/'curves.png').as_posix()}) · [统计与证据索引]({(folder/'summary.json').as_posix()})",'']
lines+=['## 各轮过程门槛','', '| 档位 | 类型 | 电流 A | 功率 W | 绝对拉力 g |','|---|---|---:|---:|---:|']
for p,kind in [(p,'原点') for p in points]+[(p,'重复点') for p in repeats]:
    limits=p['process_abort_limits']
    if limits:
        lines.append(f"| {p['value']:g} | {kind} | {limits['current_A']:g} | {limits['power_W']:g} | {limits['absolute_thrust_g']:g} |")
if repeats:
    lines+=['## 重复测点（不替换或混算原点）','']
    for p in repeats:
        original=next(v for v in points if v['value']==p['value'])
        f=p['fields']; previous=original['fields']
        lines.append(f"输出{p['value']:g}：原点拉力{previous['upper']['mean']:.2f} g，重复点{f['upper']['mean']:.2f} g；重复点电压{f['voltage']['mean']:.2f} V、电流{f['current']['mean']:.3f} A、功率{f['power']['mean']:.3f} W。")
        lines.append(f"重复点单独标为橙色菱形，原点与原连线保留。[重复点原始记录]({(root/p['source_run']/'result.json').as_posix()})")
if manifest.get('trial_notes'):
    lines+=['## 本轮过程记录','',*manifest['trial_notes'],'']
for p in points:
    lines.append(f"- 输出{p['value']:g}：[原始运行记录]({(root/p['source_run']/'result.json').as_posix()})")
(folder/'实测报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'levels':[p['value'] for p in points],'folder':str(folder)},ensure_ascii=False))
