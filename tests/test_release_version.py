from pathlib import Path
import tomllib

from app.main import app


def test_package_and_api_versions_match():
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["version"] == "0.4.0"
    assert app.version == project["version"]
