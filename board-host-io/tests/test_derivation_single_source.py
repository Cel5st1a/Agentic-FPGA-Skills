"""The frame geometry has one derivation, and all three consumers must reach it the same way.

An independent review found that `make_host_board.py` passed the board profile into the
derivation while `simulate_host.py` and `verify_board.py` did not. Since the board profile is
the second-priority source of the baudrate, a profile that declared one would have produced a
board built at that rate while the simulation evidence recorded 115200 and the live capture
opened the port at 115200 — the "single implementation" claim holding only by convention.

These tests pin the two halves of the fix: the derivation itself is one entry point
(`geometry_for_wrapper`), and it agrees with what the builder recorded for the same run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SKILL = Path(__file__).resolve().parents[1]
RUN = Path(r"D:\ML_models\_line\runs\mlp_64_32\20260928-hostd")
HOST_WRAPPER = RUN / "p3-build" / "host-board" / "wrapper.json"


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, SKILL / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def real_run():
    wrapper = json.loads(HOST_WRAPPER.read_text(encoding="utf-8"))
    contract = load("host_geometry")  # makes scripts/ importable for the helpers below
    del contract
    import yaml
    deployment = Path(wrapper["contract"])
    return {"wrapper": wrapper, "deployment": deployment,
            "contract": yaml.safe_load(deployment.read_text(encoding="utf-8-sig"))}


def test_the_derivation_agrees_with_what_the_builder_recorded(real_run):
    """The verifier's derivation and the builder's record must be the same geometry."""
    module = load("host_geometry")
    geometry = module.geometry_for_wrapper(real_run["deployment"], real_run["contract"],
                                           real_run["wrapper"])
    recorded = real_run["wrapper"]["host_protocol"]
    assert geometry.request_bytes == recorded["request_bytes"] == 264
    assert geometry.response_frame_bytes == recorded["response_bytes"] == 28
    assert geometry.baudrate == recorded["baudrate"] == 115200
    assert geometry.baudrate_source == recorded["baudrate_source"]
    assert geometry.in_elements == recorded["input_values"] == 64
    assert geometry.out_elements == recorded["output_values"] == 4
    assert geometry.to_dict() == real_run["wrapper"]["host_geometry"]


def test_external_profile_cannot_replace_recorded_host_baudrate(real_run, tmp_path):
    """The second-priority source must actually be read, from the wrapper's own record."""
    module = load("host_geometry")
    profile = module.load_board_profile(
        real_run["wrapper"]["board"]["pins_profile"]) or {}
    profile = dict(profile)
    profile["peripherals"] = dict(profile.get("peripherals") or {})
    profile["peripherals"]["uart"] = dict(profile["peripherals"].get("uart") or {},
                                          baudrate=230400)
    path = tmp_path / "profile_with_baud.yaml"
    import yaml
    path.write_text(yaml.safe_dump(profile), encoding="utf-8")

    wrapper = json.loads(json.dumps(real_run["wrapper"]))
    wrapper["board"]["pins_profile"] = str(path)
    geometry = module.geometry_for_wrapper(real_run["deployment"], real_run["contract"], wrapper)
    assert geometry.baudrate == real_run["wrapper"]["host_geometry"]["baudrate"] == 115200
    assert geometry.baudrate_source == real_run["wrapper"]["host_geometry"]["baudrate_source"]


def test_changed_contract_mapping_cannot_replace_recorded_host_baudrate(real_run, tmp_path):
    module = load("host_geometry")
    contract = dict(real_run["contract"])
    contract["host_transport"] = {"channel": "uart", "baudrate": 921600}
    with pytest.raises(ValueError, match="contract mapping"):
        module.geometry_for_wrapper(real_run["deployment"], contract, real_run["wrapper"])


def test_every_consumer_routes_through_the_one_entry_point():
    """Structural, not behavioural: `geometry_from_contract` must not be a second door.

    The helper's behaviour is pinned above; this ensures no consumer bypasses it and
    re-derives with its own (possibly different) inputs.
    """
    for name in ("make_host_board", "simulate_host", "verify_board"):
        source = (SKILL / "scripts" / f"{name}.py").read_text(encoding="utf-8")
        assert "geometry_for_wrapper(" in source, f"{name} does not use the shared entry point"
        assert "geometry_from_contract(" not in source, (
            f"{name} derives geometry directly instead of through geometry_for_wrapper")


def test_verify_board_provenance_covers_the_derivation():
    """The on-board audit record must hash whatever determines the wire bytes."""
    module = load("verify_board")
    run = RUN
    sources = module.provenance_sources(
        run / "contract" / "mlp_64_32" / "deployment.yaml",
        run / "contract" / "mlp_64_32" / "inputs.json",
        run / "evidence" / "python-output.json",
        run / "p3-build" / "csim" / "csim_results.log",
        run / "contract" / "mlp_64_32" / "encoder.keras",
        run / "p3-build" / "host-board" / "board_top.bit",
        run / "p3-build" / "host-board" / "wrapper.json",
        run / "evidence" / "host-port-audit" / "io-scope.json",
        run / "hardware-program" / "summary.json")
    names = {path.name for path in sources}
    assert "host_geometry.py" in names, "the derivation module is not in the audit record"
    assert "protocol.py" in names
    # The three files the run really has must exist; the rest are produced later in the flow.
    for required in ("host_geometry.py", "protocol.py", "verify_board.py"):
        assert (SKILL / "scripts" / required).is_file()
    assert len(sources) == len(set(sources))
