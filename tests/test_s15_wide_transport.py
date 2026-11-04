"""Complete 512-value RTL transport regression, not a board-model experiment."""

import sys

from test_s15_numeric_policy import ROOT, load

sys.path.insert(0, str(ROOT / "board-host-io/tests"))
from geometry_fixtures import synthetic_geometry  # noqa: E402


def test_full_512_values_cross_rtl_and_python_without_truncation(tmp_path):
    geo = load("board-host-io/scripts/host_geometry.py", "host_geometry")
    protocol = load("board-host-io/scripts/protocol.py", "wide_protocol")
    runner = load("board-host-io/scripts/run_rtl_tb.py", "wide_rtl_runner")
    grid = synthetic_geometry(
        geo,
        {"precision": "ap_fixed<20,6>", "model": {"input_shape": [1, 64]}},
        references=[[0.0] * 512],
    )
    assert grid.response_length_field == 1284
    inputs = [((i * 13) % 47 - 23) / 4.0 for i in range(64)]
    request = protocol.encode_request(inputs, 5, grid)
    packed = protocol.pack_payload(protocol.fixed_words(inputs, grid), grid)
    (tmp_path / "stimulus_bytes.hex").write_text("".join(f"{b:02x}\n" for b in request))
    (tmp_path / "stimulus_gaps.hex").write_text("4\n" + "2\n" * (len(request) - 1))
    (tmp_path / "expected_input.hex").write_text(packed[::-1].hex() + "\n")
    original = (ROOT / "board-host-io/tests/transport_geometry_tb.v").read_text()
    bench = original.replace("RESPONSE_BYTES = 10", "RESPONSE_BYTES = 1280")
    bench = bench.replace("OUTPUT_BITS = 80", "OUTPUT_BITS = 10240")
    packed_output = sum((((i - 256) * 1024) & ((1 << 20) - 1)) << (20 * i) for i in range(512))
    bench = bench.replace(
        "input_words[OUTPUT_BITS-1:0] ^ {OUTPUT_BITS{1'b1}}", f"10240'h{packed_output:02560x}"
    )
    bench = bench.replace("index < 2000 &&", "index < 20000 &&")
    tb = tmp_path / "wide_transport_tb.v"
    tb.write_text(bench)
    result = runner.run_testbench(
        "transport_geometry_tb",
        [ROOT / "board-host-io/assets/transport.v", tb],
        tmp_path,
        data_files=[
            tmp_path / "stimulus_bytes.hex",
            tmp_path / "stimulus_gaps.hex",
            tmp_path / "expected_input.hex",
        ],
    )
    assert result["returncode"] == 0, result["stdout"]
    frames = (tmp_path / "response_frames.hex").read_text().splitlines()
    assert len(frames) == 1
    frame = bytes.fromhex(frames[0])
    assert len(frame) == 1292
    assert int.from_bytes(frame[4:6], "little") == 1284
    _, outputs = protocol.decode_response(frame, 5, grid)
    expected = [(i - 256) / 16.0 for i in range(512)]
    assert outputs == expected
