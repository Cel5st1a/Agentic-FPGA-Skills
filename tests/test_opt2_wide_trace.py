import os
import subprocess

from test_s15_numeric_policy import ROOT

PROBE = r"""
import sys, json
from pathlib import Path
import numpy as np
import tensorflow as tf
sys.path.insert(0, str(Path(sys.argv[1])/'hls4ml-convert/scripts'))
import local_precision_search as mod
tf.keras.utils.set_random_seed(26)
observations=[]
for family in ['k1','dense100','keras3','conv2d','branched']:
    shape=(8,8,1) if family=='conv2d' else (16,)
    inputs=tf.keras.Input(shape=shape)
    x=inputs
    if family=='conv2d':
        x=tf.keras.layers.Conv2D(2,3,padding='same')(x)
        x=tf.keras.layers.Flatten()(x)
    elif family=='branched':
        x=tf.keras.layers.Concatenate()([tf.keras.layers.Dense(3)(x),tf.keras.layers.Dense(4)(x)])
    else:
        for width in ([100]*5 if family=='dense100' else [8,4]):
            x=tf.keras.layers.Dense(width)(x)
            x=tf.keras.layers.Activation('relu')(x)
    x=tf.keras.layers.Dense(1 if family=='k1' else 5)(x)
    out=tf.keras.layers.Activation('sigmoid' if family=='k1' else 'softmax')(x)
    model=tf.keras.Model(inputs,out)
    samples=np.random.default_rng(26).uniform(0,1,(4,*shape)).astype('float32')
    expected={}
    for layer in model.layers:
        activation=getattr(layer,'activation',None)
        if activation is not None and activation != tf.keras.activations.linear:
            if not isinstance(layer,tf.keras.layers.Activation): continue
        if isinstance(layer.output,(list,tuple)): continue
        values=tf.keras.Model(model.inputs,layer.output)(samples,training=False).numpy()
        expected[layer.name]=values.reshape(len(samples),-1).tolist()
    factory=tf.keras.Model
    calls=[]
    class Observer:
        def __init__(self,*a,**kw): self.model=factory(*a,**kw)
        def __call__(self,*a,**kw): calls.append(1); return self.model(*a,**kw)
    tf.keras.Model=Observer
    try: actual=mod.layer_references(model,samples)
    finally: tf.keras.Model=factory
    assert actual.keys()==expected.keys()
    for key in expected: np.testing.assert_allclose(actual[key],expected[key],atol=1e-6,rtol=1e-6)
    assert len(calls)==1,(family,len(calls),len(expected))
    observations.append({'family':family,'old_forward_calls':len(expected),'new_forward_calls':len(calls)})
print('TRACE_RESULTS '+json.dumps(observations))
"""


def test_actual_keras_multioutput_trace_preserves_cpu_values_and_reduces_forward_calls(tmp_path):
    run = subprocess.run(
        ["D:/python_envs/hls4ml_conv/Scripts/python.exe", "-B", "-c", PROBE, str(ROOT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=dict(os.environ, TF_CPP_MIN_LOG_LEVEL="3"),
    )
    (tmp_path / "trace.stdout.txt").write_text(run.stdout, encoding="utf-8")
    (tmp_path / "trace.stderr.txt").write_text(run.stderr, encoding="utf-8")
    assert run.returncode == 0, run.stdout + run.stderr
    assert "TRACE_RESULTS" in run.stdout
