import hashlib

import pytest

from src.services.prompt_version import resolve_canonical_prompt_version


@pytest.mark.parametrize(
    "declaration",
    (
        "prompt_version: EagleEye-V6.4",
        '  "prompt_version": "EagleEye-V6.4",',
        "版本：EagleEye-V6.4",
    ),
)
def test_prompt_version_extracts_narrow_supported_declarations(declaration):
    prompt = f"analysis instructions\n{declaration}\nstrict output"

    assert resolve_canonical_prompt_version(prompt) == "EagleEye-V6.4"


def test_explicit_prompt_version_has_priority_over_prompt_declaration():
    prompt = '"prompt_version": "EagleEye-V6.4"'

    assert (
        resolve_canonical_prompt_version(
            prompt,
            explicit_version="Task-Override-V2",
        )
        == "Task-Override-V2"
    )


def test_prompt_version_uses_stable_hash_when_declaration_is_absent():
    prompt = "final prompt without a version declaration"
    expected = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]

    first = resolve_canonical_prompt_version(prompt)
    second = resolve_canonical_prompt_version(prompt)

    assert first == f"sha256:{expected}"
    assert second == first
    assert resolve_canonical_prompt_version(prompt + " changed") != first


@pytest.mark.parametrize(
    "invalid_version",
    ("", "unsafe value", "line\nbreak", "x" * 65),
)
def test_explicit_prompt_version_rejects_unsafe_values_without_echoing_them(
    invalid_version,
):
    with pytest.raises(ValueError) as exc_info:
        resolve_canonical_prompt_version(
            "safe prompt",
            explicit_version=invalid_version,
        )

    if invalid_version:
        assert invalid_version not in str(exc_info.value)
