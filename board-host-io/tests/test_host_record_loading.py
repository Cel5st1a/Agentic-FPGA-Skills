"""Host consumers must use the materialised record, with identity and consistency gates."""
import copy
import hashlib
import json

import pytest
import yaml

from test_failclosed_gates import run_tree, generate, load


def read_host(tree, wrapper):
    contract = yaml.safe_load(tree['deployment'].read_text(encoding='utf-8-sig'))
    return load('host_geometry').geometry_for_wrapper(tree['deployment'], contract, wrapper)


def test_consumption_does_not_reload_changed_board_profile(run_tree):
    wrapper = generate(run_tree)
    profile = json.loads(run_tree['profile_path'].read_text())
    profile['peripherals']['uart']['baudrate'] = 230400
    run_tree['profile_path'].write_text(json.dumps(profile))
    assert read_host(run_tree, wrapper).baudrate == 115200


@pytest.mark.parametrize('field', ['request_bytes', 'payload_bytes', 'scale_out', 'in_elements'])
def test_tampered_materialised_geometry_is_refused(run_tree, field):
    wrapper = generate(run_tree)
    wrapper['host_geometry'][field] += 1
    with pytest.raises(ValueError, match='host_geometry.' + field):
        read_host(run_tree, wrapper)


def test_host_protocol_is_only_a_checked_projection(run_tree):
    wrapper = generate(run_tree)
    wrapper['host_protocol']['baudrate'] = 230400
    with pytest.raises(ValueError, match='host_protocol.baudrate'):
        read_host(run_tree, wrapper)


def test_missing_materialised_geometry_never_rebuilds_it(run_tree):
    wrapper = generate(run_tree)
    del wrapper['host_geometry']
    with pytest.raises(ValueError, match='host_geometry'):
        read_host(run_tree, wrapper)


def test_changed_contract_bytes_invalidate_geometry(run_tree):
    wrapper = generate(run_tree)
    with run_tree['deployment'].open('a') as stream:
        stream.write('\n# changed identity\n')
    with pytest.raises(ValueError, match='contract_sha256'):
        read_host(run_tree, wrapper)


def test_builder_refuses_source_record_for_another_contract(run_tree):
    run_tree['record']['contract_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='contract_sha256'):
        generate(run_tree)


def test_shared_wire_code_is_in_capture_provenance(run_tree):
    module = load('verify_board')
    path = run_tree['deployment']
    sources = module.provenance_sources(*([path] * 9))
    assert any(p.name == 'geometry.py' and p.parent.name == 'line' for p in sources)


@pytest.mark.parametrize('consumer', ['verify_board', 'simulate_host'])
def test_host_sample_flatten_refuses_ragged_input(consumer):
    with pytest.raises(ValueError, match='ragged'):
        load(consumer).flatten([[1,2],[3]])
