"""Calibrated physical LED/LCD reader. No inferred digits or inferred decimals."""
from pathlib import Path

import cv2
import numpy as np

from seven_segment import PATTERNS, crop

# Sample segment interiors, avoiding joins and the LCD's slanted ends.
CORE = [(0.30,.055,.70,.15),(.78,.22,.94,.39),(.76,.65,.93,.82),
        (.29,.86,.70,.96),(.05,.65,.22,.82),(.07,.22,.24,.39),
        (.29,.45,.70,.55)]
LED_MODES = ('red_led','white_led_core')


def warp_panel(frame, quad, size):
    width, height = size
    target = np.float32([[0,0],[width-1,0],[width-1,height-1],[0,height-1]])
    matrix = cv2.getPerspectiveTransform(np.float32(quad), target)
    return cv2.warpPerspective(frame, matrix, (width,height))


def ink_strength(panel, mode):
    if mode == 'white_led_core':
        return np.maximum(np.min(panel.astype(np.float32),axis=2)-80,0)
    if mode == 'red_led':
        b,g,r = cv2.split(panel.astype(np.float32))
        # Bright LED cores clip to white; color excess alone would lose them.
        red = np.maximum(r-np.maximum(g,b),0)
        bright = np.maximum(np.minimum(np.minimum(r,g),b)-100,0)
        return np.maximum(red,bright)
    green = panel[:,:,1]
    background = cv2.morphologyEx(green,cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(19,19)))
    return np.maximum(background.astype(np.float32)-green.astype(np.float32),0)


def region_score(ink, box):
    tile = crop(ink,box)
    return float(np.percentile(tile,70))


def read_physical_field(panel, field, mode):
    ink = ink_strength(panel,mode)
    glyphs = []
    raw_scores = []
    for box in field['cells']:
        tile = crop(ink,box)
        h,w = tile.shape
        scores = [float(np.percentile(tile[round(y1*h):round(y2*h),
                                             round(x1*w):round(x2*w)],70))
                  for x1,y1,x2,y2 in CORE]
        raw_scores.append(scores)
    peak = float(np.percentile(np.asarray(raw_scores),85))
    led_mode = mode in LED_MODES
    minimum = 18 if led_mode else 13
    if peak < minimum:
        return {'value':None,'valid':False,'reason':'笔画对比度不足','scores':raw_scores}
    # Two separated bands; middle-band evidence is rejected rather than guessed.
    on, off = (peak*.68,peak*.48) if led_mode else (peak*.43,peak*.22)
    for scores in raw_scores:
        pattern = tuple(int(s>=on) for s in scores)
        blank = max(scores)<off
        uncertain = any(off<s<on for s in scores)
        digit = ' ' if blank and field.get('allow_leading_blank') else PATTERNS.get(pattern)
        glyphs.append({'digit':digit,'pattern':pattern,'scores':[round(v,1) for v in scores],
                       'uncertain':uncertain})
    chars = [g['digit'] if g['digit'] is not None and not g['uncertain'] else '?' for g in glyphs]
    # The thrust meter drifts a few grams below zero and then spends its first
    # glyph cell on a minus sign. Only segment G alone is a minus, so this cannot
    # be confused with a digit; read the remaining cells as a negative value.
    negative = (field.get('allow_negative') and len(glyphs)>1
                and glyphs[0]['pattern']==(0,0,0,0,0,0,1)
                and not glyphs[0]['uncertain'])
    if negative:
        chars = chars[1:]
    text = ''.join(chars)
    stripped = text.lstrip()
    valid = bool(stripped) and '?' not in text and ' ' not in stripped
    reason = None if valid else '段码不确定或数字缺失'
    dot_scores = [float(np.percentile(crop(ink,b),field.get('dot_percentile',80)))
                  for b in field.get('dot_boxes',[])]
    dots = [i for i,v in enumerate(dot_scores) if v>=on]
    if any(off<v<on for v in dot_scores):
        valid,reason = False,'小数点不确定'
    if len(dots)>1:
        valid,reason = False,'检测到多个小数点'
    if field.get('require_decimal') and not dots:
        valid,reason = False,'未确认小数点'
    if dots and valid:
        place = dots[0]+1
        if place < len(text)-len(stripped) or place==len(text):
            valid,reason = False,'小数点位置超出当前标定格式'
        else:
            text = text[:place]+'.'+text[place:]
    text = text.lstrip()
    if valid and not field.get('keep_leading_zeros'):
        integer,*fraction = text.split('.')
        text = (integer.lstrip('0') or '0')+('.'+fraction[0] if fraction else '')
    if valid and negative:
        text = '-'+text
    return {'value':text if valid else None,'valid':bool(valid),'reason':reason,
            'digits':glyphs,'dot_scores':[round(v,1) for v in dot_scores],
            'contrast':round(peak,1),'thresholds':[round(off,1),round(on,1)]}


class InstrumentReader:
    def __init__(self,config,root):
        self.config = config
        self.reference = cv2.imread(str(Path(root)/config['reference_image']))
        if self.reference is None:
            raise RuntimeError('找不到实物标定照片，请重新标定。')
        self.anchor_quads = {}
        self.reference_panels = {}
        self.refine_masks = {}
        self.previous_corrections = {}
        for name,panel in config['panels'].items():
            anchor = self.detect_panel(self.reference,panel)
            if anchor is None:
                self.previous_corrections.pop(name,None)
                raise RuntimeError(f'标定照片中找不到 {name} 显示窗边界。')
            self.anchor_quads[name] = anchor
            self.reference_panels[name] = warp_panel(self.reference,panel['quad'],panel['size'])
            width,height = panel['size']
            mask = np.full((height,width),255,np.uint8)
            for field in config['fields']:
                if field['panel']!=name:
                    continue
                boxes = field['cells']+field.get('dot_boxes',[])
                if field['id']=='energy':
                    boxes += field.get('unit_box',[])
                for x,y,w,h in boxes:
                    cv2.rectangle(mask,(max(0,x-3),max(0,y-3)),
                                  (min(width-1,x+w+3),min(height-1,y+h+3)),0,-1)
            self.refine_masks[name] = mask
        self.unit_templates = {}
        for field in config['fields']:
            if field.get('unit_box'):
                panel = config['panels'][field['panel']]
                rectified = warp_panel(self.reference,panel['quad'],panel['size'])
                ink = ink_strength(rectified,panel['mode'])
                self.unit_templates[field['id']] = crop(ink,field['unit_box'][0]).copy()

    @staticmethod
    def detect_panel(frame,panel):
        """Locate each display itself; background motion must not gate readings."""
        quad = np.float32(panel['quad'])
        margin = panel.get('search_margin',180)
        x1,y1 = np.maximum(np.floor(quad.min(axis=0)-margin),0).astype(int)
        x2,y2 = np.minimum(np.ceil(quad.max(axis=0)+margin),
                           [frame.shape[1],frame.shape[0]]).astype(int)
        roi = frame[y1:y2,x1:x2]
        if not roi.size:
            return None
        if panel['mode']=='dark_lcd':
            mask = cv2.inRange(cv2.cvtColor(roi,cv2.COLOR_BGR2HSV),
                               np.array([35,100,40]),np.array([95,255,255]))
        else:
            mask = (cv2.cvtColor(roi,cv2.COLOR_BGR2GRAY)<100).astype(np.uint8)*255
        contours,_ = cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        expected_area = cv2.contourArea(quad)
        expected_center = quad.mean(axis=0)
        candidates = []
        for contour in contours:
            hull = cv2.convexHull(contour)
            area = cv2.contourArea(hull)
            if not expected_area*.65<area<expected_area*1.45:
                continue
            polygon = cv2.approxPolyDP(hull,.025*cv2.arcLength(hull,True),True).reshape(-1,2)
            if len(polygon)!=4:
                continue
            polygon = polygon.astype(np.float32)+[x1,y1]
            # Screens remain approximately upright in this calibrated setup.
            sums = polygon.sum(axis=1)
            differences = polygon[:,0]-polygon[:,1]
            ordered = np.float32([polygon[np.argmin(sums)],polygon[np.argmax(differences)],
                                  polygon[np.argmax(sums)],polygon[np.argmin(differences)]])
            if len(np.unique(ordered,axis=0))!=4 or not cv2.isContourConvex(ordered):
                continue
            width = np.linalg.norm(ordered[1]-ordered[0])
            height = np.linalg.norm(ordered[3]-ordered[0])
            expected_ratio = np.linalg.norm(quad[1]-quad[0])/np.linalg.norm(quad[3]-quad[0])
            if not expected_ratio*.75<width/height<expected_ratio*1.3:
                continue
            distance = float(np.linalg.norm(ordered.mean(axis=0)-expected_center))
            candidates.append((distance+abs(area/expected_area-1)*40,ordered))
        return min(candidates,key=lambda item:item[0])[1] if candidates else None

    def locate(self,frame,names=None):
        panels,quads,status = {},{},{}
        selected = self.config['panels'] if names is None else {
            name:panel for name,panel in self.config['panels'].items() if name in names}
        if list(frame.shape[:2]) != [1080,1920]:
            return panels,quads,{name:{'valid':False,'reason':'相机分辨率与标定不一致'}
                                for name in selected}
        for name,config in selected.items():
            anchor = self.detect_panel(frame,config)
            if anchor is None:
                status[name] = {'valid':False,'reason':'未找到显示窗边界，请检查遮挡或位置'}
                continue
            transform = cv2.getPerspectiveTransform(self.anchor_quads[name],anchor)
            quad = cv2.perspectiveTransform(np.float32(config['quad'])[None],transform)[0]
            panel = warp_panel(frame,quad,config['size'])
            detail = {'valid':True,'method':'display_boundary'}
            if config['mode']=='dark_lcd':
                # Refine only the static border and V/A/W labels; numbers and the
                # auxiliary unit are excluded, so their changes cannot steer alignment.
                try:
                    # Nearby frames usually need only a small refinement. Use
                    # the previous accepted transform as an initial estimate,
                    # but recheck correlation/displacement on every new frame.
                    correction = self.previous_corrections.get(name,np.eye(3,dtype=np.float32)).copy()
                    cc,correction = cv2.findTransformECC(
                        cv2.cvtColor(self.reference_panels[name],cv2.COLOR_BGR2GRAY),
                        cv2.cvtColor(panel,cv2.COLOR_BGR2GRAY),correction,cv2.MOTION_HOMOGRAPHY,
                        (cv2.TERM_CRITERIA_EPS|cv2.TERM_CRITERIA_COUNT,20,1e-4),
                        self.refine_masks[name],5)
                    width,height = config['size']
                    corners = np.float32([[[0,0],[width-1,0],[width-1,height-1],[0,height-1]]])
                    moved = cv2.perspectiveTransform(corners,correction)
                    displacement = float(np.max(np.linalg.norm(moved-corners,axis=2)))
                    if cc<.75 or displacement>24:
                        raise ValueError('屏幕局部校正未通过')
                    self.previous_corrections[name] = correction.copy()
                    panel = cv2.warpPerspective(panel,correction,tuple(config['size']),
                                                flags=cv2.INTER_LINEAR|cv2.WARP_INVERSE_MAP)
                    detail.update(refinement_correlation=round(cc,3),
                                  refinement_shift_pixels=round(displacement,1))
                except (cv2.error,ValueError):
                    self.previous_corrections.pop(name,None)
                    status[name] = {'valid':False,'reason':'液晶屏局部定位不确定，请保持屏幕无遮挡'}
                    continue
            panels[name],quads[name],status[name] = panel,quad.tolist(),detail
        return panels,quads,status

    def read(self,frame):
        panels,quads,status = self.locate(frame)
        alignment = {'valid':all(s['valid'] for s in status.values()),'panels':status}
        readings = []
        for field in self.config['fields']:
            if field['panel'] not in panels:
                readings.append({'id':field['id'],'name':field['name'],'unit':field['unit'],
                                 'value':None,'valid':False,'reason':status[field['panel']]['reason']})
                continue
            panel = panels[field['panel']]
            mode = self.config['panels'][field['panel']]['mode']
            result = read_physical_field(panel,field,mode)
            if field['id'] in self.unit_templates:
                ink = ink_strength(panel,mode)
                tile = crop(ink,field['unit_box'][0])
                template = self.unit_templates[field['id']]
                correlation = float(cv2.matchTemplate(tile,template,cv2.TM_CCOEFF_NORMED)[0,0])
                result['unit_match'] = round(correlation,3)
                if correlation<.60:
                    result.update(value=None,valid=False,reason='单位区域变化或模糊，请核对显示页面')
            readings.append({**result,'id':field['id'],'name':field['name'],'unit':field['unit']})
        return {'alignment':alignment,'fields':readings,'panel_quads':quads},panels

    def read_led(self,frame):
        """Fast LED-only scan used on every captured frame.

        The multiplexed seven-segment display loses a segment on part of the
        frames, so scanning only at the 5 Hz publish rate throws away several
        chances to see every segment lit. This path neither touches the LCD nor
        changes the conservative segment thresholds; it only supplies complete
        readings that can be held for a fraction of the sample interval.
        """
        names={name for name,panel in self.config['panels'].items()
               if panel['mode'] in LED_MODES}
        if not names:
            return []
        panels,quads,status=self.locate(frame,names)
        readings=[]
        for field in self.config['fields']:
            name=field['panel']
            if name not in names:
                continue
            if name not in panels:
                readings.append({'id':field['id'],'name':field['name'],'unit':field['unit'],
                                 'value':None,'valid':False,
                                 'reason':status[name]['reason']})
                continue
            result=read_physical_field(panels[name],field,self.config['panels'][name]['mode'])
            readings.append({**result,'id':field['id'],'name':field['name'],'unit':field['unit']})
        return readings


def hold_recent_led_fields(fields,cache,now,hold_seconds):
    """Replace an unreadable LED field with a complete recent-frame reading.

    A held value is a real decoded frame, never a guessed or merged digit.
    Returns the field ids that were filled from the cache.
    """
    held=[]
    for field in fields:
        cached=cache.get(field.get('id'))
        if field.get('valid') or not cached:
            continue
        seq,captured,result=cached
        age=now-captured
        if not 0<=age<=hold_seconds:
            continue
        replacement=dict(result)
        replacement['held_from_frame_seq']=seq
        replacement['held_age_seconds']=round(age,3)
        field.clear()
        field.update(replacement)
        held.append(field['id'])
    return held
