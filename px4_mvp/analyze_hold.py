"""Analyze saved trial logs and plot camera readings; never opens hardware."""
import csv
import json
import math
import os
from pathlib import Path
import statistics
import sys

folder=Path(sys.argv[1]).resolve()
report=json.loads((folder/'result.json').read_text(encoding='utf-8'))
cv_folder=Path(report['cv_log_folder'])
with (cv_folder/'samples.csv').open(encoding='utf-8-sig',newline='') as stream:
    rows=list(csv.DictReader(stream))
start=report['control']['started_monotonic_s']
end=report['control']['deadline_monotonic_s']
for row in rows:
    row['relative_s']=int(row['captured_monotonic_ns'])/1e9-start
running=[r for r in rows if 0<=r['relative_s']<end-start]
window=[r for r in running if r['relative_s']>=2]
labels={'upper':('拉力','g'),'voltage':('电压','V'),'current':('电流','A'),
        'power':('功率','W'),'energy':('电能','Wh')}

def values(samples,key,stable=False):
    suffix='_stable' if stable else '_raw'
    return [float(r[key+suffix]) for r in samples if r[key+suffix] and r[key+'_valid']=='True']

def metrics(samples,key):
    data=values(samples,key)
    stable=values(samples,key,True)
    return {'samples':len(samples),'readable_samples':len(data),'stable_samples':len(stable),
            'min':min(data) if data else None,'max':max(data) if data else None,
            'median':statistics.median(data) if data else None,
            'mean':statistics.mean(data) if data else None,
            'population_std':statistics.pstdev(data) if data else None,
            'published_stable_values':sorted(set(stable))}

observation_path=folder/'operator_observation.json'
observation=json.loads(observation_path.read_text(encoding='utf-8')) if observation_path.exists() else {}
image_review_path=folder/'image_review.json'
image_review=json.loads(image_review_path.read_text(encoding='utf-8')) if image_review_path.exists() else {}
analysis={'requested_seconds':10,'control':report['control'],
          'max_observed_cv_age_seconds':report['max_observed_cv_age_seconds'],
          'physical_observation':observation,
          'image_review':image_review,
          'running_fields':{k:metrics(running,k) for k in labels},
          'inspection_window':'Commanded run seconds 2..10; not an independently proven steady state.',
          'window_fields':{k:metrics(window,k) for k in labels},
          'measurement_limits':'Camera-decoded display values; no independent calibration or frame-by-frame ground truth. Host clock is not sensor exposure time.'}
(folder/'analysis.json').write_text(json.dumps(analysis,ensure_ascii=False,indent=2),encoding='utf-8')

selected=[r for r in rows if -2<=r['relative_s']<=14]
with (folder/'series.csv').open('w',encoding='utf-8-sig',newline='') as stream:
    columns=['captured_at','relative_s']+[k+'_'+s for k in labels for s in ('raw','stable','valid','reason')]
    writer=csv.DictWriter(stream,columns,extrasaction='ignore')
    writer.writeheader()
    writer.writerows(selected)

os.environ['MPLCONFIGDIR']=str(folder/'plot_cache')
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

fig,axes=plt.subplots(3,1,figsize=(10,7),sharex=True,layout='constrained')
for ax,key,title in zip(axes,('upper','current','power'),('Thrust display (g)','Current display (A)','Power display (W)')):
    xs=[r['relative_s'] for r in selected]
    ys=[float(r[key+'_raw']) if r[key+'_raw'] and r[key+'_valid']=='True' else math.nan for r in selected]
    ax.plot(xs,ys,'o-',color='#176e9c',markersize=3,linewidth=1,label='Readable sample')
    stable=[r for r in selected if r[key+'_stable'] and r[key+'_valid']=='True']
    ax.scatter([r['relative_s'] for r in stable],[float(r[key+'_stable']) for r in stable],
               color='#e87827',marker='x',s=24,label='Identical-value confirmation')
    ax.axvline(0,color='#555555',linestyle='--',linewidth=1)
    ax.axvline(10,color='#555555',linestyle='--',linewidth=1)
    ax.axvspan(2,10,color='#94c7a1',alpha=.12)
    ax.set_ylabel(title)
    ax.grid(alpha=.2)
axes[0].set_title('Motor 1: normalized output 0.02, commanded duration 10 s')
axes[0].legend(loc='best',fontsize=8)
axes[-1].set_xlabel('Host capture time relative to run start (s)')
fig.savefig(folder/'readings.png',dpi=160)
plt.close(fig)

lines=['# 10秒带桨联调实测记录','',
       '日期：2026-10-17（北京时间）。本轮保持 Motor 1、归一化输出0.02，不加档。','',
       '**本轮完成10秒续发流程，停止指令获确认、飞控输出回零；用户确认“已完全停止，运行无明显异常”。**','',
       '运行指令有效期每条不超过1秒，最后一条不越过本轮截止时刻。控制循环约运行'
       f"{report['control']['loop_elapsed_seconds']:.3f}秒，续发{report['control']['renewals']}次；停止回执流程另耗时约"
       f"{report['control']['stop_ack_elapsed_seconds']:.3f}秒。该计时不是物理电机停止时间。",'',
       f"运行时原始输出为{report['active_raw_outputs']}，其他通道回报保持0。最大观测数据年龄约"
       f"{report['max_observed_cv_age_seconds']:.3f}秒，仍使用1秒过期门槛。",'',
       '## 运行第2～10秒的读数','',
       '此处排除最初2秒用于观察变化，窗口选择本身不证明已经稳态。所有数值为相机识别的仪表显示值。','',
       '| 项目 | 可读/总样本 | 即时范围 | 中位数 | 均值 | 总体标准差 | 有逐字稳定值的样本 |',
       '|---|---:|---|---:|---:|---:|---:|']
for key,(label,unit) in labels.items():
    m=analysis['window_fields'][key]
    if m['readable_samples']:
        lines.append(f"| {label}（{unit}） | {m['readable_samples']}/{m['samples']} | "
                     f"{m['min']:g}～{m['max']:g} | {m['median']:g} | {m['mean']:.3f} | "
                     f"{m['population_std']:.3f} | {m['stable_samples']} |")
    else:
        lines.append(f"| {label}（{unit}） | 0/{m['samples']} | 缺测 | — | — | — | 0 |")
lines += ['',
          '“逐字稳定”沿用至少3次、持续0.6秒完全相同的显示字符判定。连续变化的可读数值仍记录，不当作采集故障；机械工况稳定需结合波动范围和另行约定的容差判断。',
          '中位数、均值和标准差是本次采样统计，不代表校准后的推力或计量不确定度；没有逐帧独立真值。','',
          '## 之前中止与本轮修订','',
          '17:40的首次10秒尝试在约4.7秒时因相机数据超过1秒门槛而中止，已保留日志和实际停转确认。',
          '随后优化LCD局部对齐：采用上一帧已通过的变换作为初始估计，并限制迭代次数；每帧仍重新检查定位相关性与位移，不沿用旧数字。',
          '恢复连接后还发现状态文件偶发访问冲突，读取端现仅对此做最多50毫秒的短时重试，随后仍验证数据时间、会话和递增序号。',
          '13项离线检查、既有11项照片回放和16项小数点检查通过；本轮前20秒静态联合监测通过，最大数据年龄约0.421秒。','',
          '本轮没有提高输出、修改飞控参数、解锁或进行拔线/强制退出故障注入。那些故障下的实际停机仍未实测完成。','',
          '## 证据','',
          f'- [控制事件]({(folder/"control_events.jsonl").as_posix()})',
          f'- [原始相机采样]({(cv_folder/"samples.csv").as_posix()})',
          f'- [本轮采样曲线]({(folder/"readings.png").as_posix()})',
          f'- [统计结果]({(folder/"analysis.json").as_posix()})',
          f'- [现场反馈]({observation_path.as_posix()})']
if image_review:
    lines += ['','## 画面中的测量条件','',image_review['observation'],
              image_review['implication'],image_review['causality_not_established'],
              '下一轮：'+image_review['next_measurement_condition']]
(folder/'实测记录.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
print(json.dumps({'folder':str(folder),'window_fields':analysis['window_fields'],
                  'whole_run_fields':analysis['running_fields']},ensure_ascii=False))
