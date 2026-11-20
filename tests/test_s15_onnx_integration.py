"""Actual ONNX conversion/reference development probes; no synthesis or board I/O."""

import json
import os
import subprocess
import sys

import pytest
from test_s15_numeric_policy import ROOT

PROBE = r"""
import hashlib,json,sys
from pathlib import Path
import numpy as np
import onnx
from onnx import helper,numpy_helper,TensorProto
import onnxruntime as ort
import yaml
root,work,activation=Path(sys.argv[1]),Path(sys.argv[2]),sys.argv[3]
sys.path.insert(0,str(root/'hls4ml-convert/scripts'))
import convert_onnx
weights=np.array([[.125,.25],[-.5,.375],[.25,-.125]],dtype=np.float32)
bias=np.array([0,.1,-.1],dtype=np.float32)
nodes=[helper.make_node('MatMul',['features','weights'],['product'],name='dense'),
       helper.make_node('Add',['product','bias'],['logits'],name='bias_add'),
       helper.make_node(activation,['logits'],['prediction'],name='activation',
                        **({'axis':1} if activation=='Softmax' else {}))]
graph=helper.make_graph(nodes,'development',
    [helper.make_tensor_value_info('features',TensorProto.FLOAT,[1,2])],
    [helper.make_tensor_value_info('prediction',TensorProto.FLOAT,[1,3])],
    [numpy_helper.from_array(weights.T.copy(),'weights'),numpy_helper.from_array(bias,'bias')])
model=onnx.shape_inference.infer_shapes(helper.make_model(graph,opset_imports=[helper.make_opsetid('',13)]))
model.graph.value_info.extend([
    helper.make_tensor_value_info('weights',TensorProto.FLOAT,[2,3]),
    helper.make_tensor_value_info('bias',TensorProto.FLOAT,[3])])
onnx.checker.check_model(model)
path=work/'original.onnx'
onnx.save(model,str(path))
digest=hashlib.sha256(path.read_bytes()).hexdigest()
x=np.array([[.2,.8],[.7,.3]],dtype=np.float32)
runtime=ort.InferenceSession(str(path),providers=['CPUExecutionProvider'])
expected=np.concatenate([runtime.run(None,{'features':row[None,:]})[0] for row in x])
z=x@weights.T+bias
analytic=np.exp(z-z.max(axis=1,keepdims=True)) if activation=='Softmax' else 1/(1+np.exp(-z))
if activation=='Softmax': analytic/=analytic.sum(axis=1,keepdims=True)
np.testing.assert_allclose(expected,analytic,atol=1e-7,rtol=1e-7)
(work/'inputs.json').write_text(json.dumps(x.tolist()))
(work/'board.yaml').write_text(yaml.safe_dump({'part':'xc7a200tfbg484-2'}))
contract={'model':{'path':'original.onnx','frontend':'onnx','input_shape':[2,2]},
    'board_profile':'board.yaml','precision':'ap_fixed<24,6>','reuse_factor':1,
    'strategy':'Resource','io_type':'io_parallel','clock_period_ns':10,
    'verification':{'inputs':'inputs.json'}}
(work/'deployment.yaml').write_text(yaml.safe_dump(contract))
sys.argv=['convert_onnx','--deployment',str(work/'deployment.yaml'),
          '--output',str(work/'project'),'--evidence',str(work/'conversion.json')]
assert convert_onnx.main()==0
actual=np.asarray(json.loads((work/'python-output.json').read_text()),dtype=np.float32)
np.testing.assert_allclose(actual,expected,atol=1e-7,rtol=1e-7)
record=json.loads((work/'conversion.json').read_text())
assert record['gate_quantity']['declared']=='auto'
assert record['gate_quantity']['skipped_hls4ml_nodes']==[]
assert record['numerical_verdict']=='NOT_EVALUATED_BY_THIS_SCRIPT'
assert hashlib.sha256(path.read_bytes()).hexdigest()==digest
source=(work/'project/firmware/myproject.cpp').read_text()
assert 'nnet::'+activation.lower() in source,source
print('ONNX_CONVERTER_INTEGRATION_PASS')
"""


@pytest.mark.parametrize("activation", ["Softmax", "Sigmoid"])
def test_actual_onnx_converter_preserves_graph_and_reference(tmp_path, activation):
    result = subprocess.run(
        [
            os.environ.get("S15_ONNX_TEST_PYTHON", sys.executable),
            "-B",
            "-c",
            PROBE,
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
    assert "ONNX_CONVERTER_INTEGRATION_PASS" in result.stdout
    record = json.loads((tmp_path / "conversion.json").read_text())
    assert record["reference"]["samples"] == 2
