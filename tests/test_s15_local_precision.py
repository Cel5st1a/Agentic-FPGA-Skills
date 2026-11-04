"""Local precision selection must be backed by actual generated C++ observations."""

import json
import os
import subprocess

from test_s15_numeric_policy import ROOT, load


def test_host_comparison_keeps_numeric_failures_and_unknowns():
    mod = load("hls4ml-convert/scripts/local_precision_search.py", "local_search")
    assert mod.compare_outputs([[0.002]], [[0.0]])["passed"] is False
    assert mod.compare_outputs([[0.002]], [[0.00195]])["passed"] is True
    for actual in ([], [[float("nan")]], [[0.0, 0.002]]):
        assert mod.compare_outputs([[0.002]], actual)["passed"] is False
    overflow = mod.compare_outputs([[1e308]], [[-1e308]])
    assert overflow["passed"] is False and overflow["max_abs"] is None


def test_changed_original_invalidates_summary_instead_of_leaving_a_pass(tmp_path):
    mod = load("hls4ml-convert/scripts/local_precision_search.py", "changed_original")
    source = tmp_path / "model.h5"
    source.write_text("before")
    before = {"model": mod.digest(source)}
    source.write_text("after")
    result = {"status": "LOCAL_HOST_PASS", "selected_precision": "ap_fixed<24,6>"}
    try:
        mod.verify_originals(before, {"model": source}, result, tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("changed original accepted")
    saved = json.loads((tmp_path / "summary.json").read_text())
    assert saved["status"] == "INVALIDATED_INPUT_CHANGE"
    assert saved["selected_precision"] is None


def test_role_override_preserves_scalar_default_and_original_mapping():
    mod = load("hls4ml-convert/scripts/local_precision_search.py", "local_override")
    source = {"hls_config": {"LayerName": {"dense": {"Precision": "ap_fixed<18,6>"}}}}
    changed = mod.local_override(source, "dense", "weight", "ap_fixed<24,6>")
    assert changed["hls_config"]["LayerName"]["dense"]["Precision"] == {
        "default": "ap_fixed<18,6>",
        "weight": "ap_fixed<24,6>",
    }
    assert source["hls_config"]["LayerName"]["dense"]["Precision"] == "ap_fixed<18,6>"
    assert mod.local_override({"hls_config": None}, "dense", "bias", "ap_fixed<20,6>")


PROBE = r"""
import hashlib,json,sys
from pathlib import Path
import numpy as np
import tensorflow as tf
import yaml
root,work=Path(sys.argv[1]),Path(sys.argv[2])
sys.path.insert(0,str(root/'hls4ml-convert/scripts'))
import local_precision_search as search
model=tf.keras.Sequential([tf.keras.layers.Input(shape=(1,),name='features'),
    tf.keras.layers.Dense(1,activation='linear',name='dense')])
model.set_weights([np.array([[.0002]],dtype=np.float32),np.array([0],dtype=np.float32)])
model.save(work/'original.h5',include_optimizer=False)
original=hashlib.sha256((work/'original.h5').read_bytes()).hexdigest()
x=np.random.default_rng(20261025).uniform(8,16,(256,1)).astype(np.float32)
(work/'inputs.json').write_text(json.dumps(x.tolist()))
(work/'board.yaml').write_text(yaml.safe_dump({'part':'xc7a200tfbg484-2'}))
contract={'model':{'path':'original.h5','frontend':'keras_h5','input_shape':[256,1]},
    'board_profile':'board.yaml','precision':'ap_fixed<18,6>','reuse_factor':1,
    'strategy':'Resource','io_type':'io_parallel','clock_period_ns':10,
    'verification':{'inputs':'inputs.json'}}
deployment=work/'deployment.yaml'
deployment.write_text(yaml.safe_dump(contract))
result=search.search(deployment,work/'attempts','dense','weight',['invalid_precision','ap_fixed<24,6>'],
                     compiler=Path('D:/mingw64/bin/g++.exe'))
assert len(result['attempts'])==3,result
assert result['attempts'][0]['comparison']['passed'] is False,result
assert result['attempts'][0]['comparison']['violations']==256,result
failed=result['attempts'][1]
assert failed['status']=='FAILED' and failed['comparison'] is None,result
assert result['attempts'][2]['comparison']['passed'] is True,result
assert result['selected_precision']=='ap_fixed<24,6>'
assert result['hardware_verified'] is False
assert result['target_layer']=='dense' and result['parameter_role']=='weight'
assert result['original_model_sha256']==original
assert hashlib.sha256((work/'original.h5').read_bytes()).hexdigest()==original
for trial in result['attempts']:
    assert trial['effective_hls_config']['Model']['Precision']['default']=='ap_fixed<18,6>'
selected=result['attempts'][2]
setting=selected['effective_hls_config']['LayerName']['dense']['Precision']
assert setting=={'weight':'ap_fixed<24,6>'}
assert 'fixed<24,6' in result['attempts'][2]['actual_parameter_precision']
contract['hls_config']={'LayerName':{'dense':{'Precision':{'weight':'ap_fixed<24,6>'}}}}
passing=work/'passing.yaml'
passing.write_text(yaml.safe_dump(contract))
reused=search.search(passing,work/'already-passing','dense','weight',['ap_fixed<32,6>'],
                     compiler=Path('D:/mingw64/bin/g++.exe'))
assert reused['status']=='BASELINE_HOST_PASS' and len(reused['attempts'])==1
try: search.search(deployment,work/'attempts','dense','weight',[],compiler=Path('unused'))
except FileExistsError: pass
else: raise AssertionError('old attempts overwritten')
print('LOCAL_PRECISION_CPP_PASS')
"""


def test_actual_cpp_selects_one_local_role_without_global_precision_change(tmp_path):
    run = subprocess.run(
        [
            "D:/python_envs/hls4ml_conv/Scripts/python.exe",
            "-B",
            "-c",
            PROBE,
            str(ROOT),
            str(tmp_path),
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
    assert "LOCAL_PRECISION_CPP_PASS" in run.stdout
