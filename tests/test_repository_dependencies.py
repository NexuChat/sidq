"""The trusted publisher checks exports independently of proposed test code."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from sidq.bot.action import ActionError
from sidq.bot.dependencies import EXPORTS, validate_exports

ROOT = Path(__file__).parents[1]
HASH_A = "sha256:" + "a" * 64
HASH_B = "sha256:" + "b" * 64
HASH_C = "sha256:" + "c" * 64
PACKAGE = f'''[[package]]
name = "example-package"
version = "1.2.3"
source = {{ registry = "https://pypi.org/simple" }}
sdist = {{ hash = "{HASH_A}" }}
wheels = [{{ hash = "{HASH_B}" }}]
'''
PIN = f"example-package==1.2.3 \\\n    --hash={HASH_A} \\\n    --hash={HASH_B}\n"


@pytest.fixture
def exports(tmp_path: Path) -> Path:
    (tmp_path / "uv.lock").write_text("version = 1\n" + PACKAGE, encoding="utf-8")
    for filename in EXPORTS:
        (tmp_path / filename).write_text(PIN, encoding="utf-8")
    return tmp_path


def test_actual_repository_exports_match_the_uv_lock(tmp_path: Path) -> None:
    for filename in ("uv.lock", *EXPORTS):
        shutil.copyfile(ROOT / filename, tmp_path / filename)
    validate_exports(tmp_path)


def test_valid_exports_do_not_read_or_execute_proposed_tests(exports: Path) -> None:
    tests = exports / "tests"
    tests.mkdir()
    (tests / "test_dependency_security.py").write_text(
        "raise RuntimeError('untrusted')"
    )
    validate_exports(exports)


@pytest.mark.parametrize("filename", EXPORTS)
def test_stale_pin_fails_for_every_export(exports: Path, filename: str) -> None:
    (exports / filename).write_text(PIN.replace("1.2.3", "1.2.2"))
    with pytest.raises(ActionError, match="absent from uv.lock"):
        validate_exports(exports)


@pytest.mark.parametrize(
    "contents",
    [
        PIN.replace(HASH_B, HASH_C),
        f"example-package==1.2.3 --hash={HASH_A}\n",
        PIN.rstrip() + f" --hash={HASH_C}\n",
        PIN + PIN.replace(HASH_B, HASH_C),
    ],
    ids=["altered", "missing", "extra", "duplicate-pin-mismatch"],
)
def test_every_exported_hash_set_must_match(exports: Path, contents: str) -> None:
    (exports / EXPORTS[0]).write_text(contents)
    with pytest.raises(ActionError, match="artifact hashes differ"):
        validate_exports(exports)


@pytest.mark.parametrize(
    "bad_pin",
    [
        "example-package==1.2.3\n",
        "example-package>=1.2.3\n",
        "example-package==1.*\n",
        "example-package @ https://example.org/archive.whl\n",
        "--index-url https://example.org/simple\n",
        f"example-package==1.2.3 --hash={HASH_A} --hash=sha256:nope\n",
        f"example-package==1.2.3 --hash={HASH_A} --hash=md5:abcd\n",
        f"example-package==1.2.3 --hash={HASH_A} --hash={HASH_B} trailing\n",
        f"example-package==1.2.3 nonsense --hash={HASH_A} --hash={HASH_B}\n",
        f"example-package==1.2.3 ; nonsense --hash={HASH_A} --hash={HASH_B}\n",
        f"example-package==1.2.3 ; (sys_platform == 'win32' --hash={HASH_A} --hash={HASH_B}\n",
        "example-package==1.2.3 \\\n",
        f"example-package==1.2.3 \\\n# interruption\n --hash={HASH_A}\n",
        f"--hash={HASH_A}\n",
    ],
)
def test_malformed_lines_cannot_hide_beside_valid_pins(
    exports: Path, bad_pin: str
) -> None:
    (exports / EXPORTS[0]).write_text(PIN + bad_pin)
    with pytest.raises(ActionError):
        validate_exports(exports)


@pytest.mark.parametrize("contents", ["", "# comments only\n\n"])
def test_empty_exports_fail(exports: Path, contents: str) -> None:
    (exports / EXPORTS[0]).write_text(contents)
    with pytest.raises(ActionError, match="no exported pins"):
        validate_exports(exports)


def test_markers_comments_normalized_names_and_multiple_versions(exports: Path) -> None:
    lock = exports / "uv.lock"
    lock.write_text(lock.read_text() + PACKAGE.replace("1.2.3", "2.0rc1+local"))
    contents = (
        "# generated export\n\n"
        + PIN.replace("example-package", "Example_Package").replace(
            "==1.2.3", "==1.2.3 ; (sys_platform == 'win32' or os_name == 'nt')"
        )
        + "    # via example\n"
        + PIN.replace("==1.2.3", "==2.0rc1+local ; sys_platform != 'win32'")
        + "    # via another dependency\n"
        + PIN.rstrip()
        + " # another marker-compatible duplicate\n"
    )
    (exports / EXPORTS[0]).write_text(contents)
    validate_exports(exports)


@pytest.mark.parametrize("filename", ("uv.lock", *EXPORTS))
def test_missing_input_fails_closed(exports: Path, filename: str) -> None:
    (exports / filename).unlink()
    with pytest.raises(ActionError):
        validate_exports(exports)


@pytest.mark.parametrize(
    "contents",
    [
        "[not valid TOML",
        "version = true\n" + PACKAGE,
        "version = 2\n" + PACKAGE,
        "version = 1\n",
        'version = 1\npackage = "not a list"\n',
        'version = 1\npackage = ["not a package"]\n',
        "version = 1\npackage = []\n",
        "version = 1\n" + PACKAGE.replace('name = "example-package"', "name = 123"),
        "version = 1\n" + PACKAGE.replace('version = "1.2.3"', "version = true"),
        "version = 1\n" + PACKAGE.replace(f'{{ hash = "{HASH_A}" }}', "false"),
        "version = 1\n" + PACKAGE.replace(f'[{{ hash = "{HASH_B}" }}]', '"bad"'),
        "version = 1\n" + PACKAGE.replace(f'{{ hash = "{HASH_B}" }}', "false"),
        "version = 1\n" + PACKAGE.replace(HASH_A, "sha256:bad"),
        "version = 1\n" + PACKAGE.replace(f'hash = "{HASH_A}"', "hash = 123"),
        "version = 1\n" + PACKAGE.replace(f'hash = "{HASH_A}"', 'url = "archive"'),
        "version = 1\n" + PACKAGE + PACKAGE,
        "version = 1\n" + PACKAGE + PACKAGE.replace(HASH_B, HASH_C),
        "version = 1\n"
        + PACKAGE
        + PACKAGE.replace("example-package", "Example_Package"),
    ],
)
def test_malformed_or_ambiguous_uv_lock_fails(exports: Path, contents: str) -> None:
    (exports / "uv.lock").write_text(contents)
    with pytest.raises(ActionError):
        validate_exports(exports)


@pytest.mark.parametrize("filename", ("uv.lock", *EXPORTS))
def test_symlinked_input_fails(exports: Path, filename: str) -> None:
    path = exports / filename
    target = exports / "real-input"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(ActionError, match="symlink"):
        validate_exports(exports)


@pytest.mark.parametrize("filename", ("uv.lock", *EXPORTS))
def test_non_utf8_input_fails_as_action_error(exports: Path, filename: str) -> None:
    (exports / filename).write_bytes(b"\xff\xfe")
    with pytest.raises(ActionError, match="cannot read dependency lock inputs"):
        validate_exports(exports)


def test_oversized_input_uses_the_bounded_reader(exports: Path) -> None:
    (exports / EXPORTS[0]).write_text("#" * (5 * 1024 * 1024 + 1))
    with pytest.raises(ActionError, match="size limit"):
        validate_exports(exports)
