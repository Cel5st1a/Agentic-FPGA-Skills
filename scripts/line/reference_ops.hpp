#pragma once
#include "nnet_utils/nnet_pooling.h"
#include "nnet_utils/nnet_conv_stream.h"
#include <algorithm>
#include <cmath>
#include <fstream>
#include <iomanip>
#include <sstream>
#include <stdexcept>
#include <string>
#include <type_traits>
#include <vector>

// This evaluator uses the emitted scalar types and weights as data. All tensor
// traversal and arithmetic below are independent of the generated nnet kernels.
// Every operation re-implements the arithmetic of the corresponding hls4ml 1.3.0
// template (vivado/nnet_utils) directly, using the same emitted ap_fixed types and
// the same expression ordering, but never calls the generated kernels themselves.
template<class T, class = void> struct scalar { using type = T; };
template<class T> struct scalar<T, std::void_t<typename T::value_type>> {
    using type = typename T::value_type;
};
template<class T> using values = std::vector<typename scalar<T>::type>;

template<class C> struct reference_logical_config : C {
    static const unsigned io_type = nnet::io_parallel;
};

template<class A, class B, unsigned N>
values<B> reference_repack_stream(const values<A>& data) {
    if (data.size() != N) throw std::runtime_error("repack scalar count differs");
    return values<B>(data.begin(), data.end());
}

template<class A, class B, class C>
values<B> reference_zeropad1d_cl(const values<A>& data) {
    if (data.size() != C::in_width * C::n_chan ||
        C::out_width != C::in_width + C::pad_left + C::pad_right)
        throw std::runtime_error("padding geometry differs");
    values<B> result(C::out_width * C::n_chan, B(0));
    for (unsigned i = 0; i < data.size(); ++i)
        result[C::pad_left * C::n_chan + i] = data[i];
    return result;
}

// The activation lookup-table ranges are emitted by the project's (possibly
// patched) nnet_utils/nnet_activation.h; fixed_reference.py parses that header and
// passes the values here.  The defaults are the stock hls4ml 1.3.0 ones.
#ifndef REFERENCE_ELU_RANGE
#define REFERENCE_ELU_RANGE 8.0
#endif
#ifndef REFERENCE_ELU_DIVISOR
#define REFERENCE_ELU_DIVISOR 8
#endif
#ifndef REFERENCE_SOFTSIGN_RANGE
#define REFERENCE_SOFTSIGN_RANGE 8.0
#endif
#ifndef REFERENCE_SOFTSIGN_DIVISOR
#define REFERENCE_SOFTSIGN_DIVISOR 16
#endif
#ifndef REFERENCE_SOFTSIGN_OFFSET
#define REFERENCE_SOFTSIGN_OFFSET 8
#endif
#ifndef REFERENCE_SOFTSIGN_OFFSET_DIVISOR
#define REFERENCE_SOFTSIGN_OFFSET_DIVISOR 16
#endif
#ifndef REFERENCE_ELU_INTERP
#define REFERENCE_ELU_INTERP 0
#endif
#ifndef REFERENCE_ELU_CLAMP
#define REFERENCE_ELU_CLAMP 8.0
#endif
#ifndef REFERENCE_SOFTSIGN_INTERP
#define REFERENCE_SOFTSIGN_INTERP 0
#endif
#ifndef REFERENCE_SOFTSIGN_INTERP_DIVISOR
#define REFERENCE_SOFTSIGN_INTERP_DIVISOR 16
#endif
#ifndef REFERENCE_SOFTSIGN_OFFSET_VALUE
#define REFERENCE_SOFTSIGN_OFFSET_VALUE 8.0
#endif

// Mirrors nnet::load_weights_from_txt: the generated code parses each comma
// separated token with `std::istringstream(token) >> w[i]`, i.e. a decimal double
// converted to the emitted fixed-point type.  Reproducing that conversion (rather
// than parsing a float first) keeps the loaded weights bit-identical.
template<class T> std::vector<T> load(const std::string& file) {
    std::ifstream stream(file);
    if (!stream) throw std::runtime_error("missing reference weight file");
    std::vector<T> result;
    std::string token;
    while (std::getline(stream, token, ',')) {
        if (token.find_first_not_of(" \r\n\t") == std::string::npos) continue;
        T value{};
        std::istringstream(token) >> value;
        result.push_back(value);
    }
    return result;
}

// Mirrors the emitted Dense kernel set: latency (nnet_dense_latency.h, row-major
// [n_in, n_out] weights) and resource (nnet_dense_resource.h, block-interleaved
// weights as written by the converter for that strategy).
template<class A, class B, class C>
values<B> reference_dense(const values<A>& data, const std::vector<typename C::weight_t>& weights,
                          const std::vector<typename C::bias_t>& biases) {
    if (data.size() != C::n_in || weights.size() != C::n_in * C::n_out || biases.size() != C::n_out)
        throw std::runtime_error("Dense reference shape mismatch");
    values<B> result(C::n_out);
    if constexpr (C::strategy == nnet::resource && C::reuse_factor > C::n_in) {
        // Traverse the emitted output-major weights by reuse cycle. For RF>N_IN,
        // each cycle contributes at most one product to each output accumulator.
        // This covers both divisible and non-divisible high-RF resource kernels.
        std::vector<typename C::accum_t> acc(C::n_out);
        for (unsigned j = 0; j < C::n_out; ++j)
            acc[j] = static_cast<typename C::accum_t>(biases[j]);
        constexpr unsigned count = C::n_in * C::n_out;
        constexpr unsigned blocks = (count + C::reuse_factor - 1) / C::reuse_factor;
        for (unsigned cycle = 0; cycle < C::reuse_factor; ++cycle) {
            for (unsigned block = 0; block < blocks; ++block) {
                unsigned index = cycle + C::reuse_factor * block;
                if (index < count)
                    acc[index / C::n_in] += static_cast<typename C::accum_t>(
                        data[index % C::n_in] * weights[index]);
            }
        }
        for (unsigned j = 0; j < C::n_out; ++j)
            result[j] = static_cast<B>(acc[j]);
        return result;
    } else if constexpr (C::strategy == nnet::resource) {
        static_assert(C::reuse_factor <= C::n_in,
                      "independent Dense reference implements the emitted RF <= N_IN resource kernel");
        constexpr unsigned rufactor = C::reuse_factor;
        constexpr unsigned nin = C::n_in;
        constexpr unsigned nout = C::n_out;
        constexpr unsigned multfactor = nin < rufactor ? nin : rufactor;
        constexpr unsigned multiplier_limit = (nin * nout + multfactor - 1) / multfactor;
        constexpr unsigned block_factor = (nin * nout + rufactor - 1) / rufactor;
        constexpr unsigned multscale = multiplier_limit / nout;
        static_assert((multiplier_limit % nout == 0 || rufactor >= nin),
                      "emitted dense resource reuse factor is not permitted");
        static_assert(multiplier_limit == block_factor,
                      "emitted dense resource kernel requires multiplier_limit == block_factor");
        std::vector<typename C::accum_t> acc(nout);
        for (unsigned j = 0; j < nout; ++j)
            acc[j] = static_cast<typename C::accum_t>(biases[j]);
        for (unsigned ir = 0; ir < rufactor; ++ir) {
            unsigned w_index = ir, in_index = ir, out_index = 0, acc_step = 0;
            for (unsigned im = 0; im < block_factor; ++im) {
                acc[out_index] += static_cast<typename C::accum_t>(
                    data[in_index] * weights[w_index]);
                w_index += rufactor;
                in_index += rufactor;
                if (in_index >= nin) in_index = ir;
                if (acc_step + 1 >= multscale) {
                    acc_step = 0;
                    out_index++;
                } else {
                    acc_step++;
                }
            }
        }
        for (unsigned j = 0; j < nout; ++j) result[j] = static_cast<B>(acc[j]);
        return result;
    } else {
        static_assert(C::strategy == nnet::latency,
                      "independent Dense reference supports latency or resource strategy");
        std::vector<typename C::accum_t> mult(C::n_in * C::n_out);
        std::vector<typename C::accum_t> acc(C::n_out);
        for (unsigned ii = 0; ii < C::n_in; ++ii) {
            A cache = data[ii];
            for (unsigned jj = 0; jj < C::n_out; ++jj) {
                unsigned index = ii * C::n_out + jj;
                mult[index] = cache * weights[index];
            }
        }
        for (unsigned iacc = 0; iacc < C::n_out; ++iacc)
            acc[iacc] = static_cast<typename C::accum_t>(biases[iacc]);
        for (unsigned ii = 0; ii < C::n_in; ++ii)
            for (unsigned jj = 0; jj < C::n_out; ++jj)
                acc[jj] += mult[ii * C::n_out + jj];
        for (unsigned ires = 0; ires < C::n_out; ++ires)
            result[ires] = acc[ires];
        return result;
    }
}

template<class A, class B, class C>
values<B> reference_conv_2d_cl(const values<A>& data, const std::vector<typename C::weight_t>& weights,
                              const std::vector<typename C::bias_t>& biases) {
    static_assert(C::pad_top == 0 && C::pad_bottom == 0 && C::pad_left == 0 && C::pad_right == 0,
                  "independent Conv2D currently requires valid padding");
    const unsigned count = C::filt_height * C::filt_width * C::n_chan;
    if (data.size() != C::in_height * C::in_width * C::n_chan ||
        weights.size() != count * C::n_filt || biases.size() != C::n_filt)
        throw std::runtime_error("Conv2D reference shape mismatch");
    values<B> result(C::out_height * C::out_width * C::n_filt);
    for (unsigned y = 0; y < C::out_height; ++y)
        for (unsigned x = 0; x < C::out_width; ++x)
            for (unsigned f = 0; f < C::n_filt; ++f) {
                typename C::accum_t sum = biases[f];
                for (unsigned ky = 0; ky < C::filt_height; ++ky)
                    for (unsigned kx = 0; kx < C::filt_width; ++kx)
                        for (unsigned c = 0; c < C::n_chan; ++c) {
                            unsigned offset = ((y * C::stride_height + ky) * C::in_width
                                               + x * C::stride_width + kx) * C::n_chan + c;
                            unsigned kernel = (ky * C::filt_width + kx) * C::n_chan + c;
                            sum += static_cast<typename C::accum_t>(data[offset] * weights[f * count + kernel]);
                        }
                result[(y * C::out_width + x) * C::n_filt + f] = sum;
            }
    return result;
}

// Mirrors the emitted Conv1D kernel set: latency (nnet_conv1d_latency.h) and
// resource (nnet_conv1d_resource.h), each with the generated line-buffer im2col
// ordering (k * n_chan + c) and pixel/partition traversal.
template<class A, class B, class C>
values<B> reference_conv_1d_cl(const values<A>& data, const std::vector<typename C::weight_t>& weights,
                               const std::vector<typename C::bias_t>& biases) {
    static_assert(C::strategy == nnet::latency || C::strategy == nnet::resource,
                  "independent Conv1D reference requires latency or resource strategy");
    static_assert(C::implementation == nnet::conv_implementation::linebuffer,
                  "independent Conv1D reference requires the generated line-buffer im2col");
    constexpr unsigned mult_n_in = C::filt_width * C::n_chan;
    constexpr unsigned mult_n_out = C::n_filt;
    static_assert(C::n_partitions * C::n_pixels == C::out_width, "inconsistent Conv1D partition geometry");
    if (data.size() != C::in_width * C::n_chan || weights.size() != mult_n_in * mult_n_out
        || biases.size() != C::n_filt)
        throw std::runtime_error("Conv1D reference shape mismatch");

    values<B> result(C::out_width * mult_n_out);
    values<A> data_buf(C::n_pixels * mult_n_in);

    for (unsigned i_part = 0; i_part < C::n_partitions; ++i_part) {
        // Generated fill_buffer_<index>: im2col of the offset, zero padded window.
        for (unsigned i_pxl = 0; i_pxl < C::n_pixels; ++i_pxl) {
            int out_pos = static_cast<int>(i_part * C::n_pixels + i_pxl) * static_cast<int>(C::stride_width);
            for (unsigned k = 0; k < C::filt_width; ++k)
                for (unsigned c = 0; c < C::n_chan; ++c) {
                    int in_pos = out_pos - static_cast<int>(C::pad_left)
                                 + static_cast<int>(k * C::dilation);
                    data_buf[i_pxl * mult_n_in + k * C::n_chan + c] =
                        (in_pos >= 0 && in_pos < static_cast<int>(C::in_width))
                            ? data[static_cast<unsigned>(in_pos) * C::n_chan + c]
                            : static_cast<typename scalar<A>::type>(0);
                }
        }
        if constexpr (C::strategy == nnet::resource) {
            constexpr unsigned block_factor =
                (mult_n_in * mult_n_out + C::reuse_factor - 1) / C::reuse_factor;
            constexpr unsigned multscale = block_factor / mult_n_out;
            static_assert((block_factor % mult_n_out == 0 || C::reuse_factor >= mult_n_in),
                          "emitted conv resource reuse factor is not permitted");
            static_assert(C::reuse_factor <= mult_n_in,
                          "emitted conv resource kernel requires RF <= FILT_WIDTH * N_CHAN");
            std::vector<typename C::accum_t> acc(C::n_pixels * mult_n_out);
            for (unsigned i_pxl = 0; i_pxl < C::n_pixels; ++i_pxl)
                for (unsigned j = 0; j < mult_n_out; ++j)
                    acc[i_pxl * mult_n_out + j] = static_cast<typename C::accum_t>(biases[j]);
            for (unsigned i_rf = 0; i_rf < C::reuse_factor; ++i_rf) {
                unsigned i_w = i_rf, i_in = i_rf, i_out = 0, i_acc = 0;
                for (unsigned i_blk = 0; i_blk < block_factor; ++i_blk) {
                    for (unsigned i_pxl = 0; i_pxl < C::n_pixels; ++i_pxl) {
                        acc[i_pxl * mult_n_out + i_out] += static_cast<typename C::accum_t>(
                            data_buf[i_pxl * mult_n_in + i_in] * weights[i_w]);
                    }
                    i_w += C::reuse_factor;
                    i_in += C::reuse_factor;
                    if (i_in >= mult_n_in) i_in = i_rf;
                    if (i_acc + 1 >= multscale) {
                        i_acc = 0;
                        i_out++;
                    } else {
                        i_acc++;
                    }
                }
            }
            for (unsigned i_pxl = 0; i_pxl < C::n_pixels; ++i_pxl)
                for (unsigned i_res = 0; i_res < mult_n_out; ++i_res)
                    result[i_part * C::n_pixels * mult_n_out + i_pxl * mult_n_out + i_res] =
                        static_cast<B>(acc[i_pxl * mult_n_out + i_res]);
        } else {
            static_assert(C::strategy == nnet::latency,
                          "independent Conv1D reference supports latency or resource strategy");
            std::vector<typename C::accum_t> mult(mult_n_in * mult_n_out);
            std::vector<typename C::accum_t> acc(mult_n_out);
            for (unsigned i_pxl = 0; i_pxl < C::n_pixels; ++i_pxl) {
                for (unsigned i_in = 0; i_in < mult_n_in; ++i_in) {
                    A cache = data_buf[i_pxl * mult_n_in + i_in];
                    for (unsigned i_out = 0; i_out < mult_n_out; ++i_out)
                        mult[i_in * mult_n_out + i_out] = cache * weights[i_in * mult_n_out + i_out];
                }
                for (unsigned i_acc = 0; i_acc < mult_n_out; ++i_acc)
                    acc[i_acc] = static_cast<typename C::accum_t>(biases[i_acc]);
                for (unsigned i_in = 0; i_in < mult_n_in; ++i_in)
                    for (unsigned i_out = 0; i_out < mult_n_out; ++i_out)
                        acc[i_out] += mult[i_in * mult_n_out + i_out];
                for (unsigned i_res = 0; i_res < mult_n_out; ++i_res)
                    result[i_part * C::n_pixels * mult_n_out + i_pxl * mult_n_out + i_res] =
                        static_cast<B>(acc[i_res]);
            }
        }
    }
    return result;
}

// Mirrors nnet::normalize (nnet_batchnorm.h) for io_parallel: the emitted
// per-filter scale/bias applied cyclically, using the same product-then-add
// expression sequence as the template.
template<class A, class B, class C>
values<B> reference_normalize(const values<A>& data, const std::vector<typename C::scale_t>& scale,
                              const std::vector<typename C::bias_t>& bias) {
    static_assert(C::io_type == nnet::io_parallel, "independent BatchNorm reference requires io_parallel");
    if (data.size() != C::n_in || scale.size() != C::n_scale_bias || bias.size() != C::n_scale_bias)
        throw std::runtime_error("BatchNorm reference shape mismatch");
    values<B> result(C::n_in);
    for (unsigned ires = 0; ires < C::n_in; ++ires) {
        if constexpr (C::n_filt == static_cast<unsigned>(-1)) {
            result[ires] = data[ires] * scale[ires] + bias[ires];
        } else {
            unsigned norm_index = ires % C::n_filt;
            result[ires] = data[ires] * scale[norm_index] + bias[norm_index];
        }
    }
    return result;
}

// Mirrors nnet::elu (nnet_activation.h).  The emitted table is generated with the
// same float expression as init_elu_table, then the template's own index
// expression and alpha multiply are reproduced on the emitted types.
inline float reference_elu_fcn_float(float input) { return std::exp(input) - 1.; }

template<class C> std::vector<typename C::table_t> build_elu_table() {
    std::vector<typename C::table_t> table(C::table_size);
    for (unsigned i = 0; i < C::table_size; ++i) {
        float in_val = -REFERENCE_ELU_RANGE * i / float(C::table_size);
        table[i] = reference_elu_fcn_float(in_val);
    }
    return table;
}

template<class A, class P, class B, class C>
values<B> reference_elu(const values<A>& data, const P alpha) {
    static_assert(C::io_type == nnet::io_parallel, "independent ELU reference requires io_parallel");
    static const std::vector<typename C::table_t> elu_table = build_elu_table<C>();
    if (data.size() != C::n_in)
        throw std::runtime_error("ELU reference shape mismatch");
    values<B> result(C::n_in);
#if REFERENCE_ELU_INTERP
    // Mirrors the interpolated generated body: sampled table + one linear step,
    // with the magnitude clamped to the table range before scaling.
    typedef ap_fixed<32, 16> elu_work_t;
    for (unsigned i = 0; i < C::n_in; ++i) {
        typename scalar<A>::type datareg = data[i];
        if (datareg >= 0) {
            result[i] = datareg;
        } else {
            elu_work_t mag = (elu_work_t)(-datareg);
            if (mag > (elu_work_t)REFERENCE_ELU_CLAMP) mag = (elu_work_t)REFERENCE_ELU_CLAMP;
            elu_work_t scaled = mag * (C::table_size / REFERENCE_ELU_DIVISOR);
            int index = (int)scaled;
            elu_work_t frac = scaled - (elu_work_t)index;
            if (index < 0) {
                index = 0;
                frac = 0;
            }
            if (index > (int)C::table_size - 2) {
                index = (int)C::table_size - 2;
                frac = 1;
            }
            typename C::table_t y0 = elu_table[index];
            typename C::table_t y1 = elu_table[index + 1];
            typename C::table_t dy = (typename C::table_t)(y1 - y0);
            typename C::table_t y =
                (typename C::table_t)(y0 + (typename C::table_t)(dy * frac));
            result[i] = alpha * y;
        }
    }
#else
    for (unsigned i = 0; i < C::n_in; ++i) {
        typename scalar<A>::type datareg = data[i];
        if (datareg >= 0) {
            result[i] = datareg;
        } else {
            int index = datareg * C::table_size / -REFERENCE_ELU_DIVISOR;
            if (index > static_cast<int>(C::table_size) - 1)
                index = static_cast<int>(C::table_size) - 1;
            result[i] = alpha * elu_table[index];
        }
    }
#endif
    return result;
}

// Mirrors nnet::pooling1d_cl (nnet_pooling.h) for the emitted valid-padded Max
// configuration, including the template's own padding sentinel and window order.
template<class A, class B, class C>
values<B> reference_pooling1d_cl(const values<A>& data) {
    static_assert(C::pool_op == nnet::Max, "independent Pooling1D reference requires Max pooling");
    constexpr int full_padded_width = C::n_in + C::pad_left + C::pad_right;
    constexpr int restricted_padded_width =
        (full_padded_width - C::pool_width) / C::stride_width * C::stride_width + 1;
    if (data.size() != C::n_in * C::n_filt)
        throw std::runtime_error("Pooling1D reference shape mismatch");
    values<B> result(C::n_out * C::n_filt);
    for (unsigned ff = 0; ff < C::n_filt; ++ff) {
        for (int ii = 0; ii < restricted_padded_width; ii += static_cast<int>(C::stride_width)) {
            typename scalar<A>::type pool[C::pool_width];
            for (unsigned jj = 0; jj < C::pool_width; ++jj) {
                if (ii + static_cast<int>(jj) >= static_cast<int>(C::pad_left)
                    && ii + static_cast<int>(jj) < static_cast<int>(C::n_in) + static_cast<int>(C::pad_left)) {
                    pool[jj] = data[static_cast<unsigned>(ii + static_cast<int>(jj) - static_cast<int>(C::pad_left))
                                        * C::n_filt + ff];
                } else {
                    // nnet::pad_val<T, Max>: most negative value of the emitted type.
                    typename scalar<A>::type pad = static_cast<typename scalar<A>::type>(0);
                    pad[pad.width - 1] = 1;
                    pool[jj] = pad;
                }
            }
            // nnet::pool_op<..., Max, accum_t> / nnet::max: T y = x[0]; compare; then
            // the accum_t return conversion and the res_T assignment.
            typename scalar<A>::type best = pool[0];
            for (unsigned jj = 1; jj < C::pool_width; ++jj)
                best = pool[jj] > best ? pool[jj] : best;
            typename C::accum_t pooled = best;
            result[(ii / static_cast<int>(C::stride_width)) * C::n_filt + ff] = pooled;
        }
    }
    return result;
}

template<class A, class B, class C> values<B> reference_relu(const values<A>& data) {
    values<B> result;
    for (const auto& value : data) result.emplace_back(value > 0 ? value : typename scalar<A>::type(0));
    return result;
}

template<class A, class B, class C> values<B> reference_linear(const values<A>& data) {
    return values<B>(data.begin(), data.end());
}

template<class A, class B, class C> values<B> reference_pooling2d_cl(const values<A>& data) {
    static_assert(C::pool_op == nnet::Max && C::pad_top == 0 && C::pad_bottom == 0 &&
                  C::pad_left == 0 && C::pad_right == 0, "reference requires valid max pooling");
    if (data.size() != C::in_height * C::in_width * C::n_filt)
        throw std::runtime_error("Pooling reference shape mismatch");
    values<B> result;
    for (unsigned y = 0; y < C::out_height; ++y)
        for (unsigned x = 0; x < C::out_width; ++x)
            for (unsigned c = 0; c < C::n_filt; ++c) {
                auto best = data[((y * C::stride_height) * C::in_width + x * C::stride_width) * C::n_filt + c];
                for (unsigned ky = 0; ky < C::pool_height; ++ky)
                    for (unsigned kx = 0; kx < C::pool_width; ++kx) {
                        auto value = data[((y * C::stride_height + ky) * C::in_width + x * C::stride_width + kx) * C::n_filt + c];
                        if (value > best) best = value;
                    }
                result.emplace_back(best);
            }
    return result;
}

// Mirrors nnet::softsign (nnet_activation.h), including the float table
// generation and the template's integer rounding index expression.
inline float reference_softsign_fcn_float(float input) { return input / (std::abs(input) + 1.); }

template<class C> std::vector<typename C::table_t> build_softsign_table() {
    std::vector<typename C::table_t> table(C::table_size);
    for (unsigned i = 0; i < C::table_size; ++i) {
        float in_val = 2 * REFERENCE_SOFTSIGN_RANGE * (i - float(C::table_size) / 2.0)
                       / float(C::table_size);
        table[i] = reference_softsign_fcn_float(in_val);
    }
    return table;
}

template<class A, class B, class C>
values<B> reference_softsign(const values<A>& data) {
    static_assert(C::io_type == nnet::io_parallel, "independent softsign reference requires io_parallel");
    static const std::vector<typename C::table_t> softsign_table = build_softsign_table<C>();
    if (data.size() != C::n_in)
        throw std::runtime_error("softsign reference shape mismatch");
    values<B> result(C::n_in);
#if REFERENCE_SOFTSIGN_INTERP
    // Mirrors the interpolated generated body: sampled table + one linear step.
    typedef ap_fixed<32, 16> softsign_work_t;
    for (unsigned i = 0; i < C::n_in; ++i) {
        softsign_work_t offset = (softsign_work_t)data[i] + (softsign_work_t)REFERENCE_SOFTSIGN_OFFSET_VALUE;
        softsign_work_t scaled = offset * (C::table_size / REFERENCE_SOFTSIGN_INTERP_DIVISOR);
        int index = (int)scaled;
        softsign_work_t frac = scaled - (softsign_work_t)index;
        if (index < 0) {
            index = 0;
            frac = 0;
        }
        if (index > (int)C::table_size - 2) {
            index = (int)C::table_size - 2;
            frac = 1;
        }
        typename C::table_t y0 = softsign_table[index];
        typename C::table_t y1 = softsign_table[index + 1];
        typename C::table_t dy = (typename C::table_t)(y1 - y0);
        result[i] = static_cast<B>(y0 + (typename C::table_t)(dy * frac));
    }
#else
    for (unsigned i = 0; i < C::n_in; ++i) {
        int data_round = data[i] * C::table_size / REFERENCE_SOFTSIGN_DIVISOR;
        int index = data_round + REFERENCE_SOFTSIGN_OFFSET * C::table_size / REFERENCE_SOFTSIGN_OFFSET_DIVISOR;
        if (index < 0)
            index = 0;
        if (index > static_cast<int>(C::table_size) - 1)
            index = static_cast<int>(C::table_size) - 1;
        result[i] = static_cast<B>(softsign_table[index]);
    }
#endif
    return result;
}

template<class T> unsigned address(T value, unsigned size) {
    unsigned bits = 0;
    while ((1u << bits) < size) ++bits;
    if ((1u << bits) != size || bits > T::width) throw std::runtime_error("invalid reference table");
    return value.range(T::width - 1, T::width - bits).to_uint();
}

template<class T> float address_value(unsigned index, unsigned size) {
    unsigned bits = 0;
    while ((1u << bits) < size) ++bits;
    T value = 0;
    value.range(T::width - 1, T::width - bits) = index;
    return value.to_float();
}

template<class A, class B, class C> values<B> reference_softmax(const values<A>& data) {
    static_assert(C::implementation == nnet::softmax_implementation::stable,
                  "independent reference requires stable Softmax");
    auto maximum = *std::max_element(data.begin(), data.end());
    std::vector<typename C::accum_t> exponentials;
    typename C::accum_t sum = 0;
    for (const auto& value : data) {
        typename C::inp_norm_t delta = maximum - value;
        unsigned index = address(delta, C::exp_table_size);
        float grid = address_value<typename C::inp_norm_t>(index, C::exp_table_size) * C::exp_scale;
        typename C::exp_table_t exponential = std::exp(-grid);
        exponentials.emplace_back(exponential);
        sum += exponentials.back();
    }
    typename C::inv_inp_t denominator = sum;
    unsigned index = address(denominator, C::inv_table_size);
    float grid = address_value<typename C::inv_inp_t>(index, C::inv_table_size);
    typename C::inv_table_t inverse = 1 / grid;
    values<B> result;
    for (const auto& value : exponentials) result.emplace_back(value * inverse);
    return result;
}
