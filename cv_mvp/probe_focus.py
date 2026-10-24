"""Probe USB-camera focus controls, restoring changed settings on exit."""
import json
import math
import time
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
from cv2_enumerate_cameras import enumerate_cameras


def main():
    cv2.setNumThreads(1)
    matches = [c for c in enumerate_cameras(cv2.CAP_DSHOW)
               if c.name == '2MP USB Camera']
    if len(matches) != 1:
        raise RuntimeError('USB camera not uniquely found')
    info = matches[0]
    cap = cv2.VideoCapture(info.index, info.backend)
    if not cap.isOpened():
        raise RuntimeError('Cannot open USB camera')
    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1920)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 1080)
    cap.set(cv2.CAP_PROP_FPS, 30)
    folder = Path(__file__).resolve().parent / 'calibration' / (
        'focus_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    folder.mkdir(parents=True, exist_ok=True)
    original = {name: cap.get(prop) for name, prop in (
        ('focus', cv2.CAP_PROP_FOCUS), ('autofocus', cv2.CAP_PROP_AUTOFOCUS))}
    result = {'camera': info.name, 'original': original, 'trials': [], 'restored': {}}
    changed = set()

    def sample(label, seconds=2):
        start = time.perf_counter()
        frames = 0
        last = None
        sharpness = []
        while time.perf_counter() - start < seconds:
            ok, frame = cap.read()
            if not ok:
                continue
            frames += 1
            last = frame
            # Same fixed physical-display regions for within-scene comparisons only.
            if time.perf_counter() - start > seconds / 2:
                values = []
                for x, y, w, h in ((870, 240, 200, 95), (780, 545, 335, 215)):
                    roi = frame[y:y+h, x:x+w]
                    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
                    values.append(float(cv2.Laplacian(gray, cv2.CV_64F).var()))
                sharpness.append(values)
        data = {'frames': frames, 'focus': cap.get(cv2.CAP_PROP_FOCUS),
                'autofocus': cap.get(cv2.CAP_PROP_AUTOFOCUS)}
        if last is not None:
            path = folder / (label + '.png')
            if not cv2.imwrite(str(path), last):
                raise RuntimeError('Failed to save diagnostic image')
            data.update(image=str(path), shape=list(last.shape),
                        brightness=float(np.mean(last)))
        if sharpness:
            data['median_laplacian_led_lcd'] = np.median(sharpness, axis=0).tolist()
        return data

    try:
        result['baseline'] = sample('baseline', 3)
        print(json.dumps({'baseline': result['baseline'], 'original': original}), flush=True)
        for value in (1, 0):
            accepted = bool(cap.set(cv2.CAP_PROP_AUTOFOCUS, value))
            row = {'property': 'autofocus', 'requested': value, 'accepted': accepted,
                   'readback': cap.get(cv2.CAP_PROP_AUTOFOCUS)}
            if accepted:
                changed.add('autofocus')
                row['sample'] = sample('autofocus_' + str(value), 3)
            result['trials'].append(row)
            print(json.dumps(row), flush=True)
        # A few conventional values probe whether any manual focus is accepted.
        for value in (0, 64, 128, 255):
            accepted = bool(cap.set(cv2.CAP_PROP_FOCUS, value))
            row = {'property': 'focus', 'requested': value, 'accepted': accepted,
                   'readback': cap.get(cv2.CAP_PROP_FOCUS)}
            if accepted:
                changed.add('focus')
                row['sample'] = sample('focus_' + str(value), 2)
            result['trials'].append(row)
            print(json.dumps(row), flush=True)
    finally:
        for name, prop in (('focus', cv2.CAP_PROP_FOCUS),
                           ('autofocus', cv2.CAP_PROP_AUTOFOCUS)):
            if name in changed and math.isfinite(original[name]) and original[name] >= 0:
                result['restored'][name] = {
                    'accepted': bool(cap.set(prop, original[name])), 'readback': cap.get(prop)}
        cap.release()
        path = folder / 'probe.json'
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
        print(json.dumps({'report': str(path), 'restored': result['restored']}), flush=True)


if __name__ == '__main__':
    main()
