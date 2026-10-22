"""Independent hashes for the deployment, its board file and the pin facts."""
import hashlib
from pathlib import Path
import yaml


def identity_fields(deployment, pins_profile=None):
    deployment = Path(deployment).resolve()
    contract = yaml.safe_load(deployment.read_text(encoding="utf-8-sig"))
    relative = contract.get("board_profile")
    if not relative:
        raise ValueError("contract declares no board_profile; board identity is required")
    board = (deployment.parent / relative).resolve()
    record = {"contract_sha256": hashlib.sha256(deployment.read_bytes()).hexdigest(),
              "board_profile": str(board),
              "board_profile_sha256": hashlib.sha256(board.read_bytes()).hexdigest()}
    if pins_profile is not None:
        pins = Path(pins_profile).resolve()
        record.update(board_pins_profile=str(pins),
                      board_pins_profile_sha256=hashlib.sha256(pins.read_bytes()).hexdigest())
    return record
