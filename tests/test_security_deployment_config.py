from pathlib import Path

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_default_compose_builds_current_source_on_loopback():
    compose = yaml.safe_load(
        (REPO_ROOT / "docker-compose.yaml").read_text(encoding="utf-8")
    )
    app = compose["services"]["app"]

    assert app["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert app["image"] == "${APP_IMAGE:-ai-goofish-monitor:local}"
    assert app["ports"] == ["127.0.0.1:8000:8000"]
    assert "ghcr.io/usagi-org" not in app["image"]
    assert "pull_policy" not in app
