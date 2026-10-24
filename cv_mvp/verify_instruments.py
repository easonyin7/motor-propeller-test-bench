"""Labeled real-photo replay plus location, decimal, and unit failure checks."""
import json
from pathlib import Path

import cv2
import numpy as np

from instrument_reader import InstrumentReader,read_physical_field,warp_panel

ROOT = Path(__file__).resolve().parent


def main():
    cv2.setNumThreads(1)
    config = json.loads((ROOT/'instrument_config.json').read_text(encoding='utf-8'))
    reader = InstrumentReader(config,ROOT)
    frame = reader.reference.copy()
    expected = {'upper':'0001','voltage':'24.8','current':'0.00','power':'0.00','energy':'35'}
    cases = []
    for name,angle,dx,dy,gain in [('reference',0,0,0,1),('translation',0,9,-7,1),
                                 ('rotation',.7,0,0,1),('dimmer',0,0,0,.8),
                                 ('brighter',0,0,0,1.1)]:
        transform = cv2.getRotationMatrix2D((960,540),angle,1)
        transform[:,2] += [dx,dy]
        sample = cv2.warpAffine(frame,transform,(1920,1080))
        sample = np.clip(sample.astype(np.float32)*gain,0,255).astype(np.uint8)
        result,_ = reader.read(sample)
        values = {f['id']:f['value'] for f in result['fields']}
        assert values==expected,(name,values)
        cases.append({'case':name,'values':values,'passed':True})

    field = next(f for f in config['fields'] if f['id']=='voltage')
    panel_config = config['panels']['lcd']
    panel = warp_panel(frame,panel_config['quad'],panel_config['size'])
    mask = np.zeros(panel.shape[:2],np.uint8)
    x,y,w,h = field['dot_boxes'][1]
    mask[y:y+h,x:x+w] = 255
    no_decimal = cv2.inpaint(panel,mask,5,cv2.INPAINT_TELEA)
    result = read_physical_field(no_decimal,field,panel_config['mode'])
    assert not result['valid'],result
    cases.append({'case':'voltage_decimal_erased','rejected':True})

    blank,_ = reader.read(np.zeros_like(frame))
    assert not blank['alignment']['valid']
    assert all(not f['valid'] and f['value'] is None for f in blank['fields'])
    cases.append({'case':'blank_camera_frame','rejected':True})

    # Cover the unit label in the full real photograph, leaving its digits intact.
    energy = next(f for f in config['fields'] if f['id']=='energy')
    x,y,w,h = energy['unit_box'][0]
    width,height = panel_config['size']
    matrix = cv2.getPerspectiveTransform(np.float32([[0,0],[width-1,0],
        [width-1,height-1],[0,height-1]]),np.float32(panel_config['quad']))
    points = cv2.perspectiveTransform(np.float32([[[x,y],[x+w,y],[x+w,y+h],[x,y+h]]]),matrix)[0]
    covered = frame.copy()
    cv2.fillConvexPoly(covered,points.astype(np.int32),(55,152,35))
    result,_ = reader.read(covered)
    values = {f['id']:f for f in result['fields']}
    assert not values['energy']['valid'],values['energy']
    assert values['voltage']['value']=='24.8',values['voltage']
    cases.append({'case':'energy_unit_covered','energy_rejected':True,'voltage_unchanged':True})

    # Regression for the user's actual moved-instrument photograph: the tabletop
    # remained visible but moved independently, which defeated global alignment.
    moved_path = ROOT/'calibration/instrument_20261017_165509/frame.png'
    moved_expected = {**expected,'upper':'0000'}
    moved,_ = reader.read(cv2.imread(str(moved_path)))
    values = {f['id']:f['value'] for f in moved['fields']}
    assert values==moved_expected,values
    cases.append({'case':'real_instrument_moved','values':values,'passed':True})

    # Move each real screen patch independently while leaving other scene pixels
    # fixed. These are controlled replay cases, not new optical measurements.
    for name,angle,scale,dx,dy in [('led',-2,1.03,-90,30),('lcd',2,.98,90,45)]:
        panel_config = config['panels'][name]
        quad = np.float32(panel_config['quad'])
        x1,y1 = np.floor(quad.min(axis=0)-10).astype(int)
        x2,y2 = np.ceil(quad.max(axis=0)+10).astype(int)
        patch_mask = np.zeros(frame.shape[:2],np.uint8)
        patch_mask[y1:y2,x1:x2] = 255
        matrix = cv2.getRotationMatrix2D(tuple(quad.mean(axis=0).astype(float)),angle,scale)
        matrix[:,2] += [dx,dy]
        transformed = cv2.warpAffine(frame,matrix,(1920,1080))
        transformed_mask = cv2.warpAffine(patch_mask,matrix,(1920,1080))
        sample = frame.copy()
        sample[patch_mask>0] = (140,140,140)
        sample[transformed_mask>250] = transformed[transformed_mask>250]
        result,_ = reader.read(sample)
        values = {f['id']:f['value'] for f in result['fields']}
        assert values==expected,(name,values)
        cases.append({'case':name+'_independently_moved','values':values,'passed':True})
    output = ROOT/'calibration/verification_instrument_replay.json'
    output.write_text(json.dumps({'reference':config['reference_image'],'cases':cases},
                                indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'passed':len(cases),'report':str(output)}))


if __name__=='__main__':
    main()
