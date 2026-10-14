"""Map observed standalone fractions to source addresses, never to verdicts."""
import re


def layout_observations(item, candidates):
    """Reuse packet candidates only after exact quote/owner/offset validation.

    A standalone fraction can be a measurement, so this is an unreviewed layout
    clue. It neither deletes body text nor asserts that a page label is present.
    """
    units = item['lines']
    if not units:
        raise ValueError('원문 단위가 없습니다.')
    start = units[0]['char_start']
    text = ''.join(unit['text'] for unit in units)
    cursor = start
    for unit in units:
        if unit['char_start'] != cursor or unit['char_end'] != cursor + len(unit['text']):
            raise ValueError('원문 단위 위치 불일치')
        cursor = unit['char_end']
    observations, seen = [], set()
    for candidate in candidates:
        if item['source_span_id'] not in candidate['source_span_ids']:
            continue
        if any(type(candidate[key]) is not int for key in ('page', 'number', 'declared_total')):
            raise ValueError('번호 후보의 관찰값은 정수여야 합니다.')
        if candidate['page'] != item['page_number'] or item['source_node_id'] not in candidate['source_node_ids']:
            raise ValueError('번호 후보의 원문 소유 위치 불일치')
        left, right = candidate['char_start'], candidate['char_end']
        if type(left) is not int or type(right) is not int or not start <= left < right <= cursor:
            raise ValueError('번호 후보가 원문 구간 밖에 있습니다.')
        if text[left-start:right-start] != candidate['quote']:
            raise ValueError('번호 후보의 원문 인용 불일치')
        match = re.fullmatch(r'\s*(\d+)[ \t]*/[ \t]*(\d+)\s*', candidate['quote'])
        if not match or int(match[1]) != candidate['number'] or int(match[2]) != candidate['declared_total']:
            raise ValueError('번호 후보의 관찰값 불일치')
        if (left, right) in seen:
            raise ValueError('중복 번호 후보')
        seen.add((left, right))
        ids = [unit['line_id'] for unit in units if unit['text'].strip()
               and unit['char_start'] < right and unit['char_end'] > left]
        if not ids:
            raise ValueError('번호 후보에 비어 있지 않은 원문 근거가 없습니다.')
        observations.append({'kind':'possible_page_label', 'review_status':'unreviewed',
            'page_number':item['page_number'], 'char_start':left, 'char_end':right,
            'quote':candidate['quote'], 'evidence_line_ids':ids,
            'observed_numerator':candidate['number'], 'observed_denominator':candidate['declared_total']})
    return observations
