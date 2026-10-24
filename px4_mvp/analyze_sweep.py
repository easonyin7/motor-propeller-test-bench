"""Plot completed saved sweep steps; never connects to hardware or fills missing data."""
import csv
import json
import math
import os
from pathlib import Path
import statistics
import sys

root=Path(__file__).resolve().parent.parent
manifest_path=Path(sys.argv[1]).resolve()
folder=manifest_path.parent
manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
hardware_path=folder/'hardware_context.json'
hardware=json.loads(hardware_path.read_text(encoding='utf-8')) if hardware_path.exists() else {}
labels={'upper':('拉力','g'),'voltage':('电压','V'),'current':('电流','A'),
        'power':('功率','W'),'energy':('电能','Wh')}
points=[]
series=[]
configs=[]

def number(row,key,stable=False):
    value=row.get(key+('_stable' if stable else '_raw'))
    return float(value) if row.get(key+'_valid')=='True' and value else None

def stats(rows,key):
    values=[n for r in rows if (n:=number(r,key)) is not None]
    confirmed=[n for r in rows if (n:=number(r,key,True)) is not None]
    return {'samples':len(rows),'readable_samples':len(values),'stable_samples':len(confirmed),
            'mean':statistics.mean(values) if values else None,
            'median':statistics.median(values) if values else None,
            'min':min(values) if values else None,'max':max(values) if values else None,
            'std':statistics.pstdev(values) if values else None}

for relative in manifest['source_runs']:
    run_folder=root/relative
    run=json.loads((run_folder/'result.json').read_text(encoding='utf-8'))
    observation=json.loads((run_folder/'operator_observation.json').read_text(encoding='utf-8'))
    if not (observation.get('physical_stop_confirmed') and observation.get('clamps_secure_without_hand_restraint')):
        raise ValueError('Physical stop/fixation confirmation is missing.')
    cv_folder=Path(run['cv_log_folder'])
    configs.append(json.loads((cv_folder/'config.json').read_text(encoding='utf-8')))
    with (cv_folder/'samples.csv').open(encoding='utf-8-sig',newline='') as stream:
        rows=list(csv.DictReader(stream))
    for step in run['sweep_steps']:
        control=step['control']
        if not (step['reported_output_returned_zero'] and control['stop_commands_acknowledged'] and control['requested_seconds']==10):
            raise ValueError('A listed step was not completed/stopped as expected.')
        start=control['started_monotonic_s']
        end=control['deadline_monotonic_s']
        running=[]; inspection=[]; trace=[]
        for row in rows:
            t=int(row['captured_monotonic_ns'])/1e9-start
            sample={**row,'normalized_output':step['value'],'relative_seconds':t,'source_run':relative}
            if -1<=t<=13:
                trace.append(sample)
                series.append(sample)
            if 0<=t<end-start:
                running.append(sample)
                if t>=2: inspection.append(sample)
        if not inspection:
            raise ValueError('No samples in a completed step inspection window.')
        baseline=json.loads((run_folder/f"step_{step['index']+1}_baseline.json").read_text(encoding='utf-8'))
        baseline_fields={f['id']:f for f in baseline['fields']}
        paired=[(number(r,'power'),number(r,'upper')) for r in inspection
                if number(r,'power') is not None and number(r,'upper') is not None]
        mean_power=statistics.mean(p[0] for p in paired) if paired else None
        mean_thrust=statistics.mean(p[1] for p in paired) if paired else None
        points.append({'value':step['value'],'source_run':relative,'cv_log_folder':str(cv_folder),
            'control':control,'raw_output_values':step['raw_output_values'],
            'max_cv_age_seconds':run['max_observed_cv_age_seconds'],
            'baseline_thrust_g':float(baseline_fields['upper']['stable_value']),
            'window_fields':{k:stats(inspection,k) for k in labels},
            'running_fields':{k:stats(running,k) for k in labels},
            'paired_power_thrust':{'samples':len(paired),'mean_power_W':mean_power,'mean_thrust_g':mean_thrust,
                'display_g_per_W':mean_thrust/mean_power if mean_power and mean_power>0 else None},
            'trace':trace})

points.sort(key=lambda p:p['value'])
assert [p['value'] for p in points]==manifest['expected_levels'],'Missing or duplicated output level.'
assert all(c==configs[0] for c in configs),'Instrument configurations differ between runs.'
summary={'manifest':manifest,'hardware_context':hardware,'accuracy_measured':False,
    'window_note':'Seconds 2..10 are an inspection window, not automatic proof of a steady physical condition.',
    'points':[{k:v for k,v in p.items() if k!='trace'} for p in points]}
summary['findings']={
    'current_and_power_means_increase':all(points[i+1]['window_fields'][k]['mean']>points[i]['window_fields'][k]['mean']
        for i in range(len(points)-1) for k in ('current','power')),
    'thrust_means_monotonic':all(points[i+1]['window_fields']['upper']['mean']>=points[i]['window_fields']['upper']['mean'] for i in range(len(points)-1)),
    'interpretation':'The 0.015 thrust mean is slightly below 0.01 with overlapping observed ranges. Repeatability/calibration remain unresolved; do not smooth away or interpret this as proven motor performance loss.'}
(folder/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
columns=['normalized_output','baseline_thrust_g']+[k+'_'+m for k in labels for m in ('mean','median','min','max','std','readable_samples','samples')]+['paired_display_g_per_W','source_run']
with (folder/'curve_points.csv').open('w',encoding='utf-8-sig',newline='') as stream:
    writer=csv.DictWriter(stream,columns)
    writer.writeheader()
    for p in points:
        writer.writerow({'normalized_output':p['value'],'baseline_thrust_g':p['baseline_thrust_g'],
            **{k+'_'+m:p['window_fields'][k][m] for k in labels for m in ('mean','median','min','max','std','readable_samples','samples')},
            'paired_display_g_per_W':p['paired_power_thrust']['display_g_per_W'],'source_run':p['source_run']})
with (folder/'time_series.csv').open('w',encoding='utf-8-sig',newline='') as stream:
    columns=['normalized_output','relative_seconds','captured_at','source_run']+[k+'_'+s for k in labels for s in ('raw','stable','valid','reason')]
    writer=csv.DictWriter(stream,columns,extrasaction='ignore')
    writer.writeheader(); writer.writerows(series)

os.environ['MPLCONFIGDIR']=str(folder/'plot_cache')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

fig,axes=plt.subplots(2,2,figsize=(10,7),layout='constrained')
for ax,key,title in zip(axes.flat,('upper','current','power'),('Thrust display (g)','Current display (A)','Power display (W)')):
    means=[p['window_fields'][key]['mean'] for p in points]
    errors=[[m-p['window_fields'][key]['min'] for m,p in zip(means,points)],
            [p['window_fields'][key]['max']-m for m,p in zip(means,points)]]
    ax.errorbar([p['value'] for p in points],means,yerr=errors,fmt='o-',capsize=5,color='#176e9c')
    ax.set_xlabel('Normalized output command'); ax.set_ylabel(title)
    ax.set_xticks(manifest['expected_levels']); ax.grid(alpha=.2)
ax=axes[1,1]
ax.plot([p['paired_power_thrust']['mean_power_W'] for p in points],
        [p['paired_power_thrust']['mean_thrust_g'] for p in points],'o-',color='#bf641e')
for p in points:
    xy=(p['paired_power_thrust']['mean_power_W'],p['paired_power_thrust']['mean_thrust_g'])
    ax.annotate(f" {p['value']:g}",xy,xytext=(5,6),textcoords='offset points',fontsize=9)
ax.set_xlabel('Mean power display (W), paired samples'); ax.set_ylabel('Mean thrust display (g)'); ax.grid(alpha=.2)
fig.suptitle('Clamped bench: initial low-output curve\nSeconds 2-10; bars show observed min/max; one trial per level',fontsize=11)
fig.savefig(folder/'curves.png',dpi=160); plt.close(fig)

fig,axes=plt.subplots(3,1,figsize=(10,7),sharex=True,layout='constrained')
for p in points:
    trace=p['trace']
    for ax,key,title in zip(axes,('upper','current','power'),('Thrust display (g)','Current display (A)','Power display (W)')):
        xs=[r['relative_seconds'] for r in trace]
        ys=[number(r,key) if number(r,key) is not None else math.nan for r in trace]
        ax.plot(xs,ys,'o-',markersize=2,linewidth=1,label=f"Output {p['value']:g}")
        ax.set_ylabel(title); ax.grid(alpha=.2)
for ax in axes:
    ax.axvline(0,color='gray',linestyle='--'); ax.axvline(10,color='gray',linestyle='--')
axes[0].legend(); axes[0].set_title('Each measured step aligned to its own command start')
axes[-1].set_xlabel('Host capture time relative to each step start (s)')
fig.savefig(folder/'time_series.png',dpi=160); plt.close(fig)

lines=['# 夹具固定后的电机桨叶低输出曲线','',
       f"日期：2026-10-17（北京时间）。**{len(points)}个档位均已实测，每档10秒；用户分别确认正常启停、无明显异常、夹具牢固且无需手扶。**",'',
       '归一化输出档位：'+ '、'.join(f"{p['value']:g}" for p in points)+'。本报告仅覆盖所列低输出测点，不能据此评价全量程额定性能。','',
       '## 分档结果','',
       '统计各档第2～10秒的即时可读样本，缺测保留。窗口选择不等同于已经确认物理稳态。表中±值为本窗口总体标准差，不是仪表精度或置信区间。','',
       '| 输出 | 拉力均值±标准差（g） | 拉力范围（g） | 电压均值（V） | 电流均值（A） | 功率均值（W） | 拉力/功率可读样本 |',
       '|---|---:|---|---:|---:|---:|---|']
for p in points:
    f=p['window_fields']; t=f['upper']
    lines.append(f"| {p['value']:g} | {t['mean']:.2f} ± {t['std']:.2f} | {t['min']:g}～{t['max']:g} | "
                 f"{f['voltage']['mean']:.2f} | {f['current']['mean']:.3f} | {f['power']['mean']:.3f} | "
                 f"{t['readable_samples']}/{t['samples']}；{f['power']['readable_samples']}/{f['power']['samples']} |")
if hardware:
    if hardware.get('product_motor_specs'):
        lines += ['','用户后续提供商品资料：电机2006 / 2150 KV，图示25.2 V（6S）、最大21.81 A / 523.44 W、建议30 A电调；桨叶图为D90S-3，90 mm、三叶、3 in螺距。实际电调额定值仍未知；厂家参考表使用GF D90-3，与D90S-3的等价性未确认。详细转录与测试影响见 `integration_runs/hardware_reference_20261017/规格核对.md`。本报告最大已测输出为'+f"{max(manifest['expected_levels']):g}"+'，不代表已经确定设备允许上限。']
    else:
        lines += ['','电机标识由用户补充为 **2006 / 2150 KV**。品牌及精确产品型号未核实；电调额定电流、桨叶尺寸/型号未知。这个标识本身不足以确定更高输出、电流或允许转速，暂不扩大本轮范围。']
lines += ['', '## 读数中的待核实问题','',
          '电流、功率均值随输出上升；拉力均值在0.015档略低于0.01档，两档观察范围重叠。保留这个非单调结果，不平滑成预期曲线，不据此判定真实性能下降。后续优先验证拉力测量重复性、零点/加载标定与受力路径，再决定是否扩大量程。',
          '最初三档各有一张运行中照片的人工目视核对，所见读数与识别快照相符；这只是有限照片的抽查，不能换算为全帧准确率。新增档位的照片检查另行记录。',
          '', '## 执行经过与边界','',manifest['continuity_note'],
          '前两档在同一会话完成。第三档因停机后的电流尚未通过逐字稳定检查而未启动。调整为最多4秒的有上限等待后，仅补测第三档；没有重复前两档，也没有把中止会话丢弃。',
          '前两档之间停机至少5秒；不同会话的测点之间停机更久。各档采用相同仪表配置，停机基线单独保存。','',
          '每条运行指令有效期不超过1秒，按总期限续发，任何故障阻止后续档位。每档停止ACK及飞控回零均记录；实际停转由现场用户确认，不用遥测计时替代物理停止时间。',
          '本轮过程边界为电流0.35 A、功率9 W、绝对拉力25 g；它们是本轮保守中止门槛，不是设备额定安全限值。数据过期门槛1秒，必需字段连续不可读1.5秒中止。',
          '统计和曲线仅来自夹紧后的数据，没有混入此前手扶条件的0.02点。没有独立计量校准、逐帧真值或RPM数据；图中的g为仪表显示单位。',
          '曲线连线仅用于查看已测点，不作全量程外推。每档仅一次10秒统计，重复性尚未评价。完整量程测试仍需实际电调等额定能力和允许上限。','',
          '## 文件','',
          f'- [分档曲线]({(folder/"curves.png").as_posix()})',
          f'- [每档时间响应]({(folder/"time_series.png").as_posix()})',
          f'- [测点CSV]({(folder/"curve_points.csv").as_posix()})',
          f'- [关联时序CSV]({(folder/"time_series.csv").as_posix()})',
          f'- [完整统计和证据索引]({(folder/"summary.json").as_posix()})']
for p in points:
    lines.append(f"- 输出{p['value']:g}来源：[{p['source_run']}]({(root/p['source_run']/ 'result.json').as_posix()})；相机原始数据：[{Path(p['cv_log_folder']).name}]({(Path(p['cv_log_folder'])/'samples.csv').as_posix()})")
quality_path=folder/'quality_review.json'
if quality_path.exists():
    quality=json.loads(quality_path.read_text(encoding='utf-8'))
    lines += ['', '## 新增档位数据核对', '', *quality['report_paragraphs']]
(folder/'实测报告.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'folder':str(folder),'points':summary['points']},ensure_ascii=False))
