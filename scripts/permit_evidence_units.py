"""Reversible punctuation-sized reading units, not a semantic sentence parser."""
import re


def make_units(item):
    text, base = item['text'], item['char_start']
    if not text:
        raise ValueError('빈 원문은 근거 단위로 분할할 수 없습니다.')
    boundaries = {0, len(text)}
    for match in re.finditer(r'[,，;；。!?！？]|(?<=[다함됨음])\.(?=\s|$)', text):
        start, end = match.span()
        # Numeric grouping separators must stay attached to the number.
        if match.group() in (',', '，') and start and end < len(text) and text[start-1].isdigit() and text[end].isdigit():
            continue
        boundaries.add(end)
    lines, offset = [], 0
    for number, value in enumerate(text.splitlines(keepends=True), 1):
        end = offset + len(value)
        lines.append({'line_id':f'L{number:03}', 'start':offset, 'end':end})
        # Keep blank rows and isolated fraction-shaped layout rows distinct.
        # A fraction is not automatically classified as a page number.
        if not value.strip() or re.fullmatch(r'\d+\s*/\s*\d+', value.strip()):
            boundaries.update((offset, end))
        offset = end
    if offset != len(text):
        raise ValueError('원문 문자 대응 오류')
    points = sorted(boundaries)
    units = []
    for number, (start, end) in enumerate(zip(points, points[1:]), 1):
        fragments = []
        for line in lines:
            left, right = max(start,line['start']), min(end,line['end'])
            if left < right:
                fragments.append({'line_id':line['line_id'], 'char_start':base+left,
                                  'char_end':base+right, 'text':text[left:right]})
        units.append({'line_id':f'U{number:03}', 'char_start':base+start, 'char_end':base+end,
                      'text':text[start:end], 'source_fragments':fragments})
    if ''.join(u['text'] for u in units) != text:
        raise ValueError('근거 단위에서 원문 누락')
    return units
