"""Reproduce review findings before changing the producer/consumer gates."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from geometry_fixtures import SKILLS, load_producer, real_record
from test_failclosed_gates import run_tree, generate, load


def test_reference_width_cannot_redefine_generated_stream_output_count(tmp_path):
    wrong = tmp_path / 'wrong-reference.json'
    wrong.write_text(json.dumps([[0.] * 16] * 100))  # actual branched output is 512, lanes=16
    with pytest.raises(AssertionError, match='references.*512.*16'):
        real_record('branched_small_ch_last_onnx/20260928-br-stream',
                    tmp_path / 'board', ['--reference-json', wrong])


def test_missing_generated_stream_types_never_fall_back_to_precision(tmp_path):
    with pytest.raises((ValueError, SystemExit), match='firmware|element_types'):
        load_producer().axis_lane_geometry(tmp_path, 'myproject', 32, 32, 1, 1, 24)


def test_unsigned_generated_stream_element_cannot_become_signed(tmp_path):
    firmware = tmp_path / 'firmware'
    firmware.mkdir()
    (firmware / 'myproject.h').write_text(
        'void myproject(hls::stream<input_t> &in, hls::stream<result_t> &out);')
    (firmware / 'defines.h').write_text(
        'typedef nnet::array<ap_fixed<24,6>, 1> input_t;\n'
        'typedef nnet::array<ap_ufixed<24,6>, 1> result_t;')
    with pytest.raises((ValueError, SystemExit), match='signed|ap_fixed|element_types'):
        load_producer().axis_lane_geometry(tmp_path, 'myproject', 32, 32, 1, 1, 24)


@pytest.mark.parametrize('wrapper', [None, [], 'broken'])
def test_nonmapping_wrapper_produces_named_error(wrapper, tmp_path):
    with pytest.raises(ValueError, match='wrapper.*expected.*mapping'):
        load('host_geometry').geometry_for_wrapper(tmp_path / 'contract.yaml', {}, wrapper)


def test_incomplete_host_record_diagnostic_is_identical_across_processes(run_tree):
    wrapper = generate(run_tree)
    wrapper['host_geometry'] = {}
    path = run_tree['tmp'] / 'incomplete-host.json'
    path.write_text(json.dumps(wrapper))
    script = '''import json,sys,yaml
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import host_geometry as h
w=json.loads(Path(sys.argv[2]).read_text())
p=Path(w['contract']);c=yaml.safe_load(p.read_text(encoding='utf-8-sig'))
try:h.geometry_for_wrapper(p,c,w)
except ValueError as e:print(str(e))
'''
    outputs = []
    for seed in ['1','2','3','4']:
        result = subprocess.run([sys.executable, '-B', '-c', script,
            str(SKILLS / 'board-host-io/scripts'), str(path)],
            env=dict(os.environ, PYTHONHASHSEED=seed), capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout)
    assert len(set(outputs)) == 1, outputs
