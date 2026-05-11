from pathlib import Path


def test_dockerfile_runs_orchestrator_as_package() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    assert "WORKDIR /src" in text
    assert "COPY orchestrator /src/orchestrator" in text
    assert "pip install --no-cache-dir -e /src/orchestrator" in text
    assert "ENV PYTHONPATH=/src" in text
    assert 'CMD ["uvicorn", "orchestrator.app:app"' in text
