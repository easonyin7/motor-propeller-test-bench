"""Conservative red seven-segment reader using calibrated fixed glyph cells."""
import cv2
import numpy as np

PATTERNS = {
    (1,1,1,1,1,1,0):'0', (0,1,1,0,0,0,0):'1', (1,1,0,1,1,0,1):'2',
    (1,1,1,1,0,0,1):'3', (0,1,1,0,0,1,1):'4', (1,0,1,1,0,1,1):'5',
    (1,0,1,1,1,1,1):'6', (1,1,1,0,0,0,0):'7', (1,1,1,1,1,1,1):'8',
    (1,1,1,1,0,1,1):'9',
}
# a,b,c,d,e,f,g; avoid the segment intersections.
REGIONS = [(0.25,.025,.75,.115),(.80,.15,.98,.40),(.80,.63,.98,.85),
           (.25,.89,.75,.99),(.015,.63,.20,.85),(.015,.15,.20,.40),
           (.25,.455,.75,.555)]


def crop(frame, box):
    x,y,w,h = [int(round(v)) for v in box]
    if min(w,h) <= 0 or x<0 or y<0 or x+w>frame.shape[1] or y+h>frame.shape[0]:
        raise ValueError('数字校准框超出画面，请重新选择区域。')
    return frame[y:y+h,x:x+w]


def red_strength(frame):
    b,g,r = cv2.split(frame.astype(np.float32))
    return np.maximum(r-np.maximum(g,b),0)


def read_field(frame, field):
    values = []
    reports = []
    roi = crop(frame,field['roi'])
    strength = red_strength(roi)
    # Adaptive within a field; retain a minimum contrast requirement.
    threshold = max(8.,float(np.percentile(strength,96))*.40)
    if np.percentile(strength,96)<12:
        return {'value':None,'valid':False,'reason':'红色数字对比度不足','digits':[]}
    for box in field['digit_boxes']:
        tile = crop(frame,box)
        mask = red_strength(tile)>threshold
        h,w = mask.shape
        occupancies = []
        for x1,y1,x2,y2 in REGIONS:
            region = mask[round(y1*h):round(y2*h),round(x1*w):round(x2*w)]
            occupancies.append(float(np.mean(region)))
        pattern = tuple(int(v>.28) for v in occupancies)
        ambiguous = any(.18<v<.36 for v in occupancies)
        digit = PATTERNS.get(pattern)
        valid = digit is not None and not ambiguous
        reports.append({'digit':digit,'valid':valid,'pattern':pattern,
                        'occupancies':[round(v,3) for v in occupancies]})
        values.append(digit if valid else '?')
    dot = red_strength(crop(frame,field['decimal_box']))>threshold
    dot_ratio = float(np.mean(dot))
    dot_present = dot_ratio>.15
    valid = '?' not in values and dot_present
    text = ''.join(values[:-1])+'.'+values[-1] if valid else None
    return {'value':text,'valid':valid,'reason':None if valid else '段码不确定或小数点未确认',
            'digits':reports,'decimal_ratio':round(dot_ratio,3),'red_threshold':round(threshold,2)}


def read_fixed_display(frame, field):
    """Read the supplied HTML's 3 integer slots plus one decimal slot."""
    offset = [0,0]
    if 'dot_anchor' in field:
        ax,ay = field['dot_anchor']
        sx,sy = ax-45,ay-45
        search = red_strength(crop(frame,[sx,sy,90,90]))
        mask = (search>max(12,float(np.percentile(search,99))*.68)).astype(np.uint8)*255
        contours,_ = cv2.findContours(mask,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        candidates = []
        for contour in contours:
            x,y,w,h = cv2.boundingRect(contour)
            cx,cy = sx+x+w/2,sy+y+h/2
            if 5<=w<=32 and 5<=h<=32 and .6<=w/h<=1.8 and cy>ay-20:
                candidates.append((cx,cy))
        if not candidates:
            return {'value':None,'valid':False,'reason':'小数点未定位；请检查数字区域','digits':[]}
        cx,cy = min(candidates,key=lambda p:(p[0]-ax)**2+(p[1]-ay)**2)
        offset = [round(cx-ax),round(cy-ay)]
        dx,dy = offset
        field = {**field,'reference_cells':[[x+dx,y+dy,w,h] for x,y,w,h in field['reference_cells']],
                 'reference_decimal':[field['reference_decimal'][0]+dx,field['reference_decimal'][1]+dy,*field['reference_decimal'][2:]]}
    strength = red_strength(crop(frame,field['roi']))
    peak = float(np.percentile(strength,99))
    if peak<12:
        return {'value':None,'valid':False,'reason':'红色数字对比度不足','digits':[]}
    threshold = max(8.,peak*.4)
    reports = []
    characters = []
    for index,box in enumerate(field['reference_cells']):
        mask = red_strength(crop(frame,box))>threshold
        h,w = mask.shape
        ratios = [float(np.mean(mask[round(y1*h):round(y2*h),round(x1*w):round(x2*w)]))
                  for x1,y1,x2,y2 in REGIONS]
        pattern = tuple(int(v>.28) for v in ratios)
        blank = max(ratios)<.12
        uncertain = any(.18<v<.36 for v in ratios)
        digit = ' ' if blank and index<2 else PATTERNS.get(pattern)
        valid = digit is not None and not uncertain
        characters.append(digit if valid else '?')
        reports.append({'digit':digit,'valid':valid,'blank':blank,'pattern':pattern,
                        'occupancies':[round(v,3) for v in ratios]})
    decimal = float(np.mean(red_strength(crop(frame,field['reference_decimal']))>threshold))
    integer = ''.join(characters[:3]).lstrip()
    valid = '?' not in characters and integer and ' ' not in integer and decimal>.15
    text = integer+'.'+characters[-1] if valid else None
    if text is not None and not 0<=float(text)<=100:
        text = None
        valid = False
    return {'value':text,'valid':bool(valid),'reason':None if valid else '空白位、段码或小数点不确定',
            'digits':reports,'decimal_ratio':round(decimal,3),'red_threshold':round(threshold,2),
            'located_boxes':field['reference_cells'],'decimal_box':field['reference_decimal'],
            'method':'fixed_four_slots','alignment_offset':offset}


def read_auto_field(frame, field):
    """Find 2–4 digit glyphs and one decimal point; reject ambiguous layouts."""
    if field.get('fixed_slots'):
        return read_fixed_display(frame,field)
    x,y,w,h = field['roi']
    roi = crop(frame,field['roi'])
    strength = red_strength(roi)
    peak = float(np.percentile(strength,99))
    if peak<14:
        return {'value':None,'valid':False,'reason':'红色数字对比度不足','digits':[]}
    successes = []
    diagnostics = []
    for factor in (.60,.66,.72):
        mask = (strength>max(10.,peak*factor)).astype(np.uint8)*255
        joined = cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_RECT,(1,19)))
        contours,_ = cv2.findContours(joined,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        boxes = sorted([(bx,by+9,bw,max(1,bh-18)) for bx,by,bw,bh in
                        (cv2.boundingRect(c) for c in contours)],key=lambda b:b[0])
        glyphs = [b for b in boxes if b[3]>h*.53 and b[2]>3]
        if not 2<=len(glyphs)<=4:
            continue
        tallest = max(b[3] for b in glyphs)
        full = [b for b in glyphs if b[3]>=tallest*.86]
        digit_h = float(np.median([b[3] for b in full]))
        line_top = round(float(np.median([b[1] for b in full])))
        if any(abs(b[3]-digit_h)>digit_h*.28 for b in glyphs):
            continue
        cells = []
        for bx,by,bw,bh in glyphs:
            if bw>bh*.80:
                cells = []
                break
            if bw<bh*.28:
                # A '1' lights only the right bars. Reconstruct the full glyph cell.
                inferred = round(bh*.55)
                bx = bx+bw-inferred
                bw = inferred
            by,bh = line_top,round(digit_h)
            cells.append([x+bx,y+by,bw,bh])
        if not cells:
            continue
        if any(cells[i+1][0]-cells[i][0]>digit_h*1.1 for i in range(len(cells)-1)):
            # A missing middle glyph must never shorten a number silently.
            continue
        dots = [b for b in boxes if 5<b[3]<digit_h*.23 and 4<b[2]<digit_h*.25
                and b[1]>min(q[1] for q in glyphs)+digit_h*.70
                and glyphs[-2][0]+glyphs[-2][2]-3<b[0]<glyphs[-1][0]]
        if len(dots)!=1:
            continue
        dot = dots[0]
        detected = {**field,'digit_boxes':cells,'decimal_box':[x+dot[0],y+dot[1],dot[2],dot[3]]}
        result = read_field(frame,detected)
        result['located_boxes'] = cells
        result['decimal_box'] = detected['decimal_box']
        result['segmentation_factor'] = factor
        diagnostics.append(result)
        if result['valid']:
            numeric = float(result['value'])
            if 0<=numeric<=100:
                successes.append(result)
    if successes and len(set(r['value'] for r in successes))==1:
        return successes[0]
    if 'reference_cells' in field:
        calibrated = {**field,'digit_boxes':field['reference_cells'],
                      'decimal_box':field['reference_decimal']}
        result = read_field(frame,calibrated)
        if result['valid'] and 0<=float(result['value'])<=100:
            allowed = np.zeros((h,w),dtype=np.uint8)
            for bx,by,bw,bh in field['reference_cells']+[field['reference_decimal']]:
                cv2.rectangle(allowed,(max(0,bx-x-5),max(0,by-y-5)),
                              (min(w-1,bx+bw-x+5),min(h-1,by+bh-y+5)),255,-1)
            extra = ((strength>peak*.6)&(allowed==0)).astype(np.uint8)*255
            cs,_ = cv2.findContours(extra,cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
            if not any(cv2.boundingRect(c)[3]>h*.4 and cv2.contourArea(c)>h*.5 for c in cs):
                return {**result,'located_boxes':field['reference_cells'],
                        'decimal_box':field['reference_decimal'],'method':'calibrated_cells'}
    if diagnostics:
        return {**diagnostics[-1],'value':None,'valid':False,'reason':'分割结果不一致或超出0.0～100.0范围'}
    return {'value':None,'valid':False,'reason':'未能分开数字和小数点','digits':[]}


class Stability:
    """Publish only repeated readings; invalidate promptly when evidence disappears."""
    def __init__(self,seconds=.6):
        self.seconds = seconds
        self.candidate = None
        self.since = 0.
        self.count = 0
        self.value = None

    def update(self,result,now):
        candidate = result['value'] if result['valid'] else None
        if candidate is None or candidate != self.candidate:
            self.candidate = candidate
            self.since = now
            self.count = 1 if candidate is not None else 0
            self.value = None
        else:
            self.count += 1
            if self.count>=3 and now-self.since>=self.seconds:
                self.value = candidate
        return self.value
