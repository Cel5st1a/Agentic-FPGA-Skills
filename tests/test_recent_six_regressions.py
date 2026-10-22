"""Regressions from the 2026-10-06 two-plus-four deployments; frozen runs are read-only."""
import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

SKILLS = Path(__file__).resolve().parents[1]
J25 = Path(r'D:\ML_models\_work\oh4-20261006\j25\runs\j25\a5')
QMD = Path(r'D:\ML_models\_work\openhands-ax7a200b-20261006-01\qkeras_mnist_dense')


def load(relative, name):
    path = SKILLS / relative
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('frontend', ['keras', 'onnx'])
def test_contract_config_reaches_conversion_without_mutating_inputs(frontend):
    mod = load(f'hls4ml-convert/scripts/convert_{frontend}.py', f'recent_{frontend}')
    template = {'Model': {'BramFactor': 99}}
    contract = {'precision': 'ap_fixed<20,6>', 'reuse_factor': 16, 'strategy': 'Resource',
                'layer_reuse_factors': {'dense': 4},
                'hls_config': {'Model': {'BitExact': None}, 'LayerType': {
                    'Activation': {'TableSize': 8192, 'TableType': 'ap_fixed<24,4>'}}}}
    saved = copy.deepcopy((template, contract))
    result = mod.build_hls_config(template, contract, ['softmax'])
    assert result['LayerType']['Activation']['TableSize'] == 8192
    assert result['LayerName']['dense']['ReuseFactor'] == 4
    assert result['LayerName']['softmax']['skip'] is True
    assert 'BitExact' not in result['Model']
    assert (template, contract) == saved


@pytest.mark.parametrize('frontend', ['keras', 'onnx'])
def test_conflicting_frozen_reuse_factors_are_refused(frontend):
    mod = load(f'hls4ml-convert/scripts/convert_{frontend}.py', f'conflict_{frontend}')
    contract = {'precision': 'ap_fixed<20,6>', 'reuse_factor': 16, 'strategy': 'Resource',
                'layer_reuse_factors': {'dense': 4},
                'hls_config': {'LayerName': {'dense': {'ReuseFactor': 8}}}}
    with pytest.raises(ValueError, match='ReuseFactor'):
        mod.build_hls_config({}, contract, [])


def test_audit_accepts_optional_null_sections_and_preserves_requested_record(tmp_path):
    mod = load('hls4ml-convert/scripts/contract_config.py', 'recent_config_null')
    requested = {'LayerName': None, 'LayerType': None}
    record = mod.firmware_config_audit(tmp_path, requested, {})
    assert record['contract_hls_config'] == requested
    assert record['warnings'] == []


def test_case_insensitive_layer_reuse_conflict_is_not_silently_overwritten():
    mod = load('hls4ml-convert/scripts/convert_keras.py', 'recent_config_case')
    contract = {'precision': 'ap_fixed<20,6>', 'reuse_factor': 16, 'strategy': 'Resource',
                'layer_reuse_factors': {'dense': 4},
                'hls_config': {'LayerName': {'Dense': {'ReuseFactor': 8}}}}
    with pytest.raises(ValueError, match='ReuseFactor'):
        mod.build_hls_config({}, contract, [])


def test_table_audit_does_not_accept_the_right_size_on_the_wrong_layer(tmp_path):
    mod = load('hls4ml-convert/scripts/contract_config.py', 'recent_config_node')
    firmware = tmp_path / 'firmware'
    firmware.mkdir()
    (firmware / 'parameters.h').write_text(
        '// first\nstruct config1 : nnet::activ_config {\n'
        '    static const unsigned table_size = 1024;\n};\n'
        '// second\nstruct config2 : nnet::activ_config {\n'
        '    static const unsigned table_size = 8192;\n};\n')
    config = {'LayerType': {'Activation': {'TableSize': 8192}}}
    nodes = [{'name': 'first', 'class_name': 'Activation'},
             {'name': 'second', 'class_name': 'Activation'}]
    record = mod.firmware_config_audit(tmp_path, config, config, nodes=nodes)
    assert record['status'] == 'FAILED'
    assert record['layer_checks'][0]['TableSize'] == {'requested': 8192, 'emitted': 1024}
    assert record['layer_checks'][1]['TableSize'] == {'requested': 8192, 'emitted': 8192}


@pytest.mark.parametrize('body', [None, '// first\nstruct config1 : nnet::activ_config {\n};\n'])
def test_requested_table_with_missing_generated_evidence_is_not_passed(tmp_path, body):
    mod = load('hls4ml-convert/scripts/contract_config.py', 'recent_config_coverage')
    if body is not None:
        (tmp_path / 'firmware').mkdir()
        (tmp_path / 'firmware/parameters.h').write_text(body)
    config = {'LayerType': {'Activation': {'TableSize': 8192}}}
    record = mod.firmware_config_audit(tmp_path, config, config,
        nodes=[{'name': 'first', 'class_name': 'Activation'}])
    assert record['status'] == 'FAILED'
    assert any('first.TableSize' in reason for reason in record['mismatches'])


def test_j25_real_multibeat_geometry_and_invalid_output_rejection():
    hg = load('board-host-io/scripts/host_geometry.py', 'recent_geometry')
    contract = yaml.safe_load((J25 / 'contract/j25/deployment.yaml').read_text())
    wrapper = json.loads((J25 / 'p3-build/board/wrapper.json').read_text())
    facts = hg._core_facts(contract, wrapper)
    assert facts[:2] == (625, 5)
    broken = copy.deepcopy(wrapper)
    broken['interface']['stream']['output_beats_per_sample'] = 5
    with pytest.raises(ValueError, match='output_beats_per_sample'):
        hg._core_facts(contract, broken)
    broken = copy.deepcopy(wrapper)
    broken['interface']['stream']['input_lanes_per_beat'] = True
    with pytest.raises(ValueError, match='input_lanes_per_beat'):
        hg._core_facts(contract, broken)


def test_generated_po2_patch_is_exact_idempotent_and_fail_closed(tmp_path):
    mod = load('hls4ml-convert/scripts/generated_compat.py', 'recent_compat')
    dest = tmp_path / 'firmware/nnet_utils/nnet_mult.h'
    dest.parent.mkdir(parents=True)
    original = QMD / 'compat/po2-product-width/nnet_mult.before.h'
    dest.write_bytes(original.read_bytes())
    before = original.read_bytes()
    result = mod.patch_po2_product(tmp_path, '1.3.0')
    assert result['status'] == 'APPLIED'
    assert result['after_sha256'] == '79697a887ac1d5b5303211c7c22a3772f2b2efd432dd8b3ac13f337d329401bf'
    assert original.read_bytes() == before
    assert mod.patch_po2_product(tmp_path, '1.3.0')['status'] == 'ALREADY_APPLIED'
    dest.write_bytes(b'unknown upstream template')
    with pytest.raises(ValueError, match='unrecognized'):
        mod.patch_po2_product(tmp_path, '1.3.0')
    assert dest.read_bytes() == b'unknown upstream template'


def test_j25_rom_generation_keeps_all_62500_elements_in_order(tmp_path):
    old = json.loads((J25 / 'p3-build/board/wrapper.json').read_text())
    cmd = [sys.executable, '-B', str(SKILLS / 'vivado-manager/scripts/make_board_wrapper.py'),
           '--deployment', old['contract'], '--verilog-dir', str(Path(old['core_verilog']).parent),
           '--top', old['top'], '--output', str(tmp_path), '--board-profile',
           str(SKILLS / 'board-profiles/AX7A200B/board_profile.yaml')]
    run = subprocess.run(cmd, capture_output=True, text=True)
    assert run.returncode == 0, run.stdout + run.stderr
    mem = [int(v, 16) for v in (tmp_path / 'input_words.mem').read_text().splitlines()]
    # This frozen successful run uses the independently verified narrow ROM.
    expected = [int(v, 16) for v in Path(old['rom_image']['path']).read_text().splitlines()]
    assert len(mem) == 62500
    assert mem == expected
    top = (tmp_path / 'board_top.v').read_text()
    assert 'beat_rom' in top and 'rom_dvalid_q' in top


def test_multibeat_adapter_handshake_and_window_alignment(tmp_path):
    runner = load('board-host-io/scripts/run_rtl_tb.py', 'recent_rtl')
    result = runner.run_testbench('host_stream_multibeat_tb', [
        SKILLS / 'board-host-io/assets/host_stream_adapter.v',
        SKILLS / 'board-host-io/tests/host_stream_multibeat_tb.v'], tmp_path)
    assert result['returncode'] == 0, result['stdout']
    assert result['marker'] == 'HOST_STREAM_MULTIBEAT_PASS'
