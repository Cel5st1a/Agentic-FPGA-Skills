import re

from test_s15_numeric_policy import ROOT


def test_full_payload_selection_and_write_have_elaboration_constant_offsets():
    source = (ROOT / "board-host-io/assets/host_stream_adapter.v").read_text()
    source = re.sub(r"//[^\n]*", "", source)
    # These are full sample buses: an element selection indexed by a runtime beat
    # creates a whole-sample selection network irrespective of arithmetic RF.
    selects = re.findall(r"(?:input_words|output_words)\s*\[([^]]+)\]", source)
    assert all("beat_index" not in expr for expr in selects), selects
