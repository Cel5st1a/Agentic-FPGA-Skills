from test_s15_numeric_policy import load


def test_generated_softmax_table_diagnostic_resolves_aliases_and_never_selects_a_winner():
    mod = load("log-analyzer/scripts/analyze_efficiency.py", "opt3_pressure")
    params = """struct softmax_cfg : nnet::activ_config {
      static const unsigned n_in = 7;
      static const unsigned exp_table_size = 4096;
      static const unsigned inv_table_size = 65536;
      typedef exponent_t exp_table_t;
      typedef reciprocal_t inv_table_t;
    };"""
    defines = "typedef ap_fixed<21,5> exponent_t; typedef ap_ufixed<19,3> reciprocal_t;"
    report = mod.nonlinear_pressure(params, defines)
    assert report[0]["tables"]["exp"]["storage_bits_single_copy"] == 4096 * 21
    assert report[0]["tables"]["inv"]["storage_bits_single_copy"] == 65536 * 19
    assert report[0]["read_parallelism"] is None
    assert report[0]["measured_resources"] is None
    assert report[0]["recommended_table_size"] is None


def test_unknown_generated_types_never_fall_back_to_contract_precision():
    mod = load("log-analyzer/scripts/analyze_efficiency.py", "opt3_pressure_unknown")
    row = mod.nonlinear_pressure(
        """struct sigmoid_cfg : nnet::activ_config {
      static const unsigned n_in = 3;
      static const unsigned table_size = 1024;
      typedef unknown_t table_t;
    };""",
        "",
    )[0]
    assert row["tables"]["table"]["storage_bits_single_copy"] is None
    assert row["tables"]["table"]["width_bits"] is None
    assert row["recommended_table_size"] is None


def test_distinct_aliases_with_same_underlying_type_are_both_resolved():
    mod = load("log-analyzer/scripts/analyze_efficiency.py", "opt3_aliases")
    rows = mod.nonlinear_pressure(
        """struct softmax_config : base {
      static const unsigned exp_table_size = 512;
      static const unsigned inv_table_size = 2048;
      typedef exp_t exp_table_t; typedef inv_t inv_table_t;
    };""",
        "typedef ap_fixed<19,4> exp_t; typedef ap_fixed<19,4> inv_t;",
    )
    assert rows[0]["tables"]["exp"]["width_bits"] == 19
    assert rows[0]["tables"]["inv"]["width_bits"] == 19


def test_stream_geometry_exposes_sample_beats_without_assuming_a_model_or_grid():
    mod = load("log-analyzer/scripts/analyze_efficiency.py", "opt3_geometry_pressure")
    wrapper = {
        "interface": {
            "style": "axis_stream_with_ap_ctrl_hs",
            "stream": {
                "input_elements": 120,
                "output_elements": 35,
                "input_lanes_per_beat": 3,
                "output_lanes_per_beat": 5,
                "input_beats_per_sample": 40,
                "output_beats_per_sample": 7,
                "tdata_bits": 72,
                "output_tdata_bits": 120,
            },
        }
    }
    result = mod.interface_pressure(wrapper)
    assert result["input_beats"] == 40 and result["output_beats"] == 7
    assert result["input_port_bits"] == 72
    assert result["measured_resources"] is None
    assert result["capacity_decision"] is None
    assert mod.interface_pressure({})["input_beats"] is None


def test_source_unroll_bound_requires_unique_exact_template_and_declared_slice():
    mod = load("log-analyzer/scripts/analyze_efficiency.py", "opt3_unroll")
    params = """struct softmax_config : base {
      static const auto implementation = nnet::softmax_implementation::stable;
      static const unsigned n_slice = 7;
      static const unsigned exp_table_size = 512;
      typedef ap_fixed<20,3> exp_table_t;
    };"""
    header = """for (unsigned i = 0; i < CONFIG_T::n_slice; i++) {
#pragma HLS unroll factor=2
unsigned x = softmax_idx_from_real_val<typename CONFIG_T::inp_norm_t, CONFIG_T::exp_table_size>"""
    assert mod.nonlinear_pressure(params, "", header)[0]["read_parallelism"] == 2
    assert mod.nonlinear_pressure(params, "", header + header)[0]["read_parallelism"] is None
    assert (
        mod.nonlinear_pressure(params, "", header.replace("factor=2", ""))[0]["read_parallelism"]
        == 7
    )


def test_unused_stable_template_cannot_describe_latency_implementation():
    mod = load("log-analyzer/scripts/analyze_efficiency.py", "opt3_latency_unroll")
    params = """struct softmax_config : base {
      static const auto implementation = nnet::softmax_implementation::latency;
      static const unsigned n_slice = 4;
      static const unsigned exp_table_size = 32;
      typedef ap_fixed<20,3> exp_table_t;
    };"""
    header = """for (unsigned i = 0; i < CONFIG_T::n_slice; i++) {
#pragma HLS unroll factor=2
unsigned x = softmax_idx_from_real_val<typename CONFIG_T::inp_norm_t, CONFIG_T::exp_table_size>"""
    assert mod.nonlinear_pressure(params, "", header)[0]["read_parallelism"] is None
