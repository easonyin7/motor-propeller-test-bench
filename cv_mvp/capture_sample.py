"""Save one local calibration frame for instrument OCR development."""
import json
import time
from pathlib import Path
import cv2
import numpy as np
from cv_camera_test import Capture, cameras

cv2.setNumThreads(1)
info = next(c for c in cameras() if c.name == '2MP USB Camera')
capture = Capture(info,1920,1080)
start = time.perf_counter()
best = None
last = -1
try:
    while time.perf_counter()-start < 5:
        item = capture.get()
        if item and item[0] != last:
            last,stamp,frame = item
            if np.mean(frame)>2:
                best = frame.copy()
        time.sleep(.02)
finally:
    capture.close()
if best is None:
    raise RuntimeError('Camera returned no usable image; reconnect USB.')
folder = Path(__file__).resolve().parent/'calibration'
folder.mkdir(exist_ok=True)
cv2.imwrite(str(folder/'frame.png'),best)
print(json.dumps({'path':str(folder/'frame.png'),'shape':list(best.shape),'brightness':float(np.mean(best))}))
