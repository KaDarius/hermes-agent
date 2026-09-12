"""Execute the label workflow with an inert gh CLI; never contact GitHub."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

# Exercises the real /bin/bash step on macOS, including its wait/recheck path.
pytestmark = pytest.mark.macos_only


@pytest.mark.parametrize('scenario,expected_reruns,expected_exit', [
    ('stale_initial', 0, 0),
    ('stale_after_wait', 0, 0),
    ('current', 1, 0),
    ('head_error', 0, 1),
    ('empty_head', 0, 1),
])
def test_label_rerun_only_dispatches_for_verified_current_head(tmp_path, scenario, expected_reruns, expected_exit):
    # Load executable workflow configuration, then observe its CLI effects.
    # No assertions inspect source text or mirror its control flow.
    repo = Path(__file__).resolve().parents[2]
    workflow = yaml.safe_load((repo / '.github/workflows/label-rerun.yml').read_text(encoding='utf-8'))
    step = workflow['jobs']['rerun-review-labels']['steps'][0]['run']
    (tmp_path / 'head').write_text('new' if scenario == 'stale_initial' else 'event-head', encoding='utf-8')
    gh = tmp_path / 'gh'
    gh.write_text('''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
p = Path(os.environ['PROBE_HOME'])
args = sys.argv[1:]
with (p / 'calls').open('a', encoding='utf-8') as f:
    f.write(json.dumps(args) + '\\n')
if args[:2] == ['pr', 'view']:
    if os.environ['SCENARIO'] == 'head_error':
        raise SystemExit(1)
    print('' if os.environ['SCENARIO'] == 'empty_head' else (p / 'head').read_text(encoding='utf-8'))
elif args[:2] == ['run', 'list']:
    print('42 in_progress' if os.environ['SCENARIO'] == 'stale_after_wait' else '42 completed')
elif args[:2] == ['run', 'watch']:
    (p / 'head').write_text('new', encoding='utf-8')
elif args[:2] == ['run', 'view']:
    print('completed')
elif args[:2] != ['run', 'rerun']:
    raise SystemExit(77)
''', encoding='utf-8')
    gh.chmod(0o700)
    timeout = tmp_path / 'timeout'
    timeout.write_text('#!/bin/sh\nshift\nexec "$@"\n', encoding='utf-8')
    timeout.chmod(0o700)
    env = {
        'PATH': str(tmp_path) + os.pathsep + str(Path(sys.executable).parent) + ':/usr/bin:/bin',
        'HOME': str(tmp_path), 'PROBE_HOME': str(tmp_path), 'SCENARIO': scenario,
        'HEAD_SHA': 'event-head', 'REPO': 'inert/example', 'PR_NUMBER': '3',
    }
    result = subprocess.run(['/bin/bash', '-c', step], env=env, capture_output=True, text=True, timeout=10)
    calls = [json.loads(line) for line in (tmp_path / 'calls').read_text(encoding='utf-8').splitlines()]
    assert sum(call[:2] == ['run', 'rerun'] for call in calls) == expected_reruns
    assert result.returncode == expected_exit, result.stdout + result.stderr
