"""Real CPU/frontend generation on toy models, never EDA or board acceptance."""

import json
import os
import subprocess

import pytest
from test_s15_numeric_policy import ROOT


@pytest.mark.parametrize("activation", ["softmax", "sigmoid"])
def test_converter_keeps_original_activation_and_python_reference(tmp_path, activation):
    probe = """import json,sys
from pathlib import Path
import numpy as np
import tensorflow as tf
import yaml
root, work, activation = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
sys.path.insert(0, str(root/'hls4ml-convert/scripts'))
import convert_keras
model=tf.keras.Sequential([tf.keras.layers.Input(shape=(2,),name='features'),
    tf.keras.layers.Dense(3,activation=activation,name='prediction')])
model.set_weights([np.array([[.125,.25,-.5],[.25,-.125,.375]],dtype=np.float32),
                  np.array([0,.1,-.1],dtype=np.float32)])
model.save(work/'original.h5',include_optimizer=False)
x=np.array([[.2,.8],[.7,.3]],dtype=np.float32)
expected=model(x,training=False).numpy()
(work/'inputs.json').write_text(json.dumps(x.tolist()))
(work/'board.yaml').write_text(yaml.safe_dump({'id':'AX7A200B','part':'xc7a200tfbg484-2'}))
contract={'model':{'path':'original.h5','frontend':'keras_h5','input_shape':[2,2]},
    'board_profile':'board.yaml','precision':'ap_fixed<24,6>','reuse_factor':1,
    'strategy':'Resource','io_type':'io_parallel','clock_period_ns':10,
    'verification':{'inputs':'inputs.json'}}
(work/'deployment.yaml').write_text(yaml.safe_dump(contract))
sys.argv=['convert_keras','--deployment',str(work/'deployment.yaml'),
          '--output',str(work/'project'),'--evidence',str(work/'conversion.json')]
assert convert_keras.main()==0
record=json.loads((work/'conversion.json').read_text())
actual=np.asarray(json.loads((work/'python-output.json').read_text()),dtype=np.float32)
np.testing.assert_array_equal(actual,expected)
assert actual.shape==(2,3)
assert record['gate_quantity']['skipped_hls4ml_nodes']==[]
assert record['numerical_verdict']=='NOT_EVALUATED_BY_THIS_SCRIPT'
source=(work/'project/firmware/myproject.cpp').read_text()
assert 'nnet::'+activation in source, source
if activation=='softmax':
    np.testing.assert_allclose(actual.sum(axis=1),1.0,atol=1e-6)
    assert record['gate_quantity']['resolved']=='probabilities'
print('TOY_CONVERTER_INTEGRATION_PASS')
"""
    result = subprocess.run(
        [
            "D:/python_envs/hls4ml_conv/Scripts/python.exe",
            "-c",
            probe,
            str(ROOT),
            str(tmp_path),
            activation,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={**os.environ, "TF_CPP_MIN_LOG_LEVEL": "2"},
    )
    (tmp_path / "probe.stdout.txt").write_text(result.stdout, encoding="utf-8")
    (tmp_path / "probe.stderr.txt").write_text(result.stderr, encoding="utf-8")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "TOY_CONVERTER_INTEGRATION_PASS" in result.stdout
    record = json.loads((tmp_path / "conversion.json").read_text())
    assert record["gate_quantity"]["skipped_hls4ml_nodes"] == []
    assert record["gate_quantity"]["declared"] == "auto"
