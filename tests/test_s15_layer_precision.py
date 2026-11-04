"""Actual generated C++ output/accumulator trials, with comparable layer traces."""

import os
import subprocess

import pytest
from test_s15_numeric_policy import ROOT

PROBE = r"""
import json,sys
from pathlib import Path
import numpy as np
import tensorflow as tf
import yaml
root,work,role=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
sys.path.insert(0,str(root/'hls4ml-convert/scripts'))
import local_precision_search as search
model=tf.keras.Sequential([tf.keras.layers.Input(shape=(1,),name='features'),
    tf.keras.layers.Dense(1,name='hidden'),tf.keras.layers.Activation('relu',name='relu'),
    tf.keras.layers.Dense(1,name='readout')])
model.set_weights([np.array([[1]],np.float32),np.array([0],np.float32),
                   np.array([[.25]],np.float32),np.array([0],np.float32)])
model.save(work/'original.h5',include_optimizer=False)
x=np.random.default_rng(2026102504).uniform(5,6,(256,1)).astype(np.float32)
(work/'inputs.json').write_text(json.dumps(x.tolist()))
(work/'board.yaml').write_text(yaml.safe_dump({'part':'xc7a200tfbg484-2'}))
contract={'model':{'path':'original.h5','frontend':'keras_h5','input_shape':[256,1]},
    'board_profile':'board.yaml','precision':'ap_fixed<24,8>','reuse_factor':1,
    'strategy':'Resource','io_type':'io_parallel','clock_period_ns':10,
    'hls_config':{'LayerName':{'hidden':{'Precision':{role:'ap_fixed<8,2>'}}}},
    'verification':{'inputs':'inputs.json'}}
deployment=work/'deployment.yaml'
if role=='accum':
    block=contract['hls_config']['LayerName']['hidden']
    block['accum_t']=block['Precision'].pop('accum')
deployment.write_text(yaml.safe_dump(contract))
result=search.search(deployment,work/'attempts','hidden',role,['ap_fixed<24,8>'],
    compiler=Path('D:/mingw64/bin/g++.exe'),trace_layers=True)
assert result['status']=='LOCAL_HOST_PASS',result
assert len(result['attempts'])==2,result
baseline,changed=result['attempts']
assert baseline['comparison']['violations']==256,baseline
assert changed['comparison']['violations']==0,changed
assert baseline['layer_diagnostics']['first_observed_divergence']=='hidden',baseline
assert changed['layer_diagnostics']['first_observed_divergence'] is None,changed
assert changed['actual_role_precision'].startswith('fixed<24,8'),changed
for layer in ('relu','readout'):
    assert baseline['actual_layer_types'][layer]==changed['actual_layer_types'][layer]
assert result['hardware_verified'] is False and result['original_inputs_unchanged'] is True
assert (work/'attempts/attempt-000/host-csim/layer-outputs.json').is_file()
print('LAYER_PRECISION_CPP_PASS')
"""


@pytest.mark.parametrize("role", ["result", "accum"])
def test_actual_cpp_local_result_and_accum_with_first_divergence(tmp_path, role):
    run = subprocess.run(
        [
            "D:/python_envs/hls4ml_conv/Scripts/python.exe",
            "-B",
            "-c",
            PROBE,
            str(ROOT),
            str(tmp_path),
            role,
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
    assert "LAYER_PRECISION_CPP_PASS" in run.stdout
