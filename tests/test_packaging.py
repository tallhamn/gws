from pathlib import Path


def test_gws_dockerfile_copies_worker_registry() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")

    assert "COPY workers.yaml ." in text
