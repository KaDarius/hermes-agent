"""Offline event arithmetic; never invokes a model, network, or live gateway."""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation, localcontext
import json
import re
from pathlib import Path


OUTCOME_TARGETS = {'simple': Decimal(60), 'bounded_fleet': Decimal(180)}


def _time(value):
    if not isinstance(value, str):
        raise ValueError('Timestamps must be decimal strings, not binary floats')
    if not re.fullmatch(r'[0-9]{1,20}(?:\.[0-9]{1,80})?', value):
        raise ValueError('Timestamp requires 1-20 integer digits and at most 80 fractional digits')
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError('Invalid timestamp') from exc
    if not result.is_finite():
        raise ValueError('Timestamp must be finite')
    return result


def _metric(value, target):
    return {'seconds': str(value) if value is not None else None,
            'target_seconds': str(target) if target is not None else None,
            'status': 'UNKNOWN' if value is None or target is None else
                      ('PASS' if value <= target else 'FAIL')}


def evaluate(data, request_id):
    # Accepted timestamps have at most 100 digits; subtraction remains exact.
    with localcontext() as context:
        context.prec = 128
        return _evaluate(data, request_id)


def _evaluate(data, request_id):
    request = data['requests'][request_id]
    complete = request.get('visibility_complete') is True
    receipt_known = complete or request.get('first_visible_verified') is True
    outcome_known = complete or request.get('outcome_verified') is True
    events = [dict(e, time=_time(e['at'])) for e in data['events']
              if e['request_id'] == request_id]
    ingress = [e for e in events if e['kind'] == 'ingress' and e['source'] == 'slack']
    if len(ingress) != 1:
        raise ValueError('Exactly one Slack ingress is required per request')
    start = ingress[0]['time']
    if any(e['time'] < start for e in events):
        raise ValueError('Event precedes request ingress')
    visible = sorted((e for e in events if e['source'] == 'slack'
                      and e.get('delivered') is True
                      and e['kind'] in {'progress', 'final', 'failure'}), key=lambda e: e['time'])
    outcomes = [e for e in visible if e['kind'] in {'final', 'failure'}]
    end = outcomes[0]['time'] if outcomes else None
    # Without a terminal observation the last silent interval is unknown.
    progress_gap = None
    if end is not None:
        points = [start] + [e['time'] for e in visible if e['time'] <= end]
        progress_gap = max(b - a for a, b in zip(points, points[1:]))
    tools = [e['time'] for e in events if e['kind'] == 'tool_record' and e['source'] == 'database']
    calls = []
    telemetry = [e for e in events if e['source'] == 'provider_telemetry'
                 and e['kind'] in {'provider_start', 'provider_end'}]
    for call_id in sorted({e.get('call_id') for e in telemetry if isinstance(e.get('call_id'), str) and e['call_id'].strip()}):
        starts = [e for e in telemetry if e.get('call_id') == call_id and e['kind'] == 'provider_start']
        ends = [e for e in telemetry if e.get('call_id') == call_id and e['kind'] == 'provider_end']
        if len(starts) != 1 or len(ends) != 1:
            continue
        a, b = starts[0], ends[0]
        if any(not isinstance(a.get(k), str) or not a[k].strip() or a[k] != b.get(k) for k in ('model', 'provider')):
            continue
        if b['time'] < a['time']:
            raise ValueError('Provider call ends before it starts')
        calls.append({'call_id': call_id, 'model': a['model'], 'provider': a['provider'],
                      'seconds': str(b['time'] - a['time'])})
    return {
        'request_id': request_id, 'request_class': request['class'],
        'receipt': _metric(visible[0]['time'] - start if visible else None, Decimal(5) if receipt_known else None),
        'outcome': _metric(end - start if end is not None else None, OUTCOME_TARGETS.get(request['class']) if outcome_known else None),
        'outcome_kind': outcomes[0]['kind'] if outcomes else None,
        'single_outcome': 'FAIL' if len(outcomes) > 1 else ('PASS' if outcomes and complete else 'UNKNOWN'),
        'progress': _metric(progress_gap, Decimal(60) if complete else None),
        'first_tool_record_seconds': str(min(tools) - start) if tools else None,
        'provider_calls': calls,
        'provider_attribution': 'OBSERVED_PAIRS_ONLY' if calls else 'UNKNOWN',
        'semantic_quality': 'REQUIRES_REVIEW', 'live_verified': False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('fixture', type=Path)
    args = parser.parse_args()
    data = json.loads(args.fixture.read_text())
    print(json.dumps([evaluate(data, key) for key in data['requests']], indent=2))


if __name__ == '__main__':
    main()
