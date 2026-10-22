"""Producer regressions: a ragged layout and undocumented output packing must fail."""
import json

import pytest

from geometry_fixtures import REAL_CASES, load_producer, real_record


@pytest.mark.parametrize('case,inputs,outputs', REAL_CASES)
def test_real_producer_records_both_sample_layouts(case, inputs, outputs, tmp_path):
    _, _, record = real_record(case, tmp_path / 'board')
    assert record['input_layout']['elements'] == inputs
    assert record.get('output_layout', {}).get('elements') == outputs
    assert record['output_layout']['flatten'] == 'c_order'


@pytest.mark.parametrize('rows', [
    [[[1, 2], [3]], [[4], [5, 6]]],
    [[[1, 2]], [[3], [4]]],
    [[True, 1]], [[float('nan'), 1]], [['1', 2]], [[[], []]],
])
def test_producer_refuses_invalid_numeric_or_rectangular_samples(rows, tmp_path):
    path = tmp_path / 'inputs.json'
    path.write_text(json.dumps(rows), encoding='utf-8')
    with pytest.raises((ValueError, SystemExit), match='sample|ragged|numeric|finite|empty'):
        load_producer().load_samples(path)


def test_stream_producer_reads_sample_layout_without_cli_element_counts(tmp_path):
    _, _, record = real_record('branched_small_ch_last_onnx/20260928-br-stream',
                               tmp_path / 'board')
    assert record['input_layout']['elements'] == 768
    assert record['output_layout']['elements'] == 512
    assert record['interface']['stream']['input_beats_per_sample'] == 256
    assert record['interface']['stream']['output_beats_per_sample'] == 32
