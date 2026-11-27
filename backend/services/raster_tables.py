"""Local ruled-table recovery for image tables with reliable closed borders."""
from functools import lru_cache


@lru_cache(maxsize=1)
def _reader():
    from rapidocr import RapidOCR, OCRVersion, ModelType
    return RapidOCR(params={'Det.ocr_version': OCRVersion.PPOCRV6,
                            'Rec.ocr_version': OCRVersion.PPOCRV6,
                            'Det.model_type': ModelType.SMALL,
                            'Rec.model_type': ModelType.SMALL,
                            'EngineConfig.onnxruntime.intra_op_num_threads': 1})


def ruled_cells(image):
    """Find closed rectangles; no blank-column guessing or instrument rules."""
    import cv2
    import numpy as np
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 8)
    horizontal = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((1, max(40, image.shape[1] // 28)), np.uint8))
    vertical = cv2.morphologyEx(bw, cv2.MORPH_OPEN, np.ones((max(20, image.shape[0] // 50), 1), np.uint8))
    mask = cv2.dilate(horizontal | vertical, np.ones((3, 3), np.uint8))
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    if hierarchy is None:
        return []
    for contour, tree in zip(contours, hierarchy[0]):
        x, y, w, h = cv2.boundingRect(contour)
        if tree[3] >= 0 and w > 35 and h > 20 and cv2.contourArea(contour) / (w*h) > .85:
            boxes.append((x, y, x+w, y+h))
    area = sum((r-l)*(b-t) for l,t,r,b in boxes)
    if not 12 <= len(boxes) <= 250 or area / (image.shape[0]*image.shape[1]) < .75:
        return []
    def cluster(values):
        groups = []
        for value in sorted(values):
            if groups and value - groups[-1][-1] <= 6:
                groups[-1].append(value)
            else:
                groups.append([value])
        return [sum(g)/len(g) for g in groups]
    xs = cluster([x for box in boxes for x in (box[0], box[2])])
    ys = cluster([y for box in boxes for y in (box[1], box[3])])
    result, occupied = [], set()
    nearest = lambda values, value: min(range(len(values)), key=lambda i: abs(values[i]-value))
    for l,t,r,b in boxes:
        c0,c1,r0,r1 = nearest(xs,l),nearest(xs,r),nearest(ys,t),nearest(ys,b)
        slots = {(y,x) for y in range(r0,r1) for x in range(c0,c1)}
        if not slots or occupied & slots:
            return []
        occupied |= slots
        result.append({'row_start': r0, 'row_end': r1, 'col_start': c0, 'col_end': c1, 'pixel_box': (l,t,r,b)})
    if len(occupied) != (len(xs)-1)*(len(ys)-1):
        return []
    return sorted(result, key=lambda c: (c['row_start'],c['col_start']))


def recover_table(page, region):
    import fitz
    import numpy as np
    b = region['bbox']
    rect = fitz.Rect(b['l'], page.rect.height-b['t'], b['r'], page.rect.height-b['b']) if b.get('coord_origin') == 'BOTTOMLEFT' else fitz.Rect(b['l'],b['t'],b['r'],b['b'])
    # Native ruled-table extraction is preferred when sufficient text exists.
    if len(page.get_text(clip=rect).strip()) > 300:
        return None
    clip = (rect + (-5,-5,5,5)) & page.rect
    pix = page.get_pixmap(matrix=fitz.Matrix(4,4), clip=clip, alpha=False)
    image = np.frombuffer(pix.samples,dtype=np.uint8).reshape(pix.height,pix.width,pix.n)[:,:,:3]
    cells = ruled_cells(image)
    if not cells:
        return None
    for cell in cells:
        l,t,r,b = cell.pop('pixel_box')
        cell['bbox'] = {'l': (pix.x+l)/4, 't': (pix.y+t)/4, 'r': (pix.x+r)/4, 'b': (pix.y+b)/4, 'coord_origin': 'TOPLEFT'}
        bounds = fitz.Rect(cell['bbox']['l'],cell['bbox']['t'],cell['bbox']['r'],cell['bbox']['b'])
        existing = []
        for old in region.get('cells', []):
            box = old.get('bbox')
            if not box:
                continue
            old_rect = fitz.Rect(box['l'],page.rect.height-box['t'],box['r'],page.rect.height-box['b']) if box.get('coord_origin')=='BOTTOMLEFT' else fitz.Rect(box['l'],box['t'],box['r'],box['b'])
            if ((bounds + (-1,-1,1,1)) & old_rect).get_area()/max(old_rect.get_area(),1) > .97:
                existing.append((old_rect.y0,old_rect.x0,old['text']))
        if existing:
            cell['text'] = ' '.join(text for _,_,text in sorted(existing))
            cell['text_source'] = 'existing_ocr_inside_ruled_cell'
        else:
            crop = image[t+2:b-2,l+2:r-2].copy()
            output = _reader()(crop)
            cell['text'] = ' '.join(output.txts or ())
            cell['text_source'] = 'local_cell_ocr'
    # Reject incomplete recovery; preserve the original OCR for visual review.
    if sum(bool(c['text']) for c in cells) < .85 * len(cells):
        return None
    height, width = max(c['row_end'] for c in cells),max(c['col_end'] for c in cells)
    from backend.services.evidence_ownership import expand_cells
    rows = expand_cells([['']*width for _ in range(height)], cells)
    return {'text': '\n'.join('| ' + ' | '.join(row) + ' |' for row in rows), 'cells': cells, 'parser': 'raster_ruled_table'}
