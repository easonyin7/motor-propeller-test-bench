"""Summarize a saved two-second target probe; no hardware access."""
import csv
import json
from pathlib import Path
import sys
from window_quality import summarize

p=Path(sys.argv[1]).resolve()
r=json.loads((p/'result.json').read_text(encoding='utf-8'))
assert r['status']=='software_checks_passed_physical_confirmation_pending'
assert r['reported_output_returned_zero'] and r['stop_commands_acknowledged']
c=r['sweep_steps'][0]['control']['target_window']
assert c['seconds']==2
with (Path(r['cv_log_folder'])/'samples.csv').open(encoding='utf-8-sig',newline='') as source:
    rows=list(csv.DictReader(source))
assert all(v['run_id']==r['run_id'] for v in rows)
rows=[v for v in rows if c['started_monotonic_s']<=int(v['captured_monotonic_ns'])/1e9<c['deadline_monotonic_s']]
result={'run_id':r['run_id'],'target_value':c['value'],'is_curve_measurement':False,
        'note':'Short probe including transient and display latency; not a steady-state point.',
        'fields':{k:summarize(rows,k) for k in ('upper','voltage','current','power')}}
(p/'probe_target_readings.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
