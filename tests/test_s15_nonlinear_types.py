"""Nonlinear table precision requests must reach the actual emitted types."""

import os
import subprocess
from types import SimpleNamespace

import pytest
from test_s15_numeric_policy import ROOT, load


def test_injected_unused_type_attribute_is_not_an_actual_role():
    mod = load("hls4ml-convert/scripts/local_precision_search.py", "unused_table_role")
    node = SimpleNamespace(
        weights={},
        expected_attributes=[],
        get_attr=lambda name: SimpleNamespace(precision="fixed<24,5>"),
    )
    with pytest.raises(ValueError, match="not present"):
        mod.role_precision(node, "inv_inp")


@pytest.mark.parametrize('role,attribute',[('exp_table','exp_table_t'),('accum','Accum_t')])
def test_local_override_replaces_existing_backend_type_spelling(role,attribute):
    mod=load('hls4ml-convert/scripts/local_precision_search.py','explicit_local_role')
    original={'hls_config':{'LayerName':{'target':{attribute:'ap_fixed<18,8>',
               'Precision':{'result':'ap_fixed<24,8>'}}}}}
    changed=mod.local_override(original,'target',role,'ap_fixed<24,5>')
    block=changed['hls_config']['LayerName']['target']
    assert block[attribute]=='ap_fixed<24,5>'
    assert block['Precision']['result']=='ap_fixed<24,8>'
    assert original['hls_config']['LayerName']['target'][attribute]=='ap_fixed<18,8>'


@pytest.mark.parametrize("section,name", [("LayerName", "probabilities"), ("LayerType", "Softmax")])
def test_table_precision_roles_bind_to_backend_attributes(section, name):
    mod = load("hls4ml-convert/scripts/contract_config.py", "nonlinear_types")
    contract = {
        "precision": "ap_fixed<24,8>",
        "reuse_factor": 1,
        "strategy": "Resource",
        "hls_config": {
            section: {
                name: {"Precision": {"result": "ap_fixed<24,8>", "inv_inp": "ap_fixed<24,5>"}}
            }
        },
    }
    actual = mod.apply_contract_config({}, contract, [])[section][name]
    assert actual["inv_inp_t"] == "ap_fixed<24,5>"
    assert actual["Precision"] == {"result": "ap_fixed<24,8>"}
    assert "inv_inp" in contract["hls_config"][section][name]["Precision"]
    contract["hls_config"][section][name]["InvInp_t"] = "ap_fixed<18,8>"
    with pytest.raises(ValueError, match="conflict"):
        mod.apply_contract_config({}, contract, [])


PROBE = r"""
import json,sys
from pathlib import Path
import numpy as np
import tensorflow as tf
import yaml
root,work=Path(sys.argv[1]),Path(sys.argv[2])
sys.path.insert(0,str(root/'hls4ml-convert/scripts'))
import local_precision_search as search
model=tf.keras.Sequential([tf.keras.layers.Input(shape=(8,),name='logits'),
    tf.keras.layers.Activation('softmax',name='probabilities')])
model.save(work/'original.h5',include_optimizer=False)
x=np.random.default_rng(2026102505).uniform(-1,1,(256,8)).astype(np.float32)
(work/'inputs.json').write_text(json.dumps(x.tolist()))
(work/'board.yaml').write_text(yaml.safe_dump({'part':'xc7a200tfbg484-2'}))
contract={'model':{'path':'original.h5','frontend':'keras_h5','input_shape':[256,8]},
    'board_profile':'board.yaml','precision':'ap_fixed<24,8>','reuse_factor':1,
    'strategy':'Resource','io_type':'io_parallel','clock_period_ns':10,
    'hls_config':{'LayerName':{'probabilities':{'Implementation':'stable','TableSize':32768,
      'Precision':{'exp_table':'ap_fixed<24,5>','inv_table':'ap_fixed<24,8>',
                   'inv_inp':'ap_fixed<16,10>'}}}},
    'verification':{'inputs':'inputs.json'}}
if sys.argv[3]=='explicit':
    block=contract['hls_config']['LayerName']['probabilities']
    block['inv_inp_t']=block['Precision'].pop('inv_inp')
deployment=work/'deployment.yaml';deployment.write_text(yaml.safe_dump(contract))
result=search.search(deployment,work/'attempts','probabilities','inv_inp',
    ['ap_fixed<24,5>'],compiler=Path('D:/mingw64/bin/g++.exe'),trace_layers=True)
assert result['status']=='LOCAL_HOST_PASS',result
baseline,changed=result['attempts']
assert baseline['comparison']['violations']>0,baseline
assert changed['comparison']['violations']==0,changed
assert changed['comparison']['values']==2048,changed
assert changed['actual_role_precision'].startswith('fixed<24,5'),changed
for role in ['result','exp_table','inv_table','accum']:
    old_type=baseline['actual_layer_types']['probabilities'][role]
    assert old_type==changed['actual_layer_types']['probabilities'][role]
assert baseline['layer_diagnostics']['first_observed_divergence']=='probabilities'
assert result['original_inputs_unchanged'] is True
print('NONLINEAR_LOCAL_CPP_PASS')
"""


@pytest.mark.parametrize('spelling',['precision','explicit'])
def test_actual_full_softmax_single_nonlinear_role_cpp(tmp_path,spelling):
    run = subprocess.run(
        [
            "D:/python_envs/hls4ml_conv/Scripts/python.exe",
            "-B",
            "-c",
            PROBE,
            str(ROOT),
            str(tmp_path),
            spelling,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "TF_CPP_MIN_LOG_LEVEL": "2"},
    )
    (tmp_path / "probe.stdout.txt").write_text(run.stdout, encoding="utf-8")
    (tmp_path / "probe.stderr.txt").write_text(run.stderr, encoding="utf-8")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "NONLINEAR_LOCAL_CPP_PASS" in run.stdout
