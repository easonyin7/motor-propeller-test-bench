"""Local USB camera, face detection and gesture recognition smoke test."""
import argparse
import csv
import json
import os
import threading
import time
from collections import Counter, deque
from datetime import datetime
from pathlib import Path

import cv2
import mediapipe as mp
import numpy as np
from cv2_enumerate_cameras import enumerate_cameras
from mediapipe.tasks import python
from mediapipe.tasks.python import vision

ROOT = Path(__file__).resolve().parent
GESTURES = {
    'Closed_Fist': '握拳', 'Open_Palm': '张开手掌', 'Pointing_Up': '食指向上',
    'Thumb_Down': '拇指向下', 'Thumb_Up': '点赞', 'Victory': 'V 手势',
    'ILoveYou': '我爱你手势', 'None': '未分类',
}
EDGES = [(0,1),(1,2),(2,3),(3,4),(0,5),(5,6),(6,7),(7,8),(5,9),
         (9,10),(10,11),(11,12),(9,13),(13,14),(14,15),(15,16),
         (13,17),(0,17),(17,18),(18,19),(19,20)]


def write_json(path, data, required=True):
    # Unique scratch paths avoid cross-process collisions. Windows readers may
    # briefly deny replacement of an open destination, so retry the rename.
    temp = path.with_suffix(f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    for attempt in range(12):
        try:
            temp.replace(path)
            return True
        except PermissionError:
            if attempt==11:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                if required:
                    raise
                return False
            time.sleep(.05)


def cameras():
    return list(enumerate_cameras(cv2.CAP_DSHOW))


class Capture:
    """Always consume the camera, keeping only the newest frame."""
    def __init__(self, info, width, height):
        self.cap = cv2.VideoCapture(info.index, info.backend)
        if not self.cap.isOpened():
            raise RuntimeError('无法打开 USB 相机；请关闭其他占用相机的软件。')
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
        self.cap.set(cv2.CAP_PROP_FPS, 30)
        # Some drivers reselect YUY2 when resolution/FPS changes. Set MJPG last.
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
        actual_fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        self.format_state = {
            'fourcc': ''.join(chr((actual_fourcc >> (8*i)) & 255) for i in range(4)),
            'width': self.cap.get(cv2.CAP_PROP_FRAME_WIDTH),
            'height': self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT),
            'fps_reported': self.cap.get(cv2.CAP_PROP_FPS),
        }
        self.lock = threading.Lock()
        self.latest = None
        self.count = 0
        self.failures = 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        try:
            while not self.stop.is_set():
                ok, frame = self.cap.read()
                if not ok:
                    self.failures += 1
                    if self.failures >= 30:
                        break
                    time.sleep(.02)
                    continue
                self.failures = 0
                with self.lock:
                    self.count += 1
                    self.latest = (self.count, time.perf_counter(), frame)
        finally:
            self.cap.release()

    def get(self):
        with self.lock:
            return self.latest

    def close(self):
        self.stop.set()
        self.thread.join(timeout=3)


class Session:
    def __init__(self, args):
        matches = [c for c in cameras() if c.name.casefold() == args.camera.casefold()]
        if len(matches) != 1:
            raise RuntimeError(f'相机名称未唯一匹配：{args.camera}；使用 cameras 命令查看设备。')
        info = matches[0]
        self.face = vision.FaceDetector.create_from_options(vision.FaceDetectorOptions(
            base_options=python.BaseOptions(model_asset_path=str(ROOT/'models/blaze_face_short_range.tflite')),
            running_mode=vision.RunningMode.VIDEO, min_detection_confidence=.6))
        try:
            self.hand = vision.GestureRecognizer.create_from_options(vision.GestureRecognizerOptions(
                base_options=python.BaseOptions(model_asset_path=str(ROOT/'models/gesture_recognizer.task')),
                running_mode=vision.RunningMode.VIDEO, num_hands=2,
                min_hand_detection_confidence=.6, min_hand_presence_confidence=.6,
                min_tracking_confidence=.6))
        except Exception:
            self.face.close()
            raise
        try:
            self.capture = Capture(info, args.width, args.height)
        except Exception:
            self.face.close()
            self.hand.close()
            raise
        self.started = time.perf_counter()
        self.last_seq = 0
        self.last_timestamp = -1
        self.last_log = -1.
        self.frames = 0
        self.faces_seen = 0
        self.hands_seen = 0
        self.histogram = Counter()
        self.latencies = deque(maxlen=300)
        self.times = deque(maxlen=60)
        self.score_threshold = args.threshold
        self.meta = {'camera':info.name, 'camera_index':info.index,
                     'backend': 'DirectShow', 'requested_resolution':[args.width,args.height],
                     'gesture_threshold':args.threshold, 'stores_images':False}
        folder = ROOT/'logs'/datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        folder.mkdir(parents=True)
        self.folder = folder
        self.file = (folder/'metrics.csv').open('w', newline='', encoding='utf-8-sig')
        self.writer = csv.writer(self.file)
        self.writer.writerow(['captured_at','frame','width','height','face_count',
                              'face_confidence','hand_count','gestures_json','inference_ms',
                              'frame_age_ms','processing_fps','capture_fps'])
        self.current = {}
        self.closed = False

    def step(self, mirror=True):
        latest = self.capture.get()
        if latest is None or latest[0] == self.last_seq:
            if not self.capture.thread.is_alive() or time.perf_counter()-self.started > 8 and latest is None:
                raise RuntimeError('相机没有返回图像或已断开连接。')
            if latest is not None and time.perf_counter()-latest[1] > 3:
                raise RuntimeError('相机取流超过 3 秒未更新。')
            return None
        seq, captured, raw = latest
        self.last_seq = seq
        frame = cv2.flip(raw,1) if mirror else raw.copy()
        h,w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # Inference at <=960px width; overlays scale to the actual captured frame.
        if w > 960:
            rgb = cv2.resize(rgb,(960,round(h*960/w)))
        brightness = round(float(np.mean(rgb)),2)
        scale = w/rgb.shape[1]
        image = mp.Image(image_format=mp.ImageFormat.SRGB,data=np.ascontiguousarray(rgb))
        timestamp = max(self.last_timestamp+1, int((captured-self.started)*1000))
        self.last_timestamp = timestamp
        begin = time.perf_counter()
        face = self.face.detect_for_video(image,timestamp)
        hand = self.hand.recognize_for_video(image,timestamp)
        ms = (time.perf_counter()-begin)*1000
        self.latencies.append(ms)
        self.frames += 1
        self.faces_seen += bool(face.detections)
        self.hands_seen += bool(hand.hand_landmarks)
        scores = []
        for det in face.detections:
            score = det.categories[0].score
            scores.append(score)
            box = det.bounding_box
            x,y,bw,bh = [round(v*scale) for v in (box.origin_x,box.origin_y,box.width,box.height)]
            cv2.rectangle(frame,(x,y),(x+bw,y+bh),(50,220,80),2)
            cv2.putText(frame,f'Face {score:.2f}',(max(x,0),max(y-8,20)),cv2.FONT_HERSHEY_SIMPLEX,.65,(50,220,80),2)
        labels = []
        for i, points in enumerate(hand.hand_landmarks):
            category = hand.gestures[i][0] if hand.gestures[i] else None
            name = category.category_name if category else 'None'
            score = category.score if category else 0.
            accepted = name != 'None' and score >= self.score_threshold
            label = name if accepted else 'None'
            labels.append({'gesture':label,'raw_gesture':name,'confidence':round(score,4)})
            if accepted:
                self.histogram[name] += 1
            coords = [(round(p.x*w),round(p.y*h)) for p in points]
            for a,b in EDGES:
                cv2.line(frame,coords[a],coords[b],(255,190,40),2)
            for p in coords:
                cv2.circle(frame,p,4,(40,80,255),-1)
            cv2.putText(frame,f'{label} {score:.2f}',coords[0],cv2.FONT_HERSHEY_SIMPLEX,.6,(255,190,40),2)
        now = time.perf_counter()
        self.times.append(now)
        fps = (len(self.times)-1)/(self.times[-1]-self.times[0]) if len(self.times)>1 else 0
        elapsed = now-self.started
        self.meta['actual_resolution'] = [w,h]
        self.current = {**self.meta, 'state':'running', 'updated_at':datetime.now().astimezone().isoformat(),
                        'elapsed_seconds':round(elapsed,2), 'processed_frames':self.frames,
                        'captured_frames':self.capture.count, 'face_count':len(scores),
                        'face_confidence':round(max(scores,default=0),4), 'hands':labels,
                        'face_detected_frames':self.faces_seen,'hand_detected_frames':self.hands_seen,
                        'gesture_detected_frames':dict(self.histogram),
                        'processing_fps':round(fps,1),'capture_fps':round(self.capture.count/elapsed,1),
                        'inference_ms':round(ms,1),'mean_inference_ms':round(sum(self.latencies)/len(self.latencies),1),
                        'mean_pixel_brightness':brightness,
                        'frame_age_ms':round((now-captured)*1000,1), 'log_folder':str(self.folder)}
        if elapsed-self.last_log >= .2:
            self.writer.writerow([datetime.fromtimestamp(time.time()-(now-captured)).astimezone().isoformat(),
                                  seq,w,h,len(scores),self.current['face_confidence'],len(labels),
                                  json.dumps(labels),round(ms,1),self.current['frame_age_ms'],
                                  round(fps,1),self.current['capture_fps']])
            self.file.flush()
            write_json(ROOT/'logs/latest.json', self.current,required=False)
            self.last_log = elapsed
        return frame,self.current

    def close(self, error=None):
        if self.closed:
            return
        self.closed = True
        self.capture.close()
        self.face.close()
        self.hand.close()
        self.file.close()
        final = {**self.meta, **self.current, 'state':'error' if error else 'stopped', 'error':error,
                 'closed_at':datetime.now().astimezone().isoformat()}
        write_json(self.folder/'summary.json',final)
        write_json(ROOT/'logs/latest.json',final,required=False)
        print(json.dumps(final,ensure_ascii=False),flush=True)


class PreviewWorker:
    """Inference and resizing never run on Tk's event thread."""
    def __init__(self, session, mirror):
        self.session = session
        self.mirror = mirror
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.latest = None
        self.error = None
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        from PIL import Image
        try:
            while not self.stop.is_set():
                item = self.session.step(self.mirror)
                if item:
                    frame,status = item
                    h,w = frame.shape[:2]
                    # Do all image conversion away from the UI thread.
                    factor = min(960/w,540/h,1)
                    if factor < 1:
                        frame = cv2.resize(frame,(round(w*factor),round(h*factor)))
                    prepared = Image.fromarray(cv2.cvtColor(frame,cv2.COLOR_BGR2RGB))
                    with self.lock:
                        self.latest = (prepared,status)
                else:
                    self.stop.wait(.003)
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.session.close(self.error)

    def get(self):
        with self.lock:
            return self.latest

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5)


def run_gui(session, args):
    import tkinter as tk
    from tkinter import ttk
    from PIL import ImageTk
    window = tk.Tk()
    window.title('CV 能力测试 · 2MP USB Camera')
    window.geometry('1100x850')
    window.configure(bg='#101820')
    status = tk.StringVar(value='正在等待 USB 相机画面…')
    result = tk.StringVar(value='面对相机，再伸出一只手。')
    history = tk.StringVar()
    tk.Label(window,text='人脸与手势 · 实时测试',font=('Microsoft YaHei',19),fg='#f0f4f8',bg='#101820').pack(pady=8)
    tk.Label(window,textvariable=status,font=('Microsoft YaHei',11),fg='#80d9c6',bg='#101820').pack()
    video = tk.Label(window,bg='#080e14')
    video.pack(padx=12,pady=8)
    tk.Label(window,textvariable=result,font=('Microsoft YaHei',14),fg='white',bg='#101820').pack()
    tk.Label(window,textvariable=history,font=('Microsoft YaHei',10),fg='#80d9c6',bg='#101820',wraplength=1060).pack(pady=5)
    tk.Label(window,text='试一试：张开手掌 → 握拳 → 点赞 → V 手势；每个动作保持 2–3 秒。\n模型得分用于显示置信度；实际正确与否请对照自己的动作观察。',
             font=('Microsoft YaHei',11),fg='#c0ccd8',bg='#101820').pack(pady=5)
    controls = ttk.Frame(window)
    controls.pack(pady=8)
    mirror = tk.BooleanVar(value=not args.no_mirror)
    worker = PreviewWorker(session,mirror.get())
    ttk.Checkbutton(controls,text='镜像画面',variable=mirror,
                    command=lambda:setattr(worker,'mirror',mirror.get())).pack(side='left',padx=15)
    closed = False
    def close(error=None):
        nonlocal closed
        if closed:
            return
        closed = True
        if error:
            worker.error = error
        worker.close()
        window.destroy()
    ttk.Button(controls,text='停止测试并退出',command=close).pack(side='left',padx=15)
    window.protocol('WM_DELETE_WINDOW',close)
    window.bind('<Escape>',lambda event:close())
    last_frame = 0
    render_times = deque(maxlen=60)
    last_ui_log = 0.
    def tick():
        nonlocal last_frame,last_ui_log
        if closed:
            return
        try:
            if worker.error:
                raise RuntimeError(worker.error)
            item = worker.get()
            if item and item[1]['processed_frames'] != last_frame:
                img,s = item
                last_frame = s['processed_frames']
                rendered = ImageTk.PhotoImage(img)
                video.configure(image=rendered)
                video.image = rendered
                now = time.perf_counter()
                render_times.append(now)
                w,h = s['actual_resolution']
                status.set(f"USB 相机 {w}×{h}  |  采集 {s['capture_fps']:.1f} 帧/秒  |  处理 {s['processing_fps']:.1f} 帧/秒  |  推理 {s['inference_ms']:.0f} 毫秒")
                texts = [('未分类' if x['gesture']=='None' else
                          f"{GESTURES.get(x['gesture'],x['gesture'])}（{x['confidence']:.0%}）") for x in s['hands']]
                result.set(f"人脸：{s['face_count']}  |  手：{len(texts)}  |  "+(' / '.join(texts) or '未检测到手'))
                if s.get('mean_pixel_brightness',255) < 2:
                    result.set('相机返回近黑画面：请检查镜头、光线；必要时退出测试并重新插拔 USB。')
                history.set('本次已检测：'+('、'.join(GESTURES[k] for k in s['gesture_detected_frames']) or '等待手势')+'  |  只保存指标，不保存照片或录像')
                if now-last_ui_log > 1:
                    ui_fps = (len(render_times)-1)/(render_times[-1]-render_times[0]) if len(render_times)>1 else 0
                    write_json(ROOT/'logs/gui.json',{'pid':os.getpid(),'title':window.title(),
                               'visible':bool(window.winfo_viewable()),'state':'running',
                               'rendered_frame':last_frame,'preview_fps':round(ui_fps,1),
                               'inference_on_ui_thread':False,'updated_at':datetime.now().astimezone().isoformat()},required=False)
                    last_ui_log = now
            if (ROOT/'logs'/f'stop_{os.getpid()}').exists():
                close()
                return
            if args.seconds and time.perf_counter()-session.started >= args.seconds:
                close()
                return
        except Exception as exc:
            from tkinter import messagebox
            messagebox.showerror('测试中断',str(exc))
            close(str(exc))
            return
        # Limit refresh frequency so pointer/window events retain plenty of time.
        window.after(33,tick)
    window.after(10,tick)
    try:
        window.mainloop()
    finally:
        worker.close()
        write_json(ROOT/'logs/gui.json',{'pid':os.getpid(),'state':'stopped',
                   'inference_on_ui_thread':False,'updated_at':datetime.now().astimezone().isoformat()},required=False)


def main():
    cv2.setNumThreads(1)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['cameras','probe','run'],nargs='?',default='run')
    parser.add_argument('--camera',default='2MP USB Camera')
    parser.add_argument('--width',type=int,default=1280)
    parser.add_argument('--height',type=int,default=720)
    parser.add_argument('--threshold',type=float,default=.65)
    parser.add_argument('--seconds',type=float,default=0)
    parser.add_argument('--no-mirror',action='store_true')
    args = parser.parse_args()
    if not 0 <= args.threshold <= 1 or min(args.width,args.height) <= 0 or args.seconds < 0:
        parser.error('invalid resolution, threshold or duration')
    if args.action == 'cameras':
        for c in cameras():
            print(json.dumps({'name':c.name,'index':c.index,'backend':c.backend,'vid':c.vid,'pid':c.pid},ensure_ascii=False))
        return
    session = Session(args)
    if args.action == 'run':
        try:
            run_gui(session,args)
        except BaseException as exc:
            session.close(str(exc))
            raise
    else:
        error = None
        try:
            while time.perf_counter()-session.started < (args.seconds or 12):
                session.step(not args.no_mirror)
                time.sleep(.003)
        except BaseException as exc:
            error = str(exc)
            raise
        finally:
            session.close(error)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        if not __import__('sys').stdout:
            import tkinter as tk
            from tkinter import messagebox
            error_window = tk.Tk()
            error_window.withdraw()
            messagebox.showerror('CV 测试启动失败',str(exc))
            error_window.destroy()
        else:
            raise
