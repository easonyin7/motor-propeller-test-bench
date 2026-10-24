"""Camera statistics; optional exposure reset. Never stores images."""
import json
import argparse
import time
import cv2
import numpy as np
from cv2_enumerate_cameras import enumerate_cameras

cv2.setNumThreads(1)
parser = argparse.ArgumentParser()
parser.add_argument('--auto-exposure',action='store_true')
args = parser.parse_args()
results = []
modes = [(cv2.CAP_DSHOW,'MJPG')] if args.auto_exposure else [(cv2.CAP_DSHOW,'default'),(cv2.CAP_DSHOW,'MJPG'),(cv2.CAP_MSMF,'MJPG')]
for backend, mode in modes:
    info = next(c for c in enumerate_cameras(backend) if c.name=='2MP USB Camera')
    cap = cv2.VideoCapture(info.index,info.backend)
    if mode == 'MJPG':
        cap.set(cv2.CAP_PROP_FOURCC,cv2.VideoWriter_fourcc(*'MJPG'))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT,720)
        cap.set(cv2.CAP_PROP_FPS,30)
    auto_set = cap.set(cv2.CAP_PROP_AUTO_EXPOSURE,1) if args.auto_exposure else None
    row = {'backend':backend,'mode':mode,'opened':cap.isOpened(),
           'auto_exposure_set':auto_set,
           'reported_fps':cap.get(cv2.CAP_PROP_FPS),
           'exposure':cap.get(cv2.CAP_PROP_EXPOSURE),
           'auto_exposure':cap.get(cv2.CAP_PROP_AUTO_EXPOSURE)}
    start = time.perf_counter()
    means = []
    shapes = []
    while time.perf_counter()-start < 5:
        ok,frame = cap.read()
        if ok:
            means.append(float(np.mean(frame)))
            shapes.append(list(frame.shape))
        else:
            time.sleep(.03)
    row.update(frames=len(means),fps=round(len(means)/(time.perf_counter()-start),1),
               mean_brightness=round(sum(means)/len(means),2) if means else None,
               last_shape=shapes[-1] if shapes else None)
    cap.release()
    results.append(row)
    print(json.dumps(row),flush=True)
    time.sleep(.5)
print(json.dumps(results),flush=True)
