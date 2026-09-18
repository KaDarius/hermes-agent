from copy import deepcopy
from decimal import Decimal

import pytest

from evals.slack_quality.evaluate import evaluate


def event(kind, at, source='slack', **extra):
    return {'request_id': 'one', 'kind': kind, 'at': at, 'source': source, **extra}


def case(events, request_class='simple'):
    return {'requests': {'one': {'class': request_class, 'visibility_complete': True}}, 'events': events}


def test_exact_decimal_latency_and_boundary_pass():
    report = evaluate(case([event('ingress', '100.000001'),
                            event('progress', '105.000001', delivered=True),
                            event('final', '160.000001', delivered=True)]), 'one')
    assert report['receipt']['seconds'] == '5.000000'
    assert report['receipt']['status'] == 'PASS'
    assert report['outcome']['status'] == 'PASS'


def test_missing_delivery_is_unknown_even_with_database_final():
    report = evaluate(case([event('ingress', '100'),
                            event('final', '101', source='database', delivered=True)]), 'one')
    assert report['outcome']['status'] == 'UNKNOWN'
    assert report['receipt']['status'] == 'UNKNOWN'


def test_followup_cannot_supply_original_final():
    other = event('final', '102', delivered=True, request_id='two')
    report = evaluate(case([event('ingress', '100'), other]), 'one')
    assert report['outcome']['status'] == 'UNKNOWN'


def test_failed_send_does_not_count_as_receipt():
    report = evaluate(case([event('ingress', '100'), event('progress', '101', delivered=False)]), 'one')
    assert report['receipt']['status'] == 'UNKNOWN'


def test_expanded_baseline_is_not_simple_request_sla():
    report = evaluate(case([event('ingress', '100'), event('failure', '994', delivered=True)], 'expanded'), 'one')
    assert report['outcome']['status'] == 'UNKNOWN'
    assert report['outcome']['seconds'] == '894'
    assert report['outcome_kind'] == 'failure'
    assert report['receipt']['status'] == 'FAIL'


def test_duplicate_final_deliveries_fail_uniqueness():
    report = evaluate(case([event('ingress', '100'), event('final', '101', delivered=True),
                            event('final', '102', delivered=True)]), 'one')
    assert report['single_outcome'] == 'FAIL'


def test_session_model_does_not_attribute_provider_latency():
    data = case([event('ingress', '100'), event('tool_record', '446', source='database')])
    data['requests']['one']['session_model'] = 'historical-model'
    report = evaluate(data, 'one')
    assert report['provider_calls'] == []
    assert report['provider_attribution'] == 'UNKNOWN'
    assert Decimal(report['first_tool_record_seconds']) == 346


def test_provider_pair_requires_matching_call_identity_and_model():
    data = case([event('ingress', '100'),
                 event('provider_start', '101', source='provider_telemetry', call_id='a', provider='p', model='m'),
                 event('provider_end', '104', source='provider_telemetry', call_id='b', provider='p', model='m')])
    assert evaluate(data, 'one')['provider_attribution'] == 'UNKNOWN'
    data['events'][2]['call_id'] = 'a'
    report = evaluate(data, 'one')
    assert report['provider_calls'][0]['seconds'] == '3'
    assert report['provider_attribution'] == 'OBSERVED_PAIRS_ONLY'
    data['events'][2]['model'] = 'other'
    assert evaluate(data, 'one')['provider_calls'] == []


@pytest.mark.parametrize('at', ['NaN', 'Infinity', '-Infinity', '99'])
def test_invalid_or_pre_ingress_timestamps_rejected(at):
    with pytest.raises(ValueError):
        evaluate(case([event('ingress', '100'), event('progress', at, delivered=True)]), 'one')


def test_missing_ingress_rejected():
    with pytest.raises(ValueError):
        evaluate(case([]), 'one')


def test_progress_gap_includes_time_to_final():
    report = evaluate(case([event('ingress', '100'), event('progress', '101', delivered=True),
                            event('final', '162', delivered=True)]), 'one')
    assert report['progress']['status'] == 'FAIL'
    assert report['progress']['seconds'] == '61'


def test_does_not_mutate_fixture_or_claim_semantic_verification():
    data = case([event('ingress', '100')]); original = deepcopy(data)
    report = evaluate(data, 'one')
    assert data == original
    assert report['semantic_quality'] == 'REQUIRES_REVIEW'
    assert report['live_verified'] is False


def test_selected_events_cannot_prove_progress_gaps_or_unique_outcome():
    data = case([event('ingress', '100'), event('progress', '105', delivered=True),
                 event('final', '200', delivered=True)])
    data['requests']['one']['visibility_complete'] = False
    report = evaluate(data, 'one')
    assert report['progress']['status'] == 'UNKNOWN'
    assert report['single_outcome'] == 'UNKNOWN'
    assert report['receipt']['status'] == 'UNKNOWN'
    assert report['outcome']['status'] == 'UNKNOWN'


@pytest.mark.parametrize('call_id', ['', '   '])
def test_empty_call_identity_never_establishes_attribution(call_id):
    data = case([event('ingress', '100'),
                 event('provider_start', '101', source='provider_telemetry', call_id=call_id, provider='p', model='m'),
                 event('provider_end', '104', source='provider_telemetry', call_id=call_id, provider='p', model='m')])
    assert evaluate(data, 'one')['provider_attribution'] == 'UNKNOWN'


def test_high_precision_above_boundary_cannot_round_to_pass():
    report = evaluate(case([event('ingress', '100'),
                            event('progress', '105.00000000000000000000000000001', delivered=True)]), 'one')
    assert report['receipt']['status'] == 'FAIL'


def test_extreme_exponent_timestamp_rejected():
    with pytest.raises(ValueError):
        evaluate(case([event('ingress', '1e999999999')]), 'one')
