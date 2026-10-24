from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from px4_mvp.runtime_paths import project_python

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'bench_app' / 'runs'
CAMERA_CONFIG = ROOT / 'cv_mvp' / 'instrument_config.json'
CAMPAIGN = ROOT / 'integration_runs/campaign_summary_20261018/全档位数据_合并.csv'
LEVELS = (.025,.03,.035,.04,.045,.05,.055,.06,.065,.07,.08,.09,.10,.12,.15,.18,.22,.27,.33,.40,.50,.60,.70,.75,.80,.85,.90)
MIN_LEVEL=.010
MAX_LEVEL=1.000
MAX_PLAN_STEPS=64
TEMPLATES = {
    '主测 10–90%': (.10,.20,.30,.40,.50,.60,.70,.80,.90),
    '低输出验证': (.025,.03,.04,.05,.06),
    '中段曲线': (.065,.10,.15,.22,.33,.40,.50,.60),
    '高段曲线': (.70,.75,.80,.85,.90),
    '全部已测档位': LEVELS,
}

@dataclass(frozen=True)
class Step:
    level: float

    def __post_init__(self):
        if not math.isfinite(self.level) or not MIN_LEVEL<=self.level<=MAX_LEVEL:
            raise ValueError('油门指令应在0.010～1.000之间。')
        if round(self.level,3)!=self.level:
            raise ValueError('油门指令最多保留三位小数，例如0.073。')

    @property
    def hold_seconds(self):
        # One uniform stable-hold time for every throttle; the bounded rise is
        # separate and the total never exceeds 20 seconds.
        return 10

    @property
    def rise_seconds(self):
        if self.level>.60:return 10
        if self.level>.06:return 5
        if self.level<=.025:return 0
        return {.03:4,.035:4,.04:4.5,.045:4,.05:5,.055:5,.06:5}.get(self.level,5)

    @property
    def stage(self):
        return '低段' if self.level<=.06 else '中段' if self.level<=.60 else '高段'

    def as_dict(self):
        return dict(level=self.level,hold_seconds=self.hold_seconds,rise_seconds=self.rise_seconds,stage=self.stage)

@dataclass(frozen=True)
class Plan:
    levels: tuple
    cooldown_seconds: float = 5
    settle_seconds: float = 2
    followup_first: bool = False

    def __post_init__(self):
        if not self.levels or len(self.levels)>MAX_PLAN_STEPS:
            raise ValueError(f'测试方案需要1～{MAX_PLAN_STEPS}个档位。')
        for value in self.levels:
            Step(value)
        if tuple(sorted(set(self.levels)))!=self.levels:
            raise ValueError('档位必须从小到大排列，且不能重复。')
        if not math.isfinite(self.cooldown_seconds) or not 5<=self.cooldown_seconds<=120:
            raise ValueError('档间停机间隔应为5～120秒。')
        if not math.isfinite(self.settle_seconds) or not 0<=self.settle_seconds<=5:
            raise ValueError('忽略目标档开始的时间应为0～5秒。')
        if not isinstance(self.followup_first,bool):
            raise ValueError('首档接续标记必须是布尔值。')

    def as_dict(self):
        return dict(steps=[Step(v).as_dict() for v in self.levels],
                    cooldown_seconds=self.cooldown_seconds,settle_seconds=self.settle_seconds,
                    followup_first=self.followup_first)

    @property
    def signature(self):
        return hashlib.sha256(json.dumps(self.as_dict(),sort_keys=True).encode()).hexdigest()

def python_executable():
    return project_python(ROOT)

def validate_profile(path):
    config=json.loads(Path(path).read_text(encoding='utf-8'))
    if not config.get('camera') or (config.get('width'),config.get('height'))!=(1920,1080):
        raise ValueError('请选择当前1920×1080仪表标定配置。')
    fields={f['id']:f for f in config['fields']}
    for key,unit in [('upper','g'),('voltage','V'),('current','A'),('power','W')]:
        if fields.get(key,{}).get('unit')!=unit:
            raise ValueError('标定缺少拉力、电压、电流或功率单位。')
    ref=Path(config['reference_image'])
    if not ref.is_absolute():
        ref=ROOT/'cv_mvp'/ref
    if not ref.is_file():
        raise ValueError('找不到标定参考照片。')
    config['reference_image']=str(ref.resolve())
    return config

def build_command(step,port,config,result_dir,operator_record=None,prepare_only=False,ui_heartbeat=None,
                  residual_thrust_limit=None,residual_unlimited=False):
    if not re.fullmatch(r'COM[1-9][0-9]*',port,re.IGNORECASE):
        raise ValueError('请选择有效的Windows串口。')
    if residual_thrust_limit is not None and not 0<residual_thrust_limit<=6:
        raise ValueError('残余拉力上限应在0～6克之间。')
    if residual_unlimited and residual_thrust_limit is not None:
        raise ValueError('残余拉力不能同时设置固定上限和不限上限。')
    args=[python_executable(),'-X','utf8',str(ROOT/'px4_mvp/timed_hold.py'),
          '--port',port,'--cv-config',str(config),'--result-dir',str(result_dir),
          '--next-step','--step-value',str(step.level),'--step-seconds',str(step.hold_seconds)]
    if ui_heartbeat:args+=['--ui-heartbeat',str(ui_heartbeat)]
    if residual_unlimited and not prepare_only:
        args+=['--residual-thrust-unlimited']
    elif residual_thrust_limit is not None and not prepare_only:
        args+=['--residual-thrust-limit',f'{residual_thrust_limit:g}']
    if step.level>.60:
        args+=['--gentle-rise','--extreme-range']
    elif step.level>.06:
        args+=['--gentle-rise','--high-range']
    elif step.level>.025:
        args+=['--gentle-rise','--expanded-trial']
    if prepare_only:
        args+=['--prepare-only','--monitor-seconds','5']
    else:
        if not operator_record:
            raise ValueError('实机执行需要本轮现场确认。')
        args+=['--execute','--guarded-prop-bench','--operator-record',str(operator_record)]
    return args
