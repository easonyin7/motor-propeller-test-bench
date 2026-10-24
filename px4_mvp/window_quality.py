"""Offline acceptance of sparse observations; never fills gaps or controls hardware."""
import math
import statistics

FIELDS={'upper':('g',2,.20),'current':('A',.02,.10),'power':('W',.5,.10)}


def summarize(rows,key):
    values=[]
    reasons={}
    for row in rows:
        try:
            value=float(row.get(key+'_raw',''))
            valid=row.get(key+'_valid')=='True' and math.isfinite(value)
        except (ValueError,TypeError):
            valid=False
        if valid:
            values.append(value)
        else:
            reason=row.get(key+'_reason') or 'missing_or_nonfinite'
            reasons[reason]=reasons.get(reason,0)+1
    return {'samples':len(rows),'valid_samples':len(values),
            'valid_fraction':len(values)/len(rows) if rows else 0,
            'mean':statistics.mean(values) if values else None,
            'min':min(values) if values else None,'max':max(values) if values else None,
            'invalid_reasons':reasons}


def assess(rows,duration):
    if duration not in (15,20):
        raise ValueError('Window comparison requires a completed 15 or 20 second trial.')
    # Predeclared exploratory thresholds, not instrument accuracy specifications.
    result={'duration_seconds':duration,'analysis_start_seconds':5,
            'policy':{'overall_min_valid_fraction':.70,'overall_min_valid_samples':20,
                      'each_window_min_valid_fraction':.50,'each_window_min_valid_samples':5,
                      'window_seconds':5},'fields':{}}
    for key,(unit,absolute,relative) in FIELDS.items():
        overall=summarize([r for r in rows if 5<=r['relative_seconds']<duration],key)
        windows=[{'start':start,'end':start+5,
                  **summarize([r for r in rows if start<=r['relative_seconds']<start+5],key)}
                 for start in range(5,duration,5)]
        means=[w['mean'] for w in windows if w['mean'] is not None]
        tolerance=max(absolute,relative*abs(overall['mean'])) if overall['mean'] is not None else absolute
        spread=max(means)-min(means) if len(means)==len(windows) else None
        coverage=(overall['valid_fraction']>=.70 and overall['valid_samples']>=20 and
                  all(w['valid_fraction']>=.50 and w['valid_samples']>=5 for w in windows))
        consistent=spread is not None and spread<=tolerance
        result['fields'][key]={'unit':unit,'overall':overall,'windows':windows,
            'window_mean_tolerance':tolerance,'absolute_tolerance':absolute,'relative_tolerance':relative,
            'window_mean_spread':spread,'coverage_passed':coverage,'window_consistency_passed':consistent,
            'passed':coverage and consistent,
            'prefix_comparisons':{str(end):summarize([r for r in rows if 5<=r['relative_seconds']<end],key)
                                  for end in (10,15,20) if end<=duration}}
    result['passed']=all(f['passed'] for f in result['fields'].values())
    result['interpretation']='Exploratory display-mean consistency only; missing-data bias, calibration and physical steady-state are not established.'
    return result
