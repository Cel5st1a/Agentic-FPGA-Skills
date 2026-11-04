import os
import subprocess

import pytest
from test_s15_numeric_policy import ROOT

PROBE = r"""
import sys,json,hashlib
from pathlib import Path
import numpy as np
import tensorflow as tf
from qkeras import QDense,QActivation,QConv2D
from qkeras.quantizers import quantized_bits,quantized_po2
sys.path.insert(0,str(Path(sys.argv[1])/'hls4ml-convert/scripts'))
import local_precision_search as mod
work=Path(sys.argv[2]);work.mkdir(exist_ok=True)
tf.keras.utils.set_random_seed(71)
family=sys.argv[3]
shape=(3,3,1) if family=='conv' else (6,)
i=tf.keras.Input(shape=shape,name='features')
x=QActivation('quantized_relu(2)',name='input_quantizer')(i)
if family=='conv':
    x=QConv2D(2,2,kernel_quantizer=quantized_bits(4,0,alpha=1),
              bias_quantizer=quantized_bits(4,0,alpha=1),name='qconv')(x)
    x=tf.keras.layers.Flatten(name='flatten')(x)
else:
    x=QDense(4,kernel_quantizer=quantized_po2(4,max_value=1),
             bias_quantizer=quantized_bits(4,0,alpha=1),name='qdense')(x)
x=QActivation('quantized_relu(3,1)',name='middle_quantizer')(x)
x=QDense(2,kernel_quantizer=quantized_bits(4,0,alpha=1),
         bias_quantizer=quantized_bits(4,0,alpha=1),name='scores')(x)
o=tf.keras.layers.Activation('softmax',name='probabilities')(x)
m=tf.keras.Model(i,o)
for layer in m.layers:
    arrays=layer.get_weights()
    if arrays:
        layer.set_weights([((np.arange(a.size).reshape(a.shape)%5)-1).astype('float32')/8
                           for a in arrays])
samples=np.random.default_rng(73).uniform(-.2,.9,(5,*shape)).astype('float32')
(work/'model.json').write_text(m.to_json(),encoding='utf-8');m.save_weights(str(work/'weights.h5'))
(work/'inputs.json').write_text(json.dumps(samples.tolist()))
(work/'board.yaml').write_text('fpga: {part: xc7a200tfbg484-2}\n')
import yaml
contract={'model':{'frontend':'qkeras_json_weights','quantized':True,'path':'model.json',
                  'associated_files':['weights.h5'],'input_shape':[5,*shape]},
          'precision':'ap_fixed<24,8>','reuse_factor':1,'strategy':'Latency',
          'clock_period_ns':10,'io_type':'io_parallel','board_profile':'board.yaml',
          'verification':{'inputs':'inputs.json','gate_output':'probabilities'},
          'hls_config':{'LayerName':{'probabilities':{'ExpTableSize':8192,'InvTableSize':16384}}}}
p=work/'deployment.yaml';p.write_text(yaml.safe_dump(contract))
hashfile=lambda f:hashlib.sha256((work/f).read_bytes()).hexdigest()
before={f:hashfile(f) for f in ['model.json','weights.h5','inputs.json','deployment.yaml']}
result=mod.search(p,work/'probe','scores','accum',[],
                  compiler=Path('D:/mingw64/bin/g++.exe'),trace_layers=True)
assert result['status']=='NO_PASS',result
assert result['eda_executed'] is False and result['hardware_verified'] is False
trial=result['attempts'][0]
assert trial['status']=='HOST_NUMERICAL_FAIL'
assert not trial['comparison']['passed']
assert trial['layer_diagnostics']['first_observed_divergence']=='probabilities'
assert all(row['comparison']['passed'] for row in trial['layer_diagnostics']['layers']
           if row['layer']!='probabilities' and row['comparison'] is not None)
ref=json.loads((work/'probe/original-layer-reference.json').read_text())
assert 'input_quantizer' in ref and 'middle_quantizer' in ref,ref.keys()
expected=tf.keras.Model(m.inputs,m.get_layer('middle_quantizer').output)(samples,training=False).numpy()
np.testing.assert_allclose(ref['middle_quantizer'],expected.reshape(5,-1),rtol=0,atol=0)
assert trial['layer_diagnostics']['layers']
assert all(hashfile(f)==h for f,h in before.items())
assert result['original_inputs_unchanged']
assert 'associated_file_0' in result['original_identity']
assert trial['effective_hls_config']['LayerName']['input_quantizer']['Precision']
if family=='dense':
    assert trial['generated_compatibility']['po2']['status'] in ['APPLIED','ALREADY_APPLIED']
for forbidden in ['weight','bias','result']:
    try:mod.search(p,work/('forbidden-'+forbidden),'scores',forbidden,['ap_fixed<8,2>'],
                   compiler=Path('D:/mingw64/bin/g++.exe'))
    except ValueError as e:assert 'quantizers must remain original' in str(e)
    else:raise AssertionError('trained quantizer override accepted')
print('OPT3_QUANTIZED_CPP_PASS '+family)
"""


@pytest.mark.parametrize("family", ["dense", "conv"])
def test_real_quantized_model_and_generated_cpp_preserve_original_quantizers(tmp_path, family):
    run = subprocess.run(
        [
            "D:/python_envs/hls4ml_conv/Scripts/python.exe",
            "-B",
            "-c",
            PROBE,
            str(ROOT),
            str(tmp_path),
            family,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ, TF_CPP_MIN_LOG_LEVEL="3"),
    )
    (tmp_path / "qkeras.stdout.txt").write_text(run.stdout, encoding="utf-8")
    (tmp_path / "qkeras.stderr.txt").write_text(run.stderr, encoding="utf-8")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "OPT3_QUANTIZED_CPP_PASS" in run.stdout
