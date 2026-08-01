"""The installable distribution must carry every runtime data dependency."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


def test_package_metadata_declares_runtime_resources_and_pep639_license() -> None:
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["license"] == "Apache-2.0"
    assert project["project"]["license-files"] == ["LICENSE"]
    assert project["tool"]["setuptools"]["package-data"] == {
        "sidq": ["claims/head.npz", "policy/default_policy.yaml"]
    }


def test_sdist_builds_an_offline_installable_wheel_with_runtime_resources(
    tmp_path: Path,
) -> None:
    assert not tmp_path.is_relative_to(_ROOT)
    sdist_dir = tmp_path / "sdist"
    wheel_dir = tmp_path / "wheel"
    install_dir = tmp_path / "installed"
    sdist_dir.mkdir()
    wheel_dir.mkdir()

    backend_call = (
        "import setuptools.build_meta as backend, sys; "
        "print(backend." + "{operation}" + "(sys.argv[1]))"
    )
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            backend_call.format(operation="build_sdist"),
            str(sdist_dir),
        ],
        cwd=_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    (sdist_path,) = sdist_dir.glob("*.tar.gz")
    extracted_dir = tmp_path / "extracted"
    shutil.unpack_archive(sdist_path, extracted_dir, filter="data")
    (sdist_root,) = extracted_dir.iterdir()

    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            backend_call.format(operation="build_wheel"),
            str(wheel_dir),
        ],
        cwd=sdist_root,
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel_path,) = wheel_dir.glob("*.whl")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-index",
            "--target",
            str(install_dir),
            str(wheel_path),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    canonical_head = _ROOT / "data" / "claims" / "reader" / "head.npz"
    expected_head_hash = hashlib.sha256(canonical_head.read_bytes()).hexdigest()
    probe = """
import hashlib
import sys
from pathlib import Path

install_dir = Path(sys.argv[1]).resolve()
repo_root = Path(sys.argv[2]).resolve()
expected_head_hash = sys.argv[3]
sys.path.insert(0, str(install_dir))

import sidq
from sidq.claims.reader import EmbeddingClaimReader
from sidq.policy.engine import load_policy

module_path = Path(sidq.__file__).resolve()
assert module_path.is_relative_to(install_dir), module_path
assert not module_path.is_relative_to(repo_root), module_path
assert load_policy().rules
reader = EmbeddingClaimReader()
packaged_head = module_path.parent / "claims" / "head.npz"
assert packaged_head.is_file()
assert hashlib.sha256(packaged_head.read_bytes()).hexdigest() == expected_head_hash
assert reader.identity["head_sha256"] == expected_head_hash
assert reader.identity["kind"] == "embedding-linear-head"
assert reader.identity["model"] == "microsoft/harrier-oss-v1-270m"
assert reader.identity["revision"] == "31de22b673913c7d658c0f03f792d77c2dcf8ebd"
assert isinstance(reader.identity["threshold"], float)
"""
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            probe,
            str(install_dir),
            str(_ROOT),
            expected_head_hash,
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
