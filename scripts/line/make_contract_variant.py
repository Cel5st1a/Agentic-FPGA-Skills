"""Create a deployment-contract variant: same package layout, only named keys changed.

Generic by construction: it knows no model, no precision and no board. Everything comes
from the base contract that is passed in, and every override is named on the command
line. The package layout is copied (not flattened) so the variant contract still
resolves its own relative references (`model.path`, `verification.*`, `board_profile`).

Usage:
  python make_contract_variant.py --deployment <deployment.yaml> --name <variant>
                                 [--out-root <dir>] [--set key=value ...]

Example:
  python make_contract_variant.py --deployment <pkg>/keras_3layer_rf8_resource/deployment.yaml \
      --name precision_24_6 --set precision=ap_fixed<24,6>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

import yaml

DEFAULT_OUT_ROOT = Path(r"D:\ML_models\_line\contracts")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def coerce(raw: str):
    """Keep the contract's own typing: booleans, numbers, and JSON objects/arrays.

    `layer_reuse_factors` is a mapping, so a variant has to be able to set one:
    `--set 'layer_reuse_factors={"q_conv2d": 18}'` must produce a dict, not the string.
    """
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    stripped = raw.strip()
    if stripped[:1] in {"{", "["}:
        try:
            return json.loads(stripped)
        except ValueError as exc:
            raise SystemExit(f"--set value looks like JSON but does not parse: {exc}")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--out-root", type=Path, default=DEFAULT_OUT_ROOT)
    parser.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = parser.parse_args()

    base_contract = args.deployment.resolve()
    base_dir = base_contract.parent
    package_root = base_dir.parent
    variant_root = (args.out_root / args.name).resolve()
    if variant_root.exists():
        raise SystemExit(f"variant already exists, refusing to overwrite: {variant_root}")

    target_dir = variant_root / base_dir.name
    target_dir.parent.mkdir(parents=True)
    shutil.copytree(base_dir, target_dir)
    shared = package_root / "public"
    if shared.is_dir():
        shutil.copytree(shared, variant_root / "public")

    contract = yaml.safe_load((target_dir / base_contract.name).read_text(encoding="utf-8-sig"))
    changes = {}
    for item in args.set:
        if "=" not in item:
            raise SystemExit(f"--set expects KEY=VALUE, got: {item}")
        key, _, raw = item.partition("=")
        key = key.strip()
        # A leading '+' adds a key inside an existing mapping instead of overriding one,
        # which is how `layer_reuse_factors.<layer>` entries are named (the mapping itself
        # is empty in the base contract). Without the '+' a typo still refuses loudly.
        adding = key.startswith("+")
        if adding:
            key = key[1:]
        node = contract
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise SystemExit(f"cannot override '{key}': '{part}' is not a mapping")
        leaf = parts[-1]
        if leaf not in node and not adding:
            raise SystemExit(f"contract has no key '{key}'; nothing to override "
                             f"(prefix the key with '+' to add it to an existing mapping)")
        old = node.get(leaf)
        node[leaf] = coerce(raw.strip())
        changes[key] = {"from": old, "to": node[leaf]}
    if not changes:
        raise SystemExit("no overrides given; refusing to create an identical variant")

    variant_contract = target_dir / base_contract.name
    variant_contract.write_text(
        yaml.safe_dump(contract, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )

    record = {
        "schema_version": 1,
        "variant": args.name,
        "base_contract": str(base_contract),
        "base_contract_sha256": sha256_file(base_contract),
        "variant_contract": str(variant_contract),
        "variant_contract_sha256": sha256_file(variant_contract),
        "changes": changes,
        "shared_assets_copied": bool(shared.is_dir()),
    }
    (variant_root / "variant.json").write_text(
        json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
