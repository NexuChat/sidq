"""Read-only consistency guard for the repository's uv dependency exports.

Run this from the trusted publisher, not from proposed PR test code. This checks
exported identities and complete artifact hash sets; it neither resolves the
dependency closure nor audits packages for vulnerabilities.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

from sidq.bot.action import ActionError
from sidq.bot.applicability import read_file

EXPORTS = (
    "requirements.lock",
    "requirements-action.lock",
    "requirements-bench.lock",
    "requirements-dev.lock",
    "requirements-landing.lock",
)
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?"
# Exact version tokens, including epochs, pre/post/dev releases and local IDs.
_VERSION = (
    r"v?(?:[0-9]+!)?[0-9]+(?:\.[0-9]+)*"
    r"(?:[-_.]?(?:a|b|c|rc|alpha|beta|pre|preview)[-_.]?[0-9]*)?"
    r"(?:(?:-[0-9]+)|(?:[-_.]?(?:post|rev|r)[-_.]?[0-9]*))?"
    r"(?:[-_.]?dev[-_.]?[0-9]*)?(?:\+[a-z0-9]+(?:[-_.][a-z0-9]+)*)?"
)
_PIN = re.compile(
    rf"(?P<name>{_NAME})==(?P<version>{_VERSION})"
    r"(?:[ \t]*;[ \t]*(?P<marker>.+))?",
    re.IGNORECASE,
)
_HASH = r"sha256:[0-9a-f]{64}"
_HASHES = re.compile(rf"--hash={_HASH}(?:[ \t]+--hash={_HASH})*")
_VARIABLE = (
    r"(?:python_version|python_full_version|os_name|sys_platform|"
    r"platform_release|platform_system|platform_version|platform_machine|"
    r"platform_python_implementation|implementation_name|implementation_version|"
    r"extra|extras|dependency_groups)"
)
_OPERAND = rf"(?:{_VARIABLE}|'[^'\\\r\n]*'|\"[^\"\\\r\n]*\")"
_COMPARISON = re.compile(
    rf"{_OPERAND}[ \t]*(?:===|==|!=|<=|>=|~=|<|>|not[ \t]+in\b|in\b)"
    rf"[ \t]*{_OPERAND}"
)


def _valid_marker(value: str) -> bool:
    """Check marker grammar without evaluating the proposed environment."""
    position, depth, operand = 0, 0, True
    while position < len(value):
        if value[position] in " \t":
            position += 1
        elif operand and value[position] == "(":
            depth += 1
            position += 1
        elif operand:
            comparison = _COMPARISON.match(value, position)
            if comparison is None:
                return False
            position, operand = comparison.end(), False
        elif value[position] == ")" and depth:
            depth -= 1
            position += 1
        else:
            conjunction = re.compile(r"(?:and|or)\b").match(value, position)
            if conjunction is None:
                return False
            position, operand = conjunction.end(), True
    return not operand and depth == 0


def _packages(contents: str) -> dict[tuple[str, str], frozenset[str]]:
    document = tomllib.loads(contents)
    if type(document.get("version")) is not int or document["version"] != 1:
        raise ActionError("unsupported uv.lock format")
    packages = document.get("package")
    if not isinstance(packages, list) or not packages:
        raise ActionError("uv.lock must contain a nonempty package list")
    result: dict[tuple[str, str], frozenset[str]] = {}
    for package in packages:
        if not isinstance(package, dict):
            raise ActionError("malformed uv.lock package")
        name, version = package.get("name"), package.get("version")
        if (
            not isinstance(name, str)
            or re.fullmatch(_NAME, name) is None
            or not isinstance(version, str)
            or re.fullmatch(_VERSION, version, re.IGNORECASE) is None
        ):
            raise ActionError("malformed uv.lock package identity")
        key = (re.sub(r"[-_.]+", "-", name).lower(), version)
        if key in result:
            raise ActionError("ambiguous duplicate uv.lock package identity")
        wheels = package.get("wheels", [])
        if not isinstance(wheels, list):
            raise ActionError("malformed uv.lock wheels")
        artifacts = [*wheels]
        if "sdist" in package:
            artifacts.append(package["sdist"])
        hashes: set[str] = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise ActionError("malformed uv.lock artifact")
            digest = artifact.get("hash")
            if not isinstance(digest, str) or re.fullmatch(_HASH, digest) is None:
                raise ActionError("uv.lock artifact requires a sha256 hash")
            hashes.add(digest)
        result[key] = frozenset(hashes)
    return result


def _statements(contents: str, filename: str) -> Iterator[str]:
    continued: list[str] = []
    for raw in contents.split("\n"):
        line = raw.removesuffix("\r").strip(" \t")
        if not line or line.startswith("#"):
            if continued:
                raise ActionError(f"{filename}: interrupted requirement continuation")
            continue
        # Requirements comments begin with whitespace + #, outside quoted markers.
        quote = ""
        for position, character in enumerate(line):
            if character in "\"'":
                quote = "" if quote == character else quote or character
            elif (
                character == "#"
                and not quote
                and position
                and line[position - 1] in " \t"
            ):
                line = line[:position].rstrip(" \t")
                break
        if line.endswith("\\"):
            continued.append(line[:-1].rstrip(" \t"))
        else:
            yield " ".join([*continued, line])
            continued.clear()
    if continued:
        raise ActionError(f"{filename}: unfinished requirement continuation")


def validate_exports(root: Path) -> None:
    """Reject unreadable, malformed, stale or differently hashed uv exports."""
    try:
        packages = _packages(read_file(root, "uv.lock"))
        for filename in EXPORTS:
            count = 0
            for statement in _statements(read_file(root, filename), filename):
                requirement, separator, hashes = statement.partition(" --hash=")
                pin = _PIN.fullmatch(requirement.rstrip(" \t"))
                if (
                    pin is None
                    or not separator
                    or _HASHES.fullmatch("--hash=" + hashes) is None
                    or (pin["marker"] is not None and not _valid_marker(pin["marker"]))
                ):
                    raise ActionError(f"{filename}: malformed or unhashed exact pin")
                key = (re.sub(r"[-_.]+", "-", pin["name"]).lower(), pin["version"])
                if key not in packages:
                    raise ActionError(
                        f"{filename}: exported pin is absent from uv.lock"
                    )
                actual = frozenset(re.findall(_HASH, hashes))
                if not actual or actual != packages[key]:
                    raise ActionError(f"{filename}: exported artifact hashes differ")
                count += 1
            if not count:
                raise ActionError(f"{filename}: no exported pins")
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ActionError("cannot read dependency lock inputs") from error
