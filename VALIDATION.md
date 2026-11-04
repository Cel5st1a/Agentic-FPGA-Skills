# S15-opt4 validation

Development environment Windows, Python 3.13, g++ (MinGW). Exact hls4ml 1.3.0 Vitis pooling template used for hash and idempotence checks.

17 targeted regressions pass, including compiled C++ explicit pooling vs independent window maximum at 2x2x1, 5x7x3, 11x11x16, 28x28x8, 32x32x4, three frames each, negative values and fallback for average pooling. Count validation covers 1/100/256/257 and invalid count types.

Ruff syntax and undefined-local checks pass across all skills. Historical strict formatting/import warnings are retained. Full suite uses existing private development parser fixtures locally; those fixtures are excluded from this repository.

Historical hardware evidence is from opt3 run-local repairs, not a formal opt4 board retest. opt4 source enables a structural dispatcher and local frame counters and needs future board regression to measure deployment benefit. No speedup claim is made.

Full inherited suite passed 226 tests in 185.61 seconds with development fixtures. Exact-template targeted suite passed 17 tests. The full suite completed before adding the final exact-template test.

Additional integration check generated an actual odd 5x7x3 Keras MaxPooling model with hls4ml 1.3.0/Vitis, applied the pinned repair, compiled the generated top and ap_fixed types with MinGW g++, and checked three consecutive frames exactly against independent window maxima. The upstream POSIX build_lib.sh entry failed under Windows cmd; the direct generated C++ top compilation passed. No HLS synthesis, RTL run or hardware retest was performed for opt4.
