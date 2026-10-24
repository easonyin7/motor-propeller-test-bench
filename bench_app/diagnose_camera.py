"""Camera-only evidence capture. Never imports or opens motor/serial control."""
import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cv_mvp'))
import cv2
from cv_camera_test import Capture, cameras
from instrument_reader import InstrumentReader


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'cv_mvp/instrument_config.json')
    parser.add_argument('--seconds', type=float, default=8)
    args = parser.parse_args()
    cv2.setNumThreads(1)
    config = json.loads(args.config.read_text(encoding='utf-8'))
    matches = [c for c in cameras() if c.name == config['camera']]
    if len(matches) != 1:
        raise RuntimeError('Camera not uniquely found')
    reader = InstrumentReader(config, ROOT/'cv_mvp')
    folder = ROOT/'bench_app/diagnostics'/datetime.now().strftime('%Y%m%d_%H%M%S')
    folder.mkdir(parents=True)
    capture = Capture(matches[0], config['width'], config['height'])
    started = time.perf_counter()
    seq = -1
    records = []
    try:
        while time.perf_counter()-started < args.seconds:
            item = capture.get()
            if item is None or item[0] == seq:
                time.sleep(.01)
                continue
            seq, captured, frame = item
            result, panels = reader.read(frame)
            index = len(records)
            result.update(frame_seq=seq, captured=captured, elapsed=captured-started)
            records.append(result)
            # Lossless source frames allow offline correction without reacquisition.
            cv2.imwrite(str(folder/f'frame_{index:03d}.png'), frame)
            if 'led' in panels:
                cv2.imwrite(str(folder/f'led_{index:03d}.png'), panels['led'])
        (folder/'samples.json').write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(dict(folder=str(folder), samples=len(records), camera=capture.format_state,
                              thrust_valid=sum(r['fields'][0]['valid'] for r in records)), ensure_ascii=False))
    finally:
        capture.close()


if __name__ == '__main__':
    main()
