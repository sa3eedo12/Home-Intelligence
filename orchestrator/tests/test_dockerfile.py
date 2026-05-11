import tomllib
from pathlib import Path


def test_dockerfile_runs_orchestrator_as_package() -> None:
    repo_orchestrator_dir = Path(__file__).resolve().parents[1]
    assert (repo_orchestrator_dir / "pyproject.toml").exists()
    assert (repo_orchestrator_dir / "__init__.py").exists()

    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    assert "WORKDIR /src" in text
    assert "COPY orchestrator /src/orchestrator" in text
    assert "pip install --no-cache-dir -e /src/orchestrator" in text
    assert "ENV PYTHONPATH=/src" in text
    assert 'CMD ["uvicorn", "orchestrator.app:app"' in text


def test_pyproject_declares_jinja2_for_dashboard_templates() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    deps = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["dependencies"]
    assert any(dep.startswith("jinja2>=") for dep in deps)
