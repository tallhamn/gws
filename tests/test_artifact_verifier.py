import pytest

from gws.verifier import verify_artifacts, ArtifactVerdict


@pytest.mark.asyncio
async def test_verify_artifacts_all_pass():
    async def mock_gateway(**kwargs):
        return {"status": "ok", "exit_code": 0, "output": ""}

    result = await verify_artifacts(
        requirements=["check:file_exists:index.html", "check:build"],
        gateway_url="http://gateway:8080",
        repo="studio-ystackai",
        _gateway_call=mock_gateway,
    )
    assert result.passed is True
    assert len(result.results) == 2
    assert all(r["passed"] for r in result.results)


@pytest.mark.asyncio
async def test_verify_artifacts_one_fails():
    call_count = 0

    async def mock_gateway(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            return {"status": "error", "exit_code": 1, "output": "build failed"}
        return {"status": "ok", "exit_code": 0, "output": ""}

    result = await verify_artifacts(
        requirements=["check:file_exists:index.html", "check:build"],
        gateway_url="http://gateway:8080",
        repo="studio-ystackai",
        _gateway_call=mock_gateway,
    )
    assert result.passed is False
    assert result.results[0]["passed"] is True
    assert result.results[1]["passed"] is False


@pytest.mark.asyncio
async def test_verify_artifacts_empty_requirements():
    result = await verify_artifacts(
        requirements=[],
        gateway_url="http://gateway:8080",
        repo="studio-ystackai",
    )
    assert result.passed is True
    assert result.results == []
