"""Read raw records and save new sessions; synced sources are never modified."""
import csv
from datetime import datetime
import json
import math
from pathlib import Path
import statistics
import io
import os
import threading
import time

from .model import CAMPAIGN, ROOT, Step

def read_json(path,default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError,ValueError):
        return default

def write_text(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temp.write_text(value,encoding='utf-8')
    for attempt in range(16):
        try:
            temp.replace(path)
            return
        except PermissionError:
            if attempt==15:
                temp.unlink(missing_ok=True)
                raise
            time.sleep(.025)

def write_json(path,value):
    write_text(path,json.dumps(value,ensure_ascii=False,indent=2))

def number(field,unit):
    if field.get('valid') is not True or field.get('unit')!=unit:
        return None
    try:
        value=float(field['value'])
        return value if math.isfinite(value) else None
    except (KeyError,TypeError,ValueError):
        return None

def stats(values):
    values=[float(v) for v in values if v is not None and math.isfinite(v)]
    return dict(n=len(values),mean=statistics.mean(values) if values else None,
                median=statistics.median(values) if values else None,
                min=min(values) if values else None,max=max(values) if values else None,
                stdev=statistics.pstdev(values) if values else None)

def target_window(report):
    steps=report.get('sweep_steps') or []
    control=steps[0]['control'] if steps else report.get('control',{})
    window=control.get('target_window')
    if window:
        return window['started_monotonic_s'],window['seconds']
    return control['started_monotonic_s'],control['requested_seconds']

def raw_samples(folder):
    path=Path(folder)/'observed_cv.jsonl'
    if not path.exists():
        return []
    rows=[]
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows

def analyze_run(folder,settle_seconds=2,source_kind='measurement'):
    folder=Path(folder);report=read_json(folder/'result.json')
    if not report or report.get('status')!='software_checks_passed_physical_confirmation_pending':
        raise ValueError('中止或未完成的运行不计入完整曲线。')
    if not all(report.get(k) is True for k in ('stop_commands_acknowledged','reported_output_returned_zero','other_channels_zero')):
        raise ValueError('停止回执或输出回零未确认，不能接续下一档。')
    start,duration=target_window(report)
    if not 0<=settle_seconds<duration:
        raise ValueError('统计忽略时间超出目标保持时间。')
    values={k:[] for k in ('thrust_g','voltage_v','current_a','power_w','display_power_w')}
    paired=[];count=0;seen=set()
    for row in raw_samples(folder):
        if row.get('run_id')!=report['run_id']:
            continue
        timestamp=row.get('captured_monotonic_ns',0)/1e9
        if not start+settle_seconds<=timestamp<start+duration:
            continue
        marker=(row.get('frame_seq'),row.get('captured_monotonic_ns'))
        if marker in seen:
            continue
        seen.add(marker);count+=1
        f={x['id']:x for x in row.get('fields',[])}
        thrust=number(f.get('upper',{}),'g');volts=number(f.get('voltage',{}),'V')
        amps=number(f.get('current',{}),'A');shown=number(f.get('power',{}),'W')
        power=volts*amps if volts is not None and amps is not None else None
        for key,value in zip(values,(thrust,volts,amps,power,shown)):
            values[key].append(value)
        if thrust is not None and power is not None:
            paired.append((thrust,power))
    metrics={k:stats(v) for k,v in values.items()}
    for value in metrics.values():
        value['readable_fraction']=value['n']/count if count else 0
    paired_power=statistics.mean(p for _,p in paired) if paired else 0
    efficiency=statistics.mean(t for t,_ in paired)/paired_power if paired_power>0 else None
    issues=[];warnings=[]
    for key,label in [('thrust_g','拉力'),('voltage_v','电压'),('current_a','电流'),('power_w','功率')]:
        n=metrics[key]['n'];fraction=metrics[key]['readable_fraction']
        if n<10:
            issues.append(label+f'有效样本不足10个（{n}/{count}）')
        elif fraction<.70:
            # The LED panels refresh faster than the camera samples them, so a
            # share of frames is correctly rejected. Keep the point, report the
            # rate, and only pause when the valid sample count itself is short.
            warnings.append(label+f'窗口可读率 {fraction:.0%}（{n}/{count}），有效样本数已满足统计要求')
    return dict(level=float(report['value']),run_dir=str(folder.resolve()),source_kind=source_kind,
                completed=True,samples=count,settle_seconds=settle_seconds,
                hold_seconds=duration,window_seconds=duration-settle_seconds,
                metrics=metrics,paired_samples=len(paired),efficiency_g_per_w=efficiency,
                power_source='same_frame_voltage_x_current',zero_correction_applied=False,
                quality=dict(passed=not issues,issues=issues,warnings=warnings),
                note='；'.join(warnings) if warnings else None,
                policy=report.get('process_policy','no_fixed_thresholds'),limits=None,
                physical_confirmation_pending=source_kind=='measurement')

def supplement_levels(points,low_g=300,high_g=500,count=8):
    """Derive throttle levels at evenly spaced thrust targets by interpolation.

    The measured (throttle, mean thrust) pairs must cover the whole thrust
    interval and increase monotonically. The returned levels are interior
    targets, so they densify the band without repeating its endpoints. This is
    only a plan generator: it never starts a motor or adds stages during a run.
    """
    if (not math.isfinite(low_g) or not math.isfinite(high_g)
            or not 0<low_g<high_g or not isinstance(count,int) or count<1):
        raise ValueError('补点拉力区间或数量无效。')
    by_level={}
    for point in points or []:
        try:
            level=float(point['level'])
            thrust=float(point['metrics']['thrust_g']['mean'])
        except (KeyError,TypeError,ValueError):
            continue
        if math.isfinite(level) and math.isfinite(thrust) and thrust>0:
            by_level[level]=thrust
    measured=sorted(by_level.items())
    if len(measured)<2:
        raise ValueError('至少需要两个已测拉力点才能插值补点。')
    for (_,first),(_,second) in zip(measured,measured[1:]):
        if second<=first:
            raise ValueError('已测拉力未随油门单调增加，无法可靠插值补点。')
    if measured[0][1]>low_g or measured[-1][1]<high_g:
        raise ValueError(f'已测拉力未覆盖 {low_g:g}–{high_g:g} g，无法在该区间补点。')
    def throttle_at(thrust):
        for (level_a,thrust_a),(level_b,thrust_b) in zip(measured,measured[1:]):
            if thrust_a<=thrust<=thrust_b:
                return level_a+(level_b-level_a)*(thrust-thrust_a)/(thrust_b-thrust_a)
        raise ValueError('目标拉力不在已测区间内。')
    levels=[]
    for index in range(1,count+1):
        thrust=low_g+(high_g-low_g)*index/(count+1)
        level=round(throttle_at(thrust),3)
        if not levels or level>levels[-1]:
            levels.append(level)
    if len(levels)!=count:
        raise ValueError('三位小数油门分辨率不足以在目标区间生成互不重复的补点。')
    for level in levels:Step(level)
    return tuple(levels)

def load_campaign(path=CAMPAIGN):
    records=[]
    with Path(path).open(encoding='utf-8-sig',newline='') as stream:
        for row in csv.DictReader(stream):
            def value(key):
                try:
                    n=float(row[key]);return n if math.isfinite(n) else None
                except (KeyError,TypeError,ValueError):
                    return None
            level=value('throttle')
            if level is None:
                continue
            metrics={
                'thrust_g':dict(mean=value('thrust_mean_g'),min=value('thrust_min_g'),max=value('thrust_max_g')),
                'voltage_v':dict(mean=value('voltage_V')),
                'current_a':dict(mean=value('current_mean_A'),max=value('current_peak_A')),
                'power_w':dict(mean=value('power_mean_W'),max=value('power_peak_W'))}
            records.append(dict(level=level,metrics=metrics,efficiency_g_per_w=value('efficiency_g_per_W'),
                source_kind='historical_campaign',run_dir=str(ROOT/'integration_runs'/row['run']),
                hold_seconds=value('hold_seconds'),completed=True,
                note='历史汇总：不同批次统计窗口、功率来源和保护策略存在差异；不自动合并重复点。'))
    return records

def export_session(session,folder):
    folder=Path(folder);folder.mkdir(parents=True,exist_ok=True)
    write_json(folder/'session.json',session)
    fields=['level','source_kind','phase','stage_number','thrust_mean_g','thrust_min_g','thrust_max_g','voltage_mean_v',
            'current_mean_a','current_peak_a','power_mean_w','power_peak_w','efficiency_g_per_w',
            'samples','thrust_valid_samples','hold_seconds','settle_seconds','policy','run_dir']
    with io.StringIO(newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader()
        for point in session.get('points',[]):
            m=point['metrics'];t=m['thrust_g'];i=m['current_a'];p=m['power_w']
            writer.writerow(dict(level=point['level'],source_kind=point['source_kind'],
                phase=point.get('phase',''),stage_number=point.get('stage_number',''),
                thrust_mean_g=t['mean'],thrust_min_g=t.get('min'),thrust_max_g=t.get('max'),
                voltage_mean_v=m['voltage_v']['mean'],current_mean_a=i['mean'],current_peak_a=i.get('max'),
                power_mean_w=p['mean'],power_peak_w=p.get('max'),
                efficiency_g_per_w=point['efficiency_g_per_w'],samples=point.get('samples'),
                thrust_valid_samples=t.get('n'),hold_seconds=point.get('hold_seconds'),
                settle_seconds=point.get('settle_seconds'),policy=point.get('policy'),run_dir=point['run_dir']))
        write_text(folder/'results.csv','\ufeff'+stream.getvalue())
    def fmt(value):return '—' if value is None else f'{value:.3f}'.rstrip('0').rstrip('.')
    lines=['# 电机桨叶测试记录','',f"状态：{session['status']}",f"模式：{session['mode']}",
        f"创建：{session.get('created_at','')}",f"电机／桨叶：{session.get('specimen','未填写')}",'',
        '新采集点统一忽略目标档前指定秒数；功率使用同帧电压×电流，仪表功率保留在原始记录。缺测不补值，未自动扣零。历史回放不代表新实测。','',
        '| 阶段 | 指令 | 拉力均值 g | 电流均值 A | 功率均值 W | 效率 g/W | 拉力有效/窗口样本 |','|---|---|---:|---:|---:|---:|---|']
    for point in session.get('points',[]):
        m=point['metrics']
        phase={'main':'主测','supplement':'自动补点'}.get(point.get('phase'),'—')
        lines.append(f"| {phase} | {point['level']:.3f} | {fmt(m['thrust_g']['mean'])} | {fmt(m['current_a']['mean'])} | {fmt(m['power_w']['mean'])} | {fmt(point['efficiency_g_per_w'])} | {m['thrust_g'].get('n','—')}/{point.get('samples','—')} |")
        for warning in (point.get('quality') or {}).get('warnings',[]):
            lines.append(f"- {point['level']:.3f} 统计提示：{warning}")
    supplement=session.get('supplement')
    if supplement:
        levels='、'.join(f'{v:.3f}' for v in supplement.get('levels',[])) or '尚未生成'
        lines+=['','## 自动补点计划','',f"状态：{supplement['status']}；目标300–500g区间内部8点。",
                f'实际输出指令：{levels}。','仅按本轮主测结果插值；不是拉力闭环，不保证实测值恰好等于目标。']
        if supplement.get('error'):lines.append('未生成原因：'+supplement['error'])
    retries=session.get('retry_log') or []
    if retries:
        lines+=['','## 自动重测记录','',
                '以下档位首次统计样本不足；电机已停机、输出已回零后只重测本档，失败尝试不计入曲线。','',
                '| 档位 | 尝试 | 原因 | 原始目录 |','|---:|---:|---|---|']
        for retry in retries:
            issues='；'.join(retry.get('issues') or []) or '统计样本不足'
            lines.append(f"| {retry['level']:.3f} | 第{retry['attempt']}次 | {issues} | {retry.get('folder','')} |")
    resumes=session.get('resume_log') or []
    if resumes:
        lines+=['','## 从失败档续测记录','',
                '续测保留原会话已完成的点和原始目录，只重新执行未完成档位；每次续测都重新进行现场三项确认。','',
                '| 次数 | 起始档位 | 剩余档位 | 前次中止原因 | 现场确认签名 |','|---:|---:|---|---|---|']
        for resume in resumes:
            levels='、'.join(f"{float(v):.3f}" for v in resume.get('remaining_levels') or [])
            reason=(resume.get('previous_error') or '—').replace('|','／').replace('\n',' ')
            lines.append(f"| {resume.get('number')} | {float(resume.get('level',0)):.3f} | {levels} | {reason} | {(resume.get('operator_signature') or '')[:16]}… |")
            if resume.get('superseded_points'):
                lines.append(f"- 第{resume.get('number')}次续测前的不足样本点已移出本轮汇总，原始目录保留：")
                lines.extend(f"  - {point.get('run_dir')}" for point in resume['superseded_points'])
    lines+=['',f"现场停转确认：{session.get('physical_confirmation') or '未确认；软件回零不等于现场停转'}",'',
            '原始运行目录：',*[f"- {p}" for p in session.get('run_dirs',[])]]
    if session.get('error'):lines+=['',f"中止原因：{session['error']}"]
    write_text(folder/'report.md','\n'.join(lines)+'\n')
