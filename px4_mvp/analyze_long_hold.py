"""Summarize one saved 15/20-second run without connecting to hardware."""
import csv
import json
from pathlib import Path
import sys
from window_quality import assess

folder=Path(sys.argv[1]).resolve()
run=json.loads((folder/'result.json').read_text(encoding='utf-8'))
assert len(run['sweep_steps'])==1
step=run['sweep_steps'][0]
assert step['reported_output_returned_zero'] and step['control']['stop_commands_acknowledged']
target=step['control'].get('target_window')
duration=target['seconds'] if target else step['control']['requested_seconds']
assert duration in (15,20)
with (Path(run['cv_log_folder'])/'samples.csv').open(encoding='utf-8-sig',newline='') as source:
    rows=list(csv.DictReader(source))
assert all(r['run_id']==run['run_id'] for r in rows)
for row in rows:
    row['relative_seconds']=int(row['captured_monotonic_ns'])/1e9-(target['started_monotonic_s'] if target else step['control']['started_monotonic_s'])
result=assess(rows,int(duration))
result.update(run_id=run['run_id'],normalized_output=step['value'],source_cv_folder=run['cv_log_folder'])
result['total_profile_seconds']=step['control']['requested_seconds']
result['warmup_seconds_excluded']=step['control']['requested_seconds']-duration
baseline={f['id']:f for f in run['baseline']['fields']}
base_current=float(baseline['current']['value'])
base_power=float(baseline['power']['value'])
current=result['fields']['current']['overall']['mean']
power=result['fields']['power']['overall']['mean']
result['load_response_observed']=(current is not None and power is not None and
                                 current>base_current+.02 and power>base_power+.5)
result['window_conditions_passed']=result['passed']
result['passed']=result['passed'] and result['load_response_observed']
result['physical_confirmation_file_present']=(folder/'operator_observation.json').exists()
(folder/'window_quality.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
lines=['# 延长采集时间的窗口核对','',f"输出 {step['value']:g}，持续 {duration:g} 秒。统计第5秒至结束的有效即时读数，缺测不补值。",'',
       '负载响应核对：'+('有高于停机基线的电流和功率响应。' if result['load_response_observed'] else '**未取得负载响应证据，本轮不能作为有效电机性能测点；即使零值均值一致也不通过。**'),'',
       '| 字段 | 均值 | 有效样本 | 各5秒窗口均值 | 窗口均值跨度 / 暂定容差 | 数据条件 |',
       '|---|---:|---:|---|---|---|']
if target:
    prefix='、'.join(f"{s['value']:g}档{s['seconds']:g}秒" for s in step['control']['profile_stages'][:-1])
    lines.insert(4,f"本次先经{prefix}缓升，再进入{step['value']:g}目标档；统计时间从目标档开始计算，预转{result['warmup_seconds_excluded']:g}秒不混入目标档统计。")
for key,label in [('upper','拉力 g'),('current','电流 A'),('power','功率 W')]:
    f=result['fields'][key]; s=f['overall']
    mean=f"{s['mean']:.4g}" if s['mean'] is not None else '缺测'
    means='、'.join(f"{w['mean']:.4g}" if w['mean'] is not None else '缺测' for w in f['windows'])
    spread=f"{f['window_mean_spread']:.4g}" if f['window_mean_spread'] is not None else '缺测'
    lines.append(f"| {label} | {mean} | {s['valid_samples']}/{s['samples']}（{s['valid_fraction']:.1%}） | {means} | {spread} / {f['window_mean_tolerance']:.4g} | {'通过' if f['passed'] else '未通过'} |")
lines+=['','以上门槛为本轮探索性数据条件：整体可读率至少70%、至少20个有效样本；每个5秒窗口至少50%可读且至少5个有效样本。',
        '各窗口均值跨度门槛：拉力取2 g与均值20%中较大值，电流取0.02 A与10%中较大值，功率取0.5 W与10%中较大值。',
        '通过仅表示仪表显示均值在该窗口内接近，不证明计量精度、物理稳态或缺测无偏。实际停止须另见现场确认记录。',
        '10/15/20秒前缀统计与各字段缺测原因见 window_quality.json。']
(folder/'窗口核对.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
import os
os.environ['MPLCONFIGDIR']=str(folder/'plot_cache')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
fig,axes=plt.subplots(3,1,sharex=True,figsize=(10,7),layout='constrained')
for ax,key,label in zip(axes,('upper','current','power'),('Thrust display (g)','Current display (A)','Power display (W)')):
    selected=[r for r in rows if -1<=r['relative_seconds']<=duration+3]
    ys=[]
    for r in selected:
        try:
            value=float(r[key+'_raw'])
        except (ValueError,TypeError):
            value=float('nan')
        ys.append(value if r[key+'_valid']=='True' else float('nan'))
    ax.plot([r['relative_seconds'] for r in selected],ys,'o-',markersize=2,linewidth=1)
    for w in result['fields'][key]['windows']:
        if w['mean'] is not None:
            ax.hlines(w['mean'],w['start'],w['end'],color='#c35424',linewidth=2)
    ax.set_ylabel(label); ax.grid(alpha=.2)
    ax.axvline(0,color='gray',linestyle='--'); ax.axvline(duration,color='gray',linestyle='--')
axes[0].set_title(f"Output {step['value']:g}, {duration:g}s; orange: five-second window means")
axes[-1].set_xlabel('Host capture time relative to command start (s)')
fig.savefig(folder/'long_hold_readings.png',dpi=150); plt.close(fig)
print(json.dumps(result,ensure_ascii=False))
