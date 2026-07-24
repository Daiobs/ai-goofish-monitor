from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_settings_page_exposes_gpt_5_6_variants_and_reasoning_effort():
    view = (REPO_ROOT / "web-ui/src/views/SettingsView.vue").read_text(
        encoding="utf-8"
    )
    api = (REPO_ROOT / "web-ui/src/api/settings.ts").read_text(encoding="utf-8")

    for model_id in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        assert model_id in view
    for effort in ("none", "low", "medium", "high", "xhigh", "max"):
        assert f'value="{effort}"' in view
    assert "OPENAI_REASONING_EFFORT" in api
