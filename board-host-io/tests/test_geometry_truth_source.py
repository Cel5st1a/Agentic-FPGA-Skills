"""D-M02: real multidimensional contracts must read the producer's recorded geometry."""
import copy
import sys

import pytest

from geometry_fixtures import REAL_CASES, SKILLS, real_record, wrapper_record

sys.path.insert(0, str(SKILLS / 'board-host-io/scripts'))
import host_geometry as hg


@pytest.mark.parametrize('case,inputs,outputs', REAL_CASES)
def test_real_contract_geometry_reads_generated_record(case, inputs, outputs, tmp_path):
    deployment, contract, wrapper = real_record(case, tmp_path / 'board')
    geometry = hg.geometry_for_wrapper(deployment, contract, wrapper)
    assert (geometry.in_elements, geometry.out_elements) == (inputs, outputs)


def test_contract_shape_is_not_a_geometry_source(tmp_path):
    contract = {'precision': 'ap_fixed<32,12>', 'model': {}}
    geometry = hg.geometry_for_wrapper(tmp_path / 'deployment.yaml', contract, wrapper_record())
    assert (geometry.in_elements, geometry.out_elements) == (64, 4)


@pytest.mark.parametrize('field', ['input_layout', 'rom_image', 'output_grid'])
def test_missing_build_fact_never_falls_back_to_contract(field, tmp_path):
    wrapper = wrapper_record()
    del wrapper[field]
    with pytest.raises(ValueError, match=field):
        hg.geometry_for_wrapper(tmp_path / 'deployment.yaml',
            {'precision': 'ap_fixed<32,12>', 'model': {'input_shape': [100, 64]}},
            wrapper, references=[[0.] * 4])


def test_nested_references_only_check_recorded_output_count(tmp_path):
    wrapper = wrapper_record(width=4)
    contract = {'precision': 'ap_fixed<32,12>', 'model': {'input_shape': [100,64]}}
    geom = hg.geometry_for_wrapper(tmp_path / 'deployment.yaml', contract, wrapper,
        references=[[[1,2],[3,4]]])
    assert geom.out_elements == 4


@pytest.mark.parametrize('change,field', [
    (lambda w: w['output_grid'].update(integer_bits=11), 'output_grid'),
    (lambda w: w['input_layout'].update(elements=True), 'input_layout.elements'),
    (lambda w: w['input_layout'].update(elements='64'), 'input_layout.elements'),
    (lambda w: w['interface']['result_outputs'].__setitem__(1,'out_0'), 'result_outputs'),
    (lambda w: w['interface']['output_bits'].update(out_0=64), 'output_bits'),
])
def test_conflicting_or_malformed_build_facts_are_named(change, field, tmp_path):
    wrapper = copy.deepcopy(wrapper_record())
    change(wrapper)
    with pytest.raises(ValueError, match=field):
        hg.geometry_for_wrapper(tmp_path / 'deployment.yaml',
            {'precision': 'ap_fixed<32,12>', 'model': {'input_shape': [100,64]}},
            wrapper, references=[[0.] * 4])


def test_reference_presence_cannot_select_output_geometry(tmp_path):
    wrapper = wrapper_record(width=4)
    contract = {'precision': 'ap_fixed<32,12>'}
    plain = hg.geometry_from_contract(contract, wrapper=wrapper)
    checked = hg.geometry_from_contract(contract, wrapper=wrapper, references=[[[1,2],[3,4]]])
    assert plain.to_dict() == checked.to_dict()
    with pytest.raises(ValueError, match='references result elements'):
        hg.geometry_from_contract(contract, wrapper=wrapper, references=[[1,2,3]])


def test_ragged_reference_with_equal_flattened_counts_is_refused():
    with pytest.raises(ValueError, match='references.*ragged'):
        hg.geometry_from_contract({'precision': 'ap_fixed<32,12>'},
            wrapper=wrapper_record(width=3), references=[[[1,2],[3]]])


def test_unknown_interface_requires_a_named_new_rule():
    wrapper = wrapper_record()
    wrapper['interface']['style'] = 'new_packed_interface'
    with pytest.raises(ValueError, match='interface.style.*explicit rule'):
        hg.geometry_from_contract({'precision': 'ap_fixed<32,12>'}, wrapper=wrapper)


def test_contract_only_entry_point_is_closed():
    with pytest.raises(ValueError, match='wrapper'):
        hg.geometry_from_contract({'precision': 'ap_fixed<32,12>',
            'model': {'input_shape': [100,64]}}, references=[[0.] * 4])


@pytest.mark.parametrize('case,field', [
    ('autoencoder_hls4ml/20260927-ae-p24s', 'output_beats_per_sample'),
    ('qkeras_mnist_cnn/20260928-qk-stream', 'output_grid.*precision'),
    ('qkeras_mnist_cnn/20260928-qk-stream-rf1', 'output_grid.*precision'),
    ('branched_small_ch_last_onnx/20260928-br-stream', 'output_beats_per_sample'),
])
def test_complete_real_records_still_refuse_unsupported_host_geometry(case, field, tmp_path):
    deployment, contract, wrapper = real_record(case, tmp_path / 'board')
    with pytest.raises(ValueError, match=field):
        hg.geometry_for_wrapper(deployment, contract, wrapper)


@pytest.mark.parametrize('case,field', [
    ('keras_3layer/20260927-p20r16', 'input_layout'),
    ('keras_conv1d_small/20260927-c1d-p20', 'output_grid'),
    ('autoencoder_hls4ml/20260927-ae-p24s', 'output_grid'),
])
def test_incomplete_historical_record_is_not_repaired(case, field):
    import json
    import yaml
    from pathlib import Path
    from geometry_fixtures import RUNS
    wrapper = json.loads((RUNS / case / 'p3-build/board/wrapper.json').read_text())
    path = Path(wrapper['contract'])
    contract = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
    with pytest.raises(ValueError, match=field):
        hg.geometry_for_wrapper(path, contract, wrapper)
