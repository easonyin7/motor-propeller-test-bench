"""Physical LED/LCD preview with independent validation and local logging."""
import argparse
import csv
import json
import os
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk,messagebox

import cv2
import numpy as np
from PIL import Image,ImageTk

from cv_camera_test import Capture,cameras,write_json
from instrument_reader import InstrumentReader,hold_recent_led_fields
from seven_segment import Stability

ROOT = Path(__file__).resolve().parent


class InstrumentWorker:
    def __init__(self,config,run_id=None,snapshot_path=None,preview_path=None):
        self.config = config
        self.run_id = run_id or uuid.uuid4().hex
        self.snapshot_path = Path(snapshot_path) if snapshot_path else ROOT/'instrument_logs/latest.json'
        self.preview_path = Path(preview_path) if preview_path else None
        self.reader = InstrumentReader(config,ROOT)
        matched = [c for c in cameras() if c.name==config['camera']]
        if len(matched)!=1:
            raise RuntimeError('未唯一找到外置 USB 相机。')
        self.capture = Capture(matched[0],config['width'],config['height'])
        self.started = time.perf_counter()
        self.folder = ROOT/'instrument_logs'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.folder.mkdir(parents=True)
        write_json(self.folder/'config.json',config)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.latest = None
        self.current = None
        self.error = None
        self.correct = 0
        self.wrong = 0
        self.thread = threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def review(self,correct):
        with self.lock:
            if not self.current or not all(f.get('stable_value') is not None for f in self.current['fields']):
                return False
            event = {'reviewed_at':datetime.now().astimezone().isoformat(),
                     'sample_time':self.current['captured_at'],
                     'values':{f['id']:f['stable_value'] for f in self.current['fields']},
                     'user_says_correct':correct}
            with (self.folder/'manual_reviews.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(event,ensure_ascii=False)+'\n')
            if correct:
                self.correct += 1
            else:
                self.wrong += 1
        return True

    def run(self):
        trackers = {f['id']:Stability(self.config['stable_seconds']) for f in self.config['fields']}
        ids = list(trackers)
        valid_counts = Counter()
        stable_counts = Counter()
        previous = {}
        samples = 0
        all_valid = 0
        led_cache = {}
        led_scans = Counter()
        led_scan_valid = Counter()
        led_cache_hits = 0
        led_hold_seconds = min(.25,max(.08,self.config['sample_interval']*1.25))
        last_seq = -1
        last_read = -1.
        last_preview = -1.
        last_disk_preview = -1.
        state = {}
        panels = {}
        changes = 0
        try:
            with (self.folder/'samples.csv').open('w',newline='',encoding='utf-8-sig') as stream, \
                 (self.folder/'changes.csv').open('w',newline='',encoding='utf-8-sig') as changed_stream:
                writer = csv.writer(stream)
                changed = csv.writer(changed_stream)
                writer.writerow(['captured_at','elapsed_seconds','alignment_valid','recognition_ms']+
                    [f'{key}_{suffix}' for key in ids for suffix in ('raw','stable','unit','valid','reason')]+
                    ['run_id','frame_seq','captured_monotonic_ns','published_monotonic_ns'])
                changed.writerow(['accepted_at','field','name','value','unit'])
                while not self.stop.is_set():
                    item = self.capture.get()
                    now = time.perf_counter()
                    if item is None or item[0]==last_seq:
                        if not self.capture.thread.is_alive() or item is None and now-self.started>8:
                            raise RuntimeError('相机未返回画面或已断开。')
                        if item and now-item[1]>3:
                            raise RuntimeError('相机画面超过3秒未更新。')
                        self.stop.wait(.005)
                        continue
                    last_seq,captured,raw = item
                    # Scan the multiplexed LED on every frame, not only on the
                    # 5 Hz publishing cadence. A complete reading is cached and
                    # may replace a partial current frame within 0.25 s.
                    for led_field in self.reader.read_led(raw):
                        led_scans[led_field['id']] += 1
                        led_scan_valid[led_field['id']] += bool(led_field['valid'])
                        if led_field['valid']:
                            led_cache[led_field['id']] = (last_seq,captured,led_field)
                    if now-last_read>=self.config['sample_interval']:
                        begin = time.perf_counter()
                        result,panels = self.reader.read(raw)
                        led_cache_hits += len(hold_recent_led_fields(
                            result['fields'],led_cache,now,led_hold_seconds))
                        ms = (time.perf_counter()-begin)*1000
                        stamp = datetime.fromtimestamp(time.time()-(time.perf_counter()-captured)).astimezone().isoformat()
                        for field in result['fields']:
                            key = field['id']
                            field['stable_value'] = trackers[key].update(field,captured)
                            valid_counts[key] += field['valid']
                            stable_counts[key] += field['stable_value'] is not None
                            if field['stable_value'] is not None and previous.get(key)!=field['stable_value']:
                                changed.writerow([stamp,key,field['name'],field['stable_value'],field['unit']])
                                previous[key] = field['stable_value']
                                changes += 1
                        samples += 1
                        all_valid += all(f['valid'] for f in result['fields'])
                        published_ns = time.monotonic_ns()
                        captured_ns = published_ns-int((time.perf_counter()-captured)*1e9)
                        state = {**result,'state':'running','pid':os.getpid(),'camera':self.config['camera'],
                            'run_id':self.run_id,'frame_seq':last_seq,
                            'captured_monotonic_ns':captured_ns,'published_monotonic_ns':published_ns,
                            'captured_at':stamp,'elapsed_seconds':round(captured-self.started,2),
                            'actual_resolution':[raw.shape[1],raw.shape[0]],
                            'camera_format':self.capture.format_state,
                            'capture_fps':round(self.capture.count/(now-self.started),1),
                            'recognition_ms':round(ms,2),'samples':samples,'all_valid_samples':all_valid,
                            'all_valid_fraction':round(all_valid/samples,4),
                            'readable_fraction':{key:round(valid_counts[key]/samples,4) for key in ids},
                            'led_hold_seconds':led_hold_seconds,
                            'led_scan_counts':dict(led_scans),
                            'led_scan_readable_fraction':{key:round(led_scan_valid[key]/count,4)
                                if count else None for key,count in led_scans.items()},
                            'led_cache_hits':led_cache_hits,
                            'stable_sample_counts':dict(stable_counts),'accepted_changes':changes,
                            'manual_correct':self.correct,'manual_wrong':self.wrong,
                            'log_folder':str(self.folder),'stores_video':False}
                        writer.writerow([stamp,state['elapsed_seconds'],result['alignment']['valid'],round(ms,2)]+
                            [f.get(suffix if suffix!='raw' else 'value') if suffix!='stable' else f.get('stable_value')
                             for f in result['fields'] for suffix in ('raw','stable','unit','valid','reason')]+
                            [self.run_id,last_seq,captured_ns,published_ns])
                        stream.flush()
                        changed_stream.flush()
                        with self.lock:
                            self.current = state
                        write_json(self.snapshot_path,state,required=False)
                        if self.snapshot_path!=ROOT/'instrument_logs/latest.json':
                            write_json(ROOT/'instrument_logs/latest.json',state,required=False)
                        request = ROOT/'instrument_logs/request_frame'
                        if request.exists():
                            cv2.imwrite(str(self.folder/'review_frame.png'),raw)
                            write_json(self.folder/'review_frame.json',state)
                            request.unlink(missing_ok=True)
                        last_read = now
                    if now-last_preview>=.08:
                        preview = cv2.resize(raw,(768,432))
                        if self.preview_path and now-last_disk_preview>=.5:
                            ok,encoded=cv2.imencode('.jpg',preview,[cv2.IMWRITE_JPEG_QUALITY,85])
                            if ok:
                                try:
                                    tmp=self.preview_path.with_suffix('.tmp')
                                    tmp.write_bytes(encoded.tobytes())
                                    tmp.replace(self.preview_path)
                                except OSError:
                                    pass
                            last_disk_preview=now
                        for name,quad in state.get('panel_quads',{}).items():
                            pts = (np.float32(quad)*.4).astype(np.int32)
                            cv2.polylines(preview,[pts],True,(60,220,100),2)
                            cv2.putText(preview,name.upper(),tuple(pts[0]+[0,-6]),
                                        cv2.FONT_HERSHEY_SIMPLEX,.6,(60,220,100),1)
                        prepared = {'overview':Image.fromarray(cv2.cvtColor(preview,cv2.COLOR_BGR2RGB))}
                        for name,panel in panels.items():
                            width = 420 if name=='led' else 400
                            height = round(panel.shape[0]*width/panel.shape[1])
                            zoom = cv2.resize(panel,(width,height),interpolation=cv2.INTER_CUBIC)
                            prepared[name] = Image.fromarray(cv2.cvtColor(zoom,cv2.COLOR_BGR2RGB))
                        with self.lock:
                            self.latest = (last_seq,prepared,state)
                        last_preview = now
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.capture.close()
            final = {**state,'state':'error' if self.error else 'stopped','error':self.error,
                     'closed_at':datetime.now().astimezone().isoformat(),
                     'manual_correct':self.correct,'manual_wrong':self.wrong}
            write_json(self.folder/'summary.json',final)
            write_json(ROOT/'instrument_logs/latest.json',final,required=False)
            if self.snapshot_path!=ROOT/'instrument_logs/latest.json':
                write_json(self.snapshot_path,final,required=False)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=6)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds',type=float,default=0)
    parser.add_argument('--headless',action='store_true')
    parser.add_argument('--run-id')
    parser.add_argument('--stop-file',type=Path)
    parser.add_argument('--config',type=Path,default=ROOT/'instrument_config.json')
    parser.add_argument('--snapshot-path',type=Path)
    parser.add_argument('--preview-path',type=Path)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    # Verify the GUI runtime before opening a camera or starting background work.
    root = None if args.headless else tk.Tk()
    try:
        worker = InstrumentWorker(config,args.run_id,args.snapshot_path,args.preview_path)
    except Exception:
        if root is not None:
            root.destroy()
        raise
    if args.headless:
        try:
            while not worker.error and worker.thread.is_alive():
                if args.stop_file and args.stop_file.exists():
                    break
                if args.seconds and time.perf_counter()-worker.started>=args.seconds:
                    break
                time.sleep(.05)
        finally:
            worker.close()
        if worker.error:
            raise RuntimeError(worker.error)
        print(json.dumps({'log_folder':str(worker.folder),'state':worker.current},ensure_ascii=False))
        return
    root.title('实物仪表识别 · USB相机')
    root.geometry('1210x880')
    root.configure(bg='#101820')
    tk.Label(root,text='实物仪表 · 实时读数',font=('Microsoft YaHei',17),bg='#101820',fg='white').pack(pady=(8,3))
    stats = tk.StringVar(value='正在连接外置 USB 相机…')
    tk.Label(root,textvariable=stats,font=('Microsoft YaHei',10),bg='#101820',fg='#8edcca').pack()
    top = tk.Frame(root,bg='#101820')
    top.pack(pady=6)
    overview = tk.Label(top,bg='#080e14')
    overview.pack(side='left')
    cards = tk.Frame(top,bg='#101820',width=380)
    cards.pack(side='left',fill='both',padx=14)
    variables = {}
    for field in config['fields']:
        card = tk.Frame(cards,bg='#1b2935',padx=12,pady=4)
        card.pack(fill='x',pady=3)
        tk.Label(card,text=field['name']+('（单位待确认）' if not field.get('unit') else ''),
                 bg='#1b2935',fg='#aec2d1',font=('Microsoft YaHei',10),anchor='w').pack(fill='x')
        value = tk.StringVar(value='等待稳定')
        detail = tk.StringVar(value='正在定位显示屏')
        tk.Label(card,textvariable=value,bg='#1b2935',fg='white',font=('Microsoft YaHei',19),anchor='w').pack(fill='x')
        tk.Label(card,textvariable=detail,bg='#1b2935',fg='#8edcca',font=('Microsoft YaHei',8),anchor='w',wraplength=330).pack(fill='x')
        variables[field['id']] = (value,detail)
    bottom = tk.Frame(root,bg='#101820')
    bottom.pack(pady=2)
    panel_widgets = {}
    for name,title in [('led','上方数码管 · 校正后的相机画面'),('lcd','电参数表 · 校正后的相机画面')]:
        frame = tk.Frame(bottom,bg='#101820')
        frame.pack(side='left',anchor='n',padx=12)
        tk.Label(frame,text=title,bg='#101820',fg='#aec2d1',font=('Microsoft YaHei',10)).pack()
        label = tk.Label(frame,bg='#080e14')
        label.pack()
        panel_widgets[name] = label
    hints = tk.StringVar(value='请保持仪表和相机固定。读数不确定时显示等待，不沿用旧值；请对照实物核对。')
    tk.Label(root,textvariable=hints,bg='#101820',fg='#c0ccd8',font=('Microsoft YaHei',10),wraplength=1170).pack(pady=5)
    buttons = ttk.Frame(root)
    buttons.pack(pady=3)
    def review(correct):
        hints.set('已保存这次人工核对结果。' if worker.review(correct) else '请等待五项读数稳定后再核对。')
    ttk.Button(buttons,text='本次五项都正确',command=lambda:review(True)).pack(side='left',padx=8)
    ttk.Button(buttons,text='本次有读错',command=lambda:review(False)).pack(side='left',padx=8)
    closed = False
    def close():
        nonlocal closed
        if closed:
            return
        closed = True
        worker.close()
        root.destroy()
    ttk.Button(buttons,text='停止并保存',command=close).pack(side='left',padx=8)
    root.protocol('WM_DELETE_WINDOW',close)
    root.bind('<Escape>',lambda event:close())
    last_seq = -1
    images = {}
    def tick():
        nonlocal last_seq
        if closed:
            return
        if worker.error:
            messagebox.showerror('实物识别中断',worker.error)
            close()
            return
        with worker.lock:
            latest = worker.latest
        if latest and latest[0]!=last_seq:
            last_seq,prepared,state = latest
            for key,im in prepared.items():
                images[key] = ImageTk.PhotoImage(im)
                widget = overview if key=='overview' else panel_widgets[key]
                widget.configure(image=images[key],width=im.width,height=im.height)
            # Clear magnified stale images when alignment has been lost.
            for key,widget in panel_widgets.items():
                if key not in prepared:
                    widget.configure(image='',text='等待重新定位',fg='white')
            if state:
                for field in state['fields']:
                    value,detail = variables[field['id']]
                    stable = field.get('stable_value')
                    value.set(f"{stable} {field['unit']}".strip() if stable is not None else '等待稳定')
                    detail.set('连续多帧已确认' if stable is not None else
                               f"即时：{field['value']} {field['unit']}" if field['valid'] else field['reason'])
                stats.set(f"1920×1080 · 相机 {state['capture_fps']:.1f} 帧/秒 · 单次识别 {state['recognition_ms']:.0f} 毫秒 · 已记录 {state['samples']} 次采样")
        if (ROOT/'instrument_logs'/f'stop_{os.getpid()}').exists() or args.seconds and time.perf_counter()-worker.started>=args.seconds:
            close()
            return
        root.after(40,tick)
    root.after(20,tick)
    try:
        root.mainloop()
    finally:
        worker.close()


if __name__=='__main__':
    try:
        main()
    except Exception as exc:
        import traceback
        error_folder = ROOT/'instrument_logs'
        error_folder.mkdir(exist_ok=True)
        (error_folder/'startup_error.log').write_text(traceback.format_exc(),encoding='utf-8')
        if __import__('sys').stdout is None:
            try:
                root = tk.Tk()
                root.withdraw()
                messagebox.showerror('实物识别启动失败',str(exc))
                root.destroy()
            except tk.TclError:
                pass
        else:
            raise
