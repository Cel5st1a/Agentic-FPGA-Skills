"""Explicit test records and read-only real assets; no production inference fallback."""
import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import yaml

SKILLS = Path(__file__).resolve().parents[2]
RUNS = Path(r'D:\ML_models\_line\runs')
REAL_CASES = [
    ('keras_3layer/20260927-p20r16b', 16, 5),
    ('keras_conv1d_small/20260927-c1d-p20', 40, 5),
    ('keras_conv2d_model/20260927-c2d-p24layers', 64, 10),
]


def load_producer():
    spec = importlib.util.spec_from_file_location('geometry_producer',
        SKILLS / 'vivado-manager/scripts/make_board_wrapper.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def real_record(case, output, extra_args=()):
    old_path = RUNS / case / 'p3-build/board/wrapper.json'
    old = json.loads(old_path.read_text(encoding='utf-8-sig'))
    deployment = Path(old['contract'])
    assert hashlib.sha256(deployment.read_bytes()).hexdigest() == old['contract_sha256']
    core = Path(old['core_verilog'])
    assert hashlib.sha256(core.read_bytes()).hexdigest() == old['core_verilog_sha256']
    command = [sys.executable, '-B', str(SKILLS / 'vivado-manager/scripts/make_board_wrapper.py'),
        '--deployment', str(deployment), '--verilog-dir', str(core.parent),
        '--top', old['top'], '--output', str(output),
        '--board-profile', str(SKILLS / 'board-profiles/AX7A200B/board_profile.yaml')]
    result = subprocess.run([*command, *map(str, extra_args)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((output / 'wrapper.json').read_text(encoding='utf-8'))
    assert record['contract_sha256'] == old['contract_sha256']
    contract = yaml.safe_load(deployment.read_text(encoding='utf-8-sig'))
    return deployment, contract, record


def wrapper_record(elements=64, width=4, precision='ap_fixed<32,12>', interface=None):
    """An explicit synthetic build fixture; only tests construct hardware this way."""
    bits, integer = map(int, precision[9:-1].split(','))
    if interface is None:
        interface = {'style': 'ap_vld_array_with_ap_ctrl_hs',
            'input_bits': elements * bits, 'elements': elements,
            'result_outputs': [f'out_{i}' for i in range(width)],
            'output_bits': {f'out_{i}': bits for i in range(width)}}
    return {'input_layout': {'flatten': 'c_order', 'elements': elements},
        'output_layout': {'flatten': 'c_order', 'elements': width},
        'rom_image': {'grid': {'total_bits': bits, 'integer_bits': integer}},
        'output_grid': {'total_bits': bits, 'integer_bits': integer},
        'interface': copy.deepcopy(interface)}


def board_facts_module():
    spec = importlib.util.spec_from_file_location(
        'board_facts', SKILLS / 'board-host-io/scripts/board_facts.py')
    module = importlib.util.module_from_spec(spec)
    sys.modules['board_facts'] = module
    spec.loader.exec_module(module)
    return module


def facts(kind='differential', **overrides):
    """A complete BoardFacts for render-level tests, provenance included.

    Both topologies are here because the difference between them is exactly what the
    second board taught this tree: the AX7A200B drives 200 MHz differentially (IBUFDS,
    two clock ports) and the Nexys Video 100 MHz single-ended (IBUF, one clock port).
    """
    if kind == 'differential':
        profile = {
            'board_id': 'AX7A200B', 'fpga': {'part': 'xc7a200tfbg484-2'},
            'board_pins': {
                'clock': {'kind': 'differential', 'clk_p': 'R4', 'clk_n': 'T4',
                          'frequency_mhz': 200, 'core_frequency_mhz': 100,
                          'iostandard': 'DIFF_SSTL15', 'provenance': 'test fixture'},
                'reset': {'pin': 'F15', 'iostandard': 'LVCMOS33',
                          'provenance': 'test fixture'}},
            'peripherals': {'uart': {'fpga_rxd': 'L14', 'fpga_txd': 'L15',
                                     'iostandard': 'LVCMOS33',
                                     'host_bridge': {'chip': 'CP2102GM', 'usb_vid': '10c4',
                                                     'usb_pid': 'ea60',
                                                     'serial_number': 'TESTSERIAL',
                                                     'provenance': 'test fixture'},
                                     'provenance': 'test fixture'}}}
    else:
        profile = {
            'board_id': 'NexysVideo', 'fpga': {'part': 'xc7a200tsbg484-1'},
            'board_pins': {
                'clock': {'kind': 'single_ended', 'pin': 'R4', 'frequency_mhz': 100,
                          'core_frequency_mhz': 100, 'iostandard': 'LVCMOS33',
                          'provenance': 'test fixture'},
                'reset': {'pin': 'G4', 'iostandard': 'LVCMOS15',
                          'provenance': 'test fixture'}},
            'peripherals': {'uart': {'fpga_rxd': 'V18', 'fpga_txd': 'AA19',
                                     'iostandard': 'LVCMOS33',
                                     'host_bridge': {'chip': 'FT232R', 'usb_vid': '0403',
                                                     'usb_pid': '6001',
                                                     'serial_number': 'TESTSERIAL',
                                                     'provenance': 'test fixture'},
                                     'provenance': 'test fixture'}}}
    profile.update(overrides)
    return board_facts_module().load_facts(profile, '<test board profile>')


def synthetic_geometry(module, contract, references=None, interface=None, board_profile=None):
    elements = contract.get('model', {}).get('input_shape', [0])[-1]
    width = len(references[0]) if references and isinstance(references[0], (list, tuple)) else 4
    if interface is not None and interface.get('style') == 'ap_vld_array_with_ap_ctrl_hs':
        interface = copy.deepcopy(interface)
        bits = int(contract.get('precision', 'ap_fixed<32,12>')[9:].split(',')[0])
        interface.setdefault('output_bits', {p: bits for p in interface['result_outputs']})
    record = wrapper_record(elements, width, contract.get('precision', 'ap_fixed<32,12>'), interface)
    return module.geometry_from_contract(contract, references=references,
        wrapper=record, board_profile=board_profile)
