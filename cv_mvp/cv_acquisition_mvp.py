"""Two-field seven-segment camera acquisition MVP with local CSV logging."""
import argparse
import csv
import json
import os
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox
import cv2
import numpy as np
from PIL import Image, ImageTk
from cv_camera_test import Capture, cameras, write_json
from seven_segment import read_auto_field, Stability

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT/'digit_config.json'


class DigitWorker:
    def __init__(self,config):
        self.config = config
        matches = [c for c in cameras() if c.name==config['camera']]
        if len(matches)!=1:
            raise RuntimeError('未唯一匹配 2MP USB Camera。')
        self.capture = Capture(matches[0],config['width'],config['height'])
        self.started = time.perf_counter()
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.current = None
        self.error = None
        self.folder = ROOT/'digit_logs'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        self.folder.mkdir(parents=True)
        self.samples = 0
        self.valid_samples = 0
        self.changes = []
        self.previous_pair = None
        self.review_correct = 0
        self.review_wrong = 0
        self.version = 0
        self.thread = threading.Thread(target=self.run,daemon=True)
        self.thread.start()

    def fields(self):
        with self.lock:
            return json.loads(json.dumps(self.config['fields'])),self.version

    def set_roi(self,index,box):
        with self.lock:
            field = self.config['fields'][index]
            ox,oy,ow,oh = field['roi']
            nx,ny,nw,nh = box
            def remap(cell):
                x,y,w,h = cell
                return [round(nx+(x-ox)*nw/ow),round(ny+(y-oy)*nh/oh),round(w*nw/ow),round(h*nh/oh)]
            if 'reference_cells' in field:
                field['reference_cells'] = [remap(c) for c in field['reference_cells']]
                field['reference_decimal'] = remap(field['reference_decimal'])
                if 'dot_anchor' in field:
                    ax,ay = field['dot_anchor']
                    field['dot_anchor'] = [round(nx+(ax-ox)*nw/ow),round(ny+(ay-oy)*nh/oh)]
            self.config['fields'][index]['roi'] = box
            self.version += 1
            saved = json.loads(json.dumps(self.config))
        write_json(CONFIG,saved)

    def review(self,correct):
        with self.lock:
            s = json.loads(json.dumps(self.current)) if self.current else None
            if not s or any(r['stable_value'] is None for r in s['fields']):
                return False
            if correct:
                self.review_correct += 1
            else:
                self.review_wrong += 1
            with (self.folder/'manual_reviews.jsonl').open('a',encoding='utf-8') as file:
                file.write(json.dumps({'reviewed_at':datetime.now().astimezone().isoformat(),
                           'camera_sample_time':s['captured_at'],'values':[r['stable_value'] for r in s['fields']],
                           'user_says_correct':correct},ensure_ascii=False)+'\n')
        return True

    def run(self):
        trackers = [Stability(self.config['stable_seconds']) for _ in range(2)]
        version = -1
        last_seq = -1
        last_read = -1.
        last_pair_time = None
        recent = deque(maxlen=60)
        readings = []
        state = {}
        try:
            with (self.folder/'samples.csv').open('w',newline='',encoding='utf-8-sig') as samples_file, \
                 (self.folder/'changes.csv').open('w',newline='',encoding='utf-8-sig') as changes_file:
                writer = csv.writer(samples_file)
                changed = csv.writer(changes_file)
                writer.writerow(['captured_at','elapsed_seconds','upper_raw','lower_raw','upper_stable',
                                 'lower_stable','upper_valid','lower_valid','upper_reason','lower_reason','recognition_ms'])
                changed.writerow(['accepted_at','elapsed_seconds','upper','lower','seconds_since_previous'])
                while not self.stop.is_set():
                    item = self.capture.get()
                    now = time.perf_counter()
                    if item is None or item[0]==last_seq:
                        if not self.capture.thread.is_alive() or item is None and now-self.started>8:
                            raise RuntimeError('相机没有返回图像或已断开。')
                        if item and now-item[1]>3:
                            raise RuntimeError('相机取流停滞。')
                        self.stop.wait(.005)
                        continue
                    last_seq,captured,raw = item
                    sample_request = ROOT/'calibration/request_frame'
                    if sample_request.exists():
                        cv2.imwrite(str(ROOT/'calibration/live_sample.png'),raw)
                        write_json(ROOT/'calibration/live_sample.json',{'captured_at':datetime.now().astimezone().isoformat(),'shape':list(raw.shape)})
                        sample_request.unlink()
                    fields,current_version = self.fields()
                    if version!=current_version:
                        trackers = [Stability(self.config['stable_seconds']) for _ in fields]
                        version = current_version
                        last_read = -1
                    h,w = raw.shape[:2]
                    if now-last_read>=self.config['sample_interval']:
                        begin = time.perf_counter()
                        readings = []
                        for field,tracker in zip(fields,trackers):
                            result = read_auto_field(raw,field)
                            result['stable_value'] = tracker.update(result,captured)
                            result['name'] = field['name']
                            readings.append(result)
                        ms = (time.perf_counter()-begin)*1000
                        elapsed = captured-self.started
                        stamp = datetime.fromtimestamp(time.time()-(time.perf_counter()-captured)).astimezone().isoformat()
                        self.samples += 1
                        self.valid_samples += all(r['valid'] for r in readings)
                        pair = tuple(r['stable_value'] for r in readings)
                        if all(v is not None for v in pair) and pair!=self.previous_pair:
                            gap = None if last_pair_time is None else round(elapsed-last_pair_time,2)
                            event = {'accepted_at':stamp,'elapsed_seconds':round(elapsed,2),
                                     'values':list(pair),'seconds_since_previous':gap}
                            self.changes.append(event)
                            changed.writerow([stamp,round(elapsed,2),*pair,gap])
                            changes_file.flush()
                            self.previous_pair = pair
                            last_pair_time = elapsed
                        writer.writerow([stamp,round(elapsed,2),*[r['value'] for r in readings],
                                         *[r['stable_value'] for r in readings],*[r['valid'] for r in readings],
                                         *[r.get('reason') for r in readings],round(ms,2)])
                        samples_file.flush()
                        state = {'state':'running','camera':self.config['camera'],'actual_resolution':[w,h],
                                 'pid':os.getpid(),'captured_at':stamp,'elapsed_seconds':round(elapsed,2),
                                 'fields':readings,'samples':self.samples,'both_valid_samples':self.valid_samples,
                                 'valid_sample_fraction':round(self.valid_samples/self.samples,4),
                                 'recognized_changes':len(self.changes),'recent_changes':self.changes[-8:],
                                 'recognition_ms':round(ms,2),'capture_fps':round(self.capture.count/(now-self.started),1),
                                 'mean_pixel_brightness':round(float(np.mean(raw)),2),
                                 'log_folder':str(self.folder),'stores_video':False,
                                 'manual_correct':self.review_correct,'manual_wrong':self.review_wrong,
                                 'roi_version':version}
                        with self.lock:
                            self.current = state
                        write_json(ROOT/'digit_logs/latest.json',state,required=False)
                        last_read = now
                    # No mirroring: the numeric glyphs must keep their actual direction.
                    preview = cv2.resize(raw,(960,round(h*960/w)))
                    for i,field in enumerate(fields):
                        x,y,bw,bh = field['roi']
                        cv2.rectangle(preview,(round(x*960/w),round(y*960/w)),
                                      (round((x+bw)*960/w),round((y+bh)*960/w)),(60,220,100),2)
                        text = readings[i]['stable_value'] if i<len(readings) else None
                        cv2.putText(preview,f'{i+1}: {text or "waiting"}',
                                    (round(x*960/w),max(20,round(y*960/w)-7)),cv2.FONT_HERSHEY_SIMPLEX,.7,(60,220,100),2)
                    prepared = Image.fromarray(cv2.cvtColor(preview,cv2.COLOR_BGR2RGB))
                    recent.append(now)
                    with self.lock:
                        self.latest = (last_seq,prepared,state)
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.capture.close()
            final = {**state,'state':'error' if self.error else 'stopped','error':self.error,
                     'closed_at':datetime.now().astimezone().isoformat(),
                     'manual_correct':self.review_correct,'manual_wrong':self.review_wrong,
                     'all_changes':self.changes}
            write_json(self.folder/'summary.json',final)
            write_json(ROOT/'digit_logs/latest.json',final,required=False)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--seconds',type=float,default=0)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    config = json.loads(CONFIG.read_text(encoding='utf-8'))
    worker = DigitWorker(config)
    root = tk.Tk()
    root.title('数字识别测试 · 两组七段数字 · 1080P')
    root.geometry('1120x860')
    root.configure(bg='#101820')
    tk.Label(root,text='两组数字 · 实时识别',font=('Microsoft YaHei',18),fg='white',bg='#101820').pack(pady=7)
    stats = tk.StringVar(value='正在等待 1080P 相机画面…')
    tk.Label(root,textvariable=stats,font=('Microsoft YaHei',10),fg='#80d9c6',bg='#101820').pack()
    canvas = tk.Canvas(root,width=960,height=540,bg='#080e14',highlightthickness=0)
    canvas.pack(pady=8)
    image_item = canvas.create_image(0,0,anchor='nw')
    values = tk.StringVar(value='上方：等待稳定  |  下方：等待稳定')
    tk.Label(root,textvariable=values,font=('Microsoft YaHei',21),fg='white',bg='#101820').pack(pady=4)
    details = tk.StringVar()
    tk.Label(root,textvariable=details,font=('Microsoft YaHei',10),fg='#c0ccd8',bg='#101820',wraplength=1090).pack()
    hints = tk.StringVar(value='保持相机位置固定。核对屏幕实际数字；刷新时短暂“等待稳定”是正常的。')
    tk.Label(root,textvariable=hints,font=('Microsoft YaHei',10),fg='#c0ccd8',bg='#101820',wraplength=1090).pack(pady=5)
    buttons = ttk.Frame(root)
    buttons.pack(pady=5)
    selection = {'index':None,'start':None,'rectangle':None}
    def select(index):
        selection['index'] = index
        hints.set(f'在预览中拖动框选第 {index+1} 行整组数字，包含小数点；上下留约20像素空白。')
    def pressed(event):
        if selection['index'] is not None:
            selection['start'] = (event.x,event.y)
            selection['rectangle'] = canvas.create_rectangle(event.x,event.y,event.x,event.y,outline='#ffdf60',width=2)
    def dragged(event):
        if selection['start']:
            canvas.coords(selection['rectangle'],*selection['start'],event.x,event.y)
    def released(event):
        if not selection['start']:
            return
        x,y = selection['start']
        with worker.lock:
            s = worker.current
        if s:
            scale = s['actual_resolution'][0]/960
            box = [round(min(x,event.x)*scale),round(min(y,event.y)*scale),
                   round(abs(x-event.x)*scale),round(abs(y-event.y)*scale)]
            if box[2]>50 and box[3]>50 and box[0]>=0 and box[1]>=0 and box[0]+box[2]<=s['actual_resolution'][0] and box[1]+box[3]<=s['actual_resolution'][1]:
                worker.set_roi(selection['index'],box)
                hints.set('区域已保存；重新等待连续稳定读数。')
        canvas.delete(selection['rectangle'])
        selection.update(index=None,start=None,rectangle=None)
    canvas.bind('<ButtonPress-1>',pressed)
    canvas.bind('<B1-Motion>',dragged)
    canvas.bind('<ButtonRelease-1>',released)
    ttk.Button(buttons,text='重选上方区域',command=lambda:select(0)).pack(side='left',padx=5)
    ttk.Button(buttons,text='重选下方区域',command=lambda:select(1)).pack(side='left',padx=5)
    def review(correct):
        if worker.review(correct):
            hints.set('已记录本次人工核对结果；请在数字刷新前完成核对。')
        else:
            hints.set('目前没有两组稳定值，请等读数稳定后核对。')
    ttk.Button(buttons,text='本次两组都正确',command=lambda:review(True)).pack(side='left',padx=5)
    ttk.Button(buttons,text='本次有读错',command=lambda:review(False)).pack(side='left',padx=5)
    closed = False
    def close():
        nonlocal closed
        if closed:
            return
        closed = True
        worker.close()
        root.destroy()
    ttk.Button(buttons,text='停止并保存结果',command=close).pack(side='left',padx=5)
    root.protocol('WM_DELETE_WINDOW',close)
    root.bind('<Escape>',lambda event:close())
    last_seq = -1
    def tick():
        nonlocal last_seq
        if closed:
            return
        if worker.error:
            messagebox.showerror('数字识别测试中断',worker.error)
            close()
            return
        with worker.lock:
            latest = worker.latest
        if latest and latest[0]!=last_seq:
            last_seq,prepared,s = latest
            rendered = ImageTk.PhotoImage(prepared)
            canvas.itemconfigure(image_item,image=rendered)
            canvas.image = rendered
            if s:
                readings = s['fields']
                values.set('  |  '.join(f"{r['name']}：{r['stable_value'] or '等待稳定'}" for r in readings))
                w,h = s['actual_resolution']
                stats.set(f"相机 {w}×{h} · 采集 {s['capture_fps']:.1f} 帧/秒 · 每秒约5次识别 · 识别耗时 {s['recognition_ms']:.0f} 毫秒")
                raw = ' / '.join(r['value'] or r.get('reason','不确定') for r in readings)
                details.set(f"即时结果：{raw}  |  已记录 {s['recognized_changes']} 组稳定变化  |  两组均可读样本 {s['both_valid_samples']}/{s['samples']}（可读率，不是正确率）")
                if s['mean_pixel_brightness']<2:
                    hints.set('相机返回近黑画面，请退出测试、检查镜头并重新插拔 USB。')
                elif s['recognized_changes'] and selection['index'] is None:
                    gap = s['recent_changes'][-1]['seconds_since_previous']
                    if gap is not None:
                        hints.set(f'最近两组稳定变化间隔 {gap:.1f} 秒；预期约10秒。请对照实际显示核对。')
        if (ROOT/'digit_logs'/f'stop_{os.getpid()}').exists() or args.seconds and time.perf_counter()-worker.started>=args.seconds:
            close()
            return
        root.after(40,tick)
    root.after(20,tick)
    try:
        root.mainloop()
    finally:
        worker.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if not __import__('sys').stdout:
            error_root = tk.Tk()
            error_root.withdraw()
            messagebox.showerror('数字识别启动失败',str(exc))
            error_root.destroy()
        else:
            raise
