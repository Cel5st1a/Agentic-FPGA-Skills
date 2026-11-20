"""Exercise the deployed pooling arithmetic and fallback across independent geometries."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "hls4ml-convert/scripts"))
from line.contract import original_model_criterion
from pooling_compat import patch_stream_pooling


@pytest.mark.parametrize("count", [1, 100, 256, 257])
def test_original_criterion_uses_declared_count(count):
    criterion = original_model_criterion({"verification": {"samples": count}})
    assert criterion.samples == count
    assert criterion.atol == criterion.rtol == .001
    assert criterion.samples_source == "contract.verification.samples"


@pytest.mark.parametrize("count", [0, -1, True, 100.5, "100"])
def test_bad_sample_count_is_rejected(count):
    with pytest.raises(ValueError):
        original_model_criterion({"verification": {"samples": count}})


def test_default_count_preserved():
    assert original_model_criterion().samples == 256


def test_pool_patch_hash_refusal_is_atomic(tmp_path):
    target = tmp_path / "firmware/nnet_utils/nnet_pooling_stream.h"
    target.parent.mkdir(parents=True)
    target.write_text("unknown")
    with pytest.raises(ValueError):
        patch_stream_pooling(tmp_path, "1.3.0", "Vitis")
    assert target.read_text() == "unknown"


@pytest.mark.parametrize("height,width,channels", [(2, 2, 1), (5, 7, 3), (11, 11, 16),
                                                  (28, 28, 8), (32, 32, 4)])
def test_cpp_pooling_matches_window_max_and_resets_frames(tmp_path, height, width, channels):
    compiler = shutil.which("g++")
    assert compiler, "C++ regression compiler required"
    asset = (ROOT / "hls4ml-convert/assets/pooling2d_explicit.h").read_text()
    repair = asset[asset.index("template <class data_T", asset.index("pooling2d_cl_stock") + 1):]
    # Omit the stock body, use an observable fallback to verify selection.
    source = r"""
#include <array>
#include <queue>
#include <cassert>
namespace hls { template<class T> struct stream {
    std::queue<T> q; void write(T x) {q.push(x);}
    T read(){assert(!q.empty()); T x=q.front();q.pop();return x;}
}; }
enum {Max, Average};
enum class conv_implementation {linebuffer};
#define PRAGMA_DATA_PACK(x)
template<int C> struct Pack {
    using value_type=int; static const int size=C; std::array<int,C> x;
    int& operator[](int i){return x[i];}
    const int& operator[](int i)const{return x[i];}
};
bool fallback=false;
template<class D,class R,class C> void pooling2d_cl_stock(hls::stream<D>&,hls::stream<R>&){
    fallback=true;
}
""" + repair + f"""
struct Config {{
 static const unsigned in_height={height}, in_width={width}, n_filt={channels};
 static const unsigned pool_height=2,pool_width=2,stride_height=2,stride_width=2;
 static const unsigned pad_top=0,pad_bottom=0,pad_left=0,pad_right=0;
 static const int pool_op=Max;
 static constexpr conv_implementation implementation=conv_implementation::linebuffer;
}};
struct Unsupported:Config {{ static const int pool_op=Average; }};
int main(){{
 for(int frame=0;frame<3;frame++){{
  hls::stream<Pack<{channels}>> in,out;
  int data[{height}][{width}][{channels}];
  for(int y=0;y<{height};y++)for(int x=0;x<{width};x++){{
   Pack<{channels}> p;
   for(int c=0;c<{channels};c++)p[c]=data[y][x][c]=((y*71+x*13+c*5+frame*37)%193)-96;
   in.write(p);
  }}
  pooling2d_cl<Pack<{channels}>,Pack<{channels}>,Config>(in,out);
  assert(in.q.empty());
  for(int y=0;y+1<{height};y+=2)for(int x=0;x+1<{width};x+=2){{
   auto p=out.read();
   for(int c=0;c<{channels};c++){{
    int expected=data[y][x][c];
    for(int dy=0;dy<2;dy++)for(int dx=0;dx<2;dx++)
     if(data[y+dy][x+dx][c]>expected)expected=data[y+dy][x+dx][c];
    assert(p[c]==expected);
   }}
  }}
  assert(out.q.empty());
  pooling2d_cl<Pack<{channels}>,Pack<{channels}>,Unsupported>(in,out);
  assert(fallback);
 }}
}}
"""
    cpp = tmp_path / "pool.cpp"
    exe = tmp_path / "pool.exe"
    cpp.write_text(source)
    subprocess.run([compiler, "-std=c++14", str(cpp), "-o", str(exe)],
                   check=True, capture_output=True)
    subprocess.run([str(exe)], check=True, capture_output=True)


def test_patch_stock_template_idempotent(tmp_path):
    import os

    source = os.environ.get("S15_POOLING_TEMPLATE")
    if source is None:
        pytest.skip("installed hls4ml template fixture not supplied")
    header = Path(source)
    target = tmp_path / "firmware/nnet_utils/nnet_pooling_stream.h"
    target.parent.mkdir(parents=True)
    target.write_bytes(header.read_bytes())
    first = patch_stream_pooling(tmp_path, "1.3.0", "Vitis")
    assert first["status"] == "APPLIED"
    raw = target.read_bytes()
    assert patch_stream_pooling(tmp_path, "1.3.0", "Vitis")["status"] == "ALREADY_APPLIED"
    assert target.read_bytes() == raw

def test_keras_converter_records_the_first_pooling_patch_outcome():
    import ast

    converter = ROOT / "hls4ml-convert/scripts/convert_keras.py"
    module = ast.parse(converter.read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "patch_stream_pooling"
    ]
    assert len(calls) == 1, "a second patch call hides the APPLIED record and before hash"

def test_vitis_generated_script_already_sources_options(tmp_path):
    import importlib.util

    source = ROOT / "vitis-build-agent/scripts/run_stage.py"
    spec = importlib.util.spec_from_file_location("vitis_generated_options", source)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    script = tmp_path / "build_prj.tcl"
    script_text = (
        "set tcldir [file dirname [info script]]\n"
        "source [file join $tcldir project.tcl]\n"
        "source [file join $tcldir build_opt.tcl]\n"
    )
    script.write_text(script_text, encoding="utf-8")
    runner.write_options(tmp_path, runner.STAGE_FLAGS["csynth"])
    evidence = runner.bind_stage_options(tmp_path)
    assert evidence["status"] == "BOUND"
    assert script.read_text(encoding="utf-8") == script_text

def test_sequential_dense_repair_only_removes_top_pipeline(tmp_path):
    from generated_compat import sequential_dense_top

    firmware = tmp_path / "firmware"
    firmware.mkdir()
    target = firmware / "myproject.cpp"
    raw = (
        b'void myproject() {\r\n    #pragma HLS PIPELINE\r\n    dense_call();\r\n}\r\n'
        b'void layer_helper() {\r\n    #pragma HLS PIPELINE\r\n    mac();\r\n}\r\n'
    )
    target.write_bytes(raw)
    info = sequential_dense_top(tmp_path, "1.3.0", "Vitis", "io_parallel", "myproject")
    assert info["status"] == "APPLIED"
    assert target.read_bytes() == raw.replace(b"    #pragma HLS PIPELINE\r\n", b"", 1)
    assert info["before_sha256"] != info["after_sha256"]
    assert info["hardware_verified"] is False
