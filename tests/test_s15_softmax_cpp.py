"""Schedule-only changes must preserve actual full Softmax C++ results."""

import os
import subprocess

from test_s15_numeric_policy import ROOT

PROBE = r"""
import hashlib,json,shutil,sys
from pathlib import Path
import hls4ml
import numpy as np
import tensorflow as tf
root,work=Path(sys.argv[1]),Path(sys.argv[2])
sys.path.insert(0,str(root/'hls4ml-convert/scripts'))
from generated_compat import configure_softmax_exp_reads
from local_precision_search import host_predict,compare_outputs
model=tf.keras.Sequential([tf.keras.layers.Input(shape=(8,),name='logits'),
    tf.keras.layers.Activation('softmax',name='probabilities')])
model.save(work/'original.h5',include_optimizer=False)
x=np.random.default_rng(2026102505).uniform(-1,1,(256,8)).astype(np.float32)
ref=model(x,training=False).numpy().tolist()
(work/'inputs.json').write_text(json.dumps(x.tolist()))
(work/'reference.json').write_text(json.dumps(ref))
config={'Model':{'Precision':'ap_fixed<24,8>','ReuseFactor':1,'Strategy':'Resource'},
    'LayerName':{'probabilities':{'Implementation':'stable','TableSize':32768,
        'exp_table_t':'ap_fixed<24,5>','inv_table_t':'ap_fixed<24,8>',
        'inv_inp_t':'ap_fixed<24,5>','Precision':{'result':'ap_fixed<24,8>'}}}}
graph=hls4ml.converters.convert_from_keras_model(model,hls_config=config,
    output_dir=str(work/'baseline/project'),part='xc7a200tfbg484-2',
    clock_period=10,io_type='io_parallel',backend='Vivado')
graph.write()
node=next(n for n in graph.get_layers() if n.name=='probabilities')
assert str(node.get_attr('exp_table_t').precision).startswith('fixed<24,5')
assert str(node.get_attr('inv_inp_t').precision).startswith('fixed<24,5')
(work/'effective-config.json').write_text(json.dumps(graph.config.config,default=str,indent=2))
outputs=host_predict(work/'baseline/project','myproject',x.tolist(),8,8,
                     Path('D:/mingw64/bin/g++.exe'))
results={'baseline':compare_outputs(ref,outputs)}
for factor in (1,2):
    variant=work/f'factor-{factor}/project'
    shutil.copytree(work/'baseline/project',variant)
    patch=configure_softmax_exp_reads(variant,hls4ml.__version__,factor)
    current=host_predict(variant,'myproject',x.tolist(),8,8,Path('D:/mingw64/bin/g++.exe'))
    assert current==outputs, factor
    comparison=compare_outputs(ref,current)
    results[str(factor)]={'patch':patch,'comparison':comparison,
        'bitwise_float_output_equal':current==outputs}
    assert comparison['passed'],comparison
assert results['baseline']['passed'],results
(work/'scope-summary.json').write_text(json.dumps({'results':results,
    'hardware_verified':False,'eda_executed':False},indent=2))
print('SOFTMAX_SCHEDULE_CPP_PASS')
"""


def test_actual_cpp_schedule_preserves_full_softmax(tmp_path):
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
    assert "SOFTMAX_SCHEDULE_CPP_PASS" in run.stdout
