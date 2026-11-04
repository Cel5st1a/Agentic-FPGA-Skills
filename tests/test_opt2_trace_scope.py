from test_s15_numeric_policy import load


def test_nested_stateful_and_custom_layers_keep_legacy_path():
    mod = load("hls4ml-convert/scripts/local_precision_search.py", "trace_scope")
    builtin = type("Dense", (), {"__module__": "keras.layers", "stateful": False})
    nested = builtin()
    nested.layers = [builtin()]
    assert mod.batch_trace_safe(nested) is True
    nested.layers[0].stateful = True
    assert mod.batch_trace_safe(nested) is False
    custom = type("Custom", (), {"__module__": "user_extension", "stateful": False})
    assert mod.batch_trace_safe(custom()) is False
    unknown_fn = builtin()
    unknown_fn.activation = lambda x: x
    assert mod.batch_trace_safe(unknown_fn) is False
