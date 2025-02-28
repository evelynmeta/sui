#!/usr/bin/env python3
# Copyright (c) Mysten Labs, Inc.
# SPDX-License-Identifier: Apache-2.0

import argparse
from os import chdir, remove
from pathlib import Path
import re
from shutil import which, rmtree
import subprocess
from sys import stderr, stdout


def parse_args():
    parser = argparse.ArgumentParser(
        prog="execution-layer",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the operations, without running them",
    )

    subparsers = parser.add_subparsers(
        description="Tools for managing cuts of the execution-layer",
    )

    cut = subparsers.add_parser(
        "cut",
        help=(
            "Create a new copy of execution-related crates, and add them to "
            "the workspace.  Assigning an execution layer version to the new "
            "copy and implementing the Execution and Verifier traits in "
            "crates/sui-execution must be done manually as a follow-up."
        ),
    )

    cut.set_defaults(do=do_cut)
    cut.add_argument(
        "feature",
        type=feature,
        help="The name of the new cut to make",
    )

    return parser.parse_args()


def feature(f):
    if re.match(r"[a-z][a-zA-Z0-9_]*", f):
        return f
    else:
        raise argparse.ArgumentTypeError(f"Invalid feature name: '{f}'")


def do_cut(args):
    """Perform the actions of the 'cut' sub-command.
    Accepts the parsed command-line arguments as a parameter."""
    cmd = cut_command(args.feature)

    if args.dry_run:
        cmd.append("--dry-run")
        print(run(cmd))
        return

    impl_module = Path() / "sui-execution" / "src" / (args.feature + ".rs")
    if impl_module.is_file():
        raise RuntimeError(
            f"Impl module for '{args.feature}' already exists at '{impl_module}'"
        )

    print("Cutting new release", file=stderr)
    result = subprocess.run(cmd, stdout=stdout, stderr=stderr)

    if result.returncode != 0:
        print("Cut failed", file=stderr)
        exit(result.returncode)

    clean_up_cut(args.feature)
    update_toml(args.feature)
    generate_impls(args.feature, impl_module)
    generate_lib()
    run(["cargo", "hakari", "generate"])


def run(command):
    """Run command, and return its stdout as a UTF-8 string."""
    result = subprocess.run(command, stdout=subprocess.PIPE)
    return result.stdout.decode("utf-8")


def repo_root():
    """Find the repository root, using git."""
    return run(["git", "rev-parse", "--show-toplevel"]).strip()


def cut_command(f):
    """Arguments for creating the cut for 'feature'."""
    return [
        *["cargo", "run", "--bin", "cut", "--"],
        *["--feature", f],
        *["-d", f"sui-execution/latest:sui-execution/{f}:-latest"],
        *["-d", f"external-crates/move:external-crates/move-execution/{f}"],
        *["-p", "sui-adapter-latest"],
        *["-p", "sui-move-natives-latest"],
        *["-p", "sui-verifier-latest"],
        *["-p", "move-bytecode-verifier"],
        *["-p", "move-stdlib"],
        *["-p", "move-vm-runtime"],
    ]


def clean_up_cut(feature):
    """Remove some special-case files/directories from a given cut"""
    move_exec = Path() / "external-crates" / "move-execution" / feature
    rmtree(move_exec / "move-bytecode-verifier" / "transactional-tests")
    remove(move_exec / "move-stdlib" / "src" / "main.rs")
    rmtree(move_exec / "move-stdlib" / "tests")


def update_toml(feature):
    """Add dependencies for 'feature' to sui-execution's manifest."""
    toml_path = Path() / "sui-execution" / "Cargo.toml"

    # Read all the lines
    with open(toml_path) as toml:
        lines = toml.readlines()

    # Write them back, looking for template comment lines
    with open(toml_path, mode="w") as toml:
        for line in lines:
            if line.startswith("# ") and "$CUT" in line:
                toml.write(line[2:].replace("$CUT", feature))
            toml.write(line)


def generate_impls(feature, copy):
    """Create the implementations of the `Executor` and `Verifier`.

    Copies the implementation for the `latest` cut and updates its imports."""
    orig = Path() / "sui-execution" / "src" / "latest.rs"
    with open(orig, mode="r") as orig, open(copy, mode="w") as copy:
        for line in orig:
            line = re.sub(r"^use (.*)_latest::", rf"use \1_{feature}::", line)
            copy.write(line)


def generate_lib():
    """Expose all `Executor` and `Verifier` impls via lib.rs

    Generates and overwrites sui-execution/src/lib.rs to assign a numeric
    execution version for every trait that assigns an execution version.

    Version snapshots (whose names follow the pattern `/v[0-9]+/`) are assigned
    versions according to their names (v0 gets 0, v1 gets 1, etc).

    `latest` gets the next version after all version snapshots.

    Feature snapshots (all other snapshots) are assigned versions starting with
    `u64::MAX` and going down, in the order they were created (as measured by
    git commit timestamps)"""

    src = Path() / "sui-execution" / "src"
    actual_path = src / "lib.rs"
    template_path = src / "lib.template.rs"

    cuts = discover_cuts()

    with open(template_path, mode="r") as template_file:
        template = template_file.read()

    def substitute(m):
        spc = m.group(1)
        var = m.group(2)

        if var == "GENERATED_MESSAGE":
            cmd = "./scripts/execution-layer"
            return f"{spc}// DO NOT MODIFY, Generated by {cmd}"
        elif var == "MOD_CUTS":
            return "".join(f"{spc}mod {cut};" for (_, _, cut) in cuts)
        elif var == "FEATURE_CONSTS":
            return "".join(
                f"{spc}pub const {feature}: u64 = {version};"
                for (version, feature, _) in cuts
                if feature is not None
            )
        elif var == "EXECUTOR_CUTS":
            executor = (
                "{spc}{version} => Arc::new({cut}::Executor::new(\n"
                "{spc}    protocol_config,\n"
                "{spc}    paranoid_type_checks,\n"
                "{spc}    silent,\n"
                "{spc})?),\n"
            )
            return "\n".join(
                executor.format(spc=spc, version=feature or version, cut=cut)
                for (version, feature, cut) in cuts
            )
        elif var == "VERIFIER_CUTS":
            call = "Verifier::new(protocol_config, is_metered, metrics)"
            return "\n".join(
                f"{spc}{feature or version} => Box::new({cut}::{call}),"
                for (version, feature, cut) in cuts
            )
        else:
            raise AssertionError(f"Don't know how to substitute {var}")

    with open(actual_path, mode="w") as actual_file:
        actual_file.write(
            re.sub(
                r"^(\s*)// \$([A-Z_]+)$",
                substitute,
                template,
                flags=re.MULTILINE,
            ),
        )


# Modules in `sui-execution` that don't count as "cuts" (they are
# other supporting modules)
NOT_A_CUT = {
    "executor",
    "lib",
    "lib.template",
    "tests",
    "verifier",
}


def discover_cuts():
    """Find all modules corresponding to execution layer cuts.

    Finds all modules within the `sui-execution` crate that count as
    entry points for an execution layer cut.  Returns a list of
    3-tuples, where:

    - The 0th element is a string representing the version number.
    - The 1st element is an (optional) constant name for the version,
      used to easily export the versions for features.
    - The 2nd element is the name of the module.

    Snapshot cuts (with names following the pattern /latest|v[0-9]+/)
    are assigned version numbers according to their name (with latest
    getting the version one higher than the highest occupied snapshot
    version).

    Feature cuts (all other cuts) are assigned versions starting with
    `u64::MAX` and counting down, ordering features first by commit
    time, and then by name.
    """

    snapshots = []
    features = []

    src = Path() / "sui-execution" / "src"
    for f in src.iterdir():
        if not f.is_file() or f.stem in NOT_A_CUT:
            continue
        elif re.match(r"latest|v[0-9]+", f.stem):
            snapshots.append(f)
        else:
            features.append(f)

    def snapshot_key(path):
        if path.stem == "latest":
            return float("inf")
        else:
            return int(path.stem[1:])

    def feature_key(path):
        return path.stem

    snapshots.sort(key=snapshot_key)
    features.sort(key=feature_key)

    cuts = []
    for snapshot in snapshots:
        mod = snapshot.stem
        if mod != "latest":
            cuts.append((mod[1:], None, mod))
            continue

        # Latest gets one higher version than any other snapshot
        # version we've assigned so far
        ver = 1 + max(int(v) for (v, _, _) in cuts)
        cuts.append((str(ver), None, "latest"))

    # "Feature" cuts are not intended to be used on production
    # networks, so stability is not as important for them, they are
    # assigned versions in lexicographical order.
    for i, feature in enumerate(features):
        version = f"u64::MAX - {i}" if i > 0 else "u64::MAX"
        cuts.append((version, feature.stem.upper(), feature.stem))

    return cuts


if __name__ == "__main__":
    for bin in ["git", "cargo", "cargo-hakari"]:
        if not which(bin):
            print(f"Please install '{bin}'", file=stderr)

    args = parse_args()
    chdir(repo_root())
    args.do(args)
