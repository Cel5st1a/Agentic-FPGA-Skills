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
