template <class data_T, class res_T, typename CONFIG_T>
void pooling2d_cl_stock(hls::stream<data_T> &data, hls::stream<res_T> &res) {
    assert(CONFIG_T::implementation == conv_implementation::linebuffer &&
           "Only \"linebuffer\" implementation is supported in Vitis HLS.");

    #pragma HLS INLINE recursive
    assert(CONFIG_T::pad_top == 0 && CONFIG_T::pad_bottom == 0 && CONFIG_T::pad_left == 0 && CONFIG_T::pad_right == 0);
    assert(CONFIG_T::pool_height == CONFIG_T::stride_height && CONFIG_T::pool_width == CONFIG_T::stride_width);

    static ap_shift_reg<typename data_T::value_type, CONFIG_T::in_width> line_buffer[MAX(CONFIG_T::pool_height - 1, 1)]
                                                                                    [CONFIG_T::n_filt];
    #pragma HLS ARRAY_PARTITION variable = line_buffer complete dim = 2

ReadInputHeight:
    for (unsigned i_ih = 0; i_ih < CONFIG_T::in_height; i_ih++) {
    ReadInputWidth:
        for (unsigned i_iw = 0; i_iw < CONFIG_T::in_width; i_iw++) {
            #pragma HLS LOOP_FLATTEN
            #pragma HLS PIPELINE

            compute_pool_buffer_2d<data_T, res_T, CONFIG_T>(data.read(), line_buffer, res);
        }
    }
}

template <class data_T, class res_T, typename CONFIG_T>
void pooling2d_cl_explicit(hls::stream<data_T> &data, hls::stream<res_T> &res) {
    assert(CONFIG_T::implementation == conv_implementation::linebuffer &&
           "Only \"linebuffer\" implementation is supported in Vitis HLS.");
    assert(CONFIG_T::pad_top == 0 && CONFIG_T::pad_bottom == 0 && CONFIG_T::pad_left == 0 && CONFIG_T::pad_right == 0);
    assert(CONFIG_T::pool_height == CONFIG_T::stride_height && CONFIG_T::pool_width == CONFIG_T::stride_width);

    // Deterministic 2x2 stride-2 streamed max pool.  Row `y` even stores the
    // top row of a window; row `y` odd consumes it and emits at odd x.
    static typename data_T::value_type prev_row[CONFIG_T::n_filt][CONFIG_T::in_width];
    static typename data_T::value_type cur_prev[CONFIG_T::n_filt];

    #pragma HLS ARRAY_PARTITION variable=prev_row complete dim=1

    assert(CONFIG_T::pool_height == 2 && CONFIG_T::pool_width == 2 &&
           "streamed pooling repair implements only 2x2 stride-2 pooling");

ReadInputHeight:
    for (unsigned i_ih = 0; i_ih < CONFIG_T::in_height; i_ih++) {
    ReadInputWidth:
        for (unsigned i_iw = 0; i_iw < CONFIG_T::in_width; i_iw++) {
            #pragma HLS LOOP_FLATTEN
            #pragma HLS PIPELINE
            const unsigned px = i_iw, py = i_ih;
            data_T in_elem = data.read();
            if ((py & 1u) == 0u) {
                for (unsigned c = 0; c < CONFIG_T::n_filt; c++) {
                    #pragma HLS UNROLL
                    prev_row[c][px] = in_elem[c];
                }
            } else {
                if ((px & 1u) == 1u) {
                    res_T res_pack;
                    PRAGMA_DATA_PACK(res_pack)
                    for (unsigned c = 0; c < CONFIG_T::n_filt; c++) {
                        #pragma HLS UNROLL
                        typename data_T::value_type m = prev_row[c][px - 1];
                        if (prev_row[c][px] > m) m = prev_row[c][px];
                        if (cur_prev[c] > m) m = cur_prev[c];
                        if (in_elem[c] > m) m = in_elem[c];
                        res_pack[c] = m;
                    }
                    res.write(res_pack);
                }
                for (unsigned c = 0; c < CONFIG_T::n_filt; c++) {
                    #pragma HLS UNROLL
                    cur_prev[c] = in_elem[c];
                }
            }

        }
    }
}


template <class data_T, class res_T, typename CONFIG_T, bool supported> struct opt4_pool_dispatch {
    static void run(hls::stream<data_T> &data, hls::stream<res_T> &res) {
        pooling2d_cl_stock<data_T, res_T, CONFIG_T>(data, res);
    }
};
template <class data_T, class res_T, typename CONFIG_T>
struct opt4_pool_dispatch<data_T, res_T, CONFIG_T, true> {
    static void run(hls::stream<data_T> &data, hls::stream<res_T> &res) {
        pooling2d_cl_explicit<data_T, res_T, CONFIG_T>(data, res);
    }
};
template <class data_T, class res_T, typename CONFIG_T>
void pooling2d_cl(hls::stream<data_T> &data, hls::stream<res_T> &res) {
    const bool supported = CONFIG_T::pool_op == Max &&
        CONFIG_T::pool_height == 2 && CONFIG_T::pool_width == 2 &&
        CONFIG_T::stride_height == 2 && CONFIG_T::stride_width == 2 &&
        CONFIG_T::pad_top == 0 && CONFIG_T::pad_bottom == 0 &&
        CONFIG_T::pad_left == 0 && CONFIG_T::pad_right == 0 &&
        data_T::size == CONFIG_T::n_filt && res_T::size == CONFIG_T::n_filt;
    opt4_pool_dispatch<data_T, res_T, CONFIG_T, supported>::run(data, res);
}

