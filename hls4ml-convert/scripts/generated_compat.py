"""Verified compatibility repair restricted to a newly generated hls4ml project."""
import hashlib

BEFORE = '28bc325053f8458fc65e12c54a6b98aa4d9bfe8f8984836a6ddbb149bdd9a269'
AFTER = '79697a887ac1d5b5303211c7c22a3772f2b2efd432dd8b3ac13f337d329401bf'
OLD = '    using r_T = ap_fixed<2 * (decltype(w_T::weight)::width + x_T::width), (decltype(w_T::weight)::width + x_T::width)>;'
NEW = '''    // Shift amount is signed: |shift| <= 2**(Ww-1).  Keep that many integer
    // bits for a left shift and that many fractional bits for a right shift so
    // the product is exact; the original 2*(Ww+Wx) / (Ww+Wx) split truncated
    // every product below 2**-(Ww+Wx-Ix).
    static const int expo_width = decltype(w_T::weight)::width;
    static const int max_shift = 1 << (expo_width - 1);
    using r_T = ap_fixed<x_T::width + 2 * max_shift + 1, x_T::iwidth + max_shift + 1>;'''


def patch_po2_product(project, version):
    target = (project / 'firmware/nnet_utils/nnet_mult.h').resolve()
    if not target.is_relative_to(project.resolve()) or not target.is_file():
        raise ValueError('po2 compatibility target must be inside the generated project')
    raw = target.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    record = {'patch': 'po2-product-width', 'path': str(target), 'hls4ml_version': version,
              'before_sha256': digest, 'scope': 'generated project only'}
    if version != '1.3.0' or digest not in {BEFORE, AFTER}:
        raise ValueError(f'unrecognized hls4ml po2 template/version: {version}, {digest}; no files changed')
    if digest == AFTER:
        return dict(record, status='ALREADY_APPLIED', after_sha256=digest)
    patched = raw.replace(OLD.encode(), NEW.encode())
    after = hashlib.sha256(patched).hexdigest()
    if after != AFTER:
        raise ValueError('unrecognized po2 patch result; no files changed')
    target.write_bytes(patched)
    return dict(record, status='APPLIED', after_sha256=after)


def pin_output_grid(hls_model, precision):
    """Pin only resolved HLS graph outputs, after frontend/skip/fusion resolution."""
    outputs = hls_model.get_output_variables()
    if len(outputs) != 1:
        raise ValueError('board output-grid pin requires exactly one graph output')
    variable = outputs[0]
    before = str(variable.type.precision)
    from hls4ml.backends.fpga.fpga_types import APTypeConverter
    variable.type.precision = APTypeConverter().convert(
        hls_model.config.backend.convert_precision_string(precision))
    producer = next(node.name for node in hls_model.get_layers()
                    if hls_model.outputs[0] in node.outputs)
    config = hls_model.config.config['HLSConfig']
    block = config.setdefault('LayerName', {}).setdefault(producer, {})
    previous = block.get('Precision', {})
    if not isinstance(previous, dict):
        previous = {'default': previous}
    block['Precision'] = dict(previous, result=precision)
    return {'output': hls_model.outputs[0], 'before': before,
            'producer': producer, 'after': str(variable.type.precision), 'scope': 'graph output only'}
