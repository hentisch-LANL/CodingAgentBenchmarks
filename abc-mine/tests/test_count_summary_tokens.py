"""The optional summary-token utility accepts either provider URL convention."""

import importlib.util
import io
from pathlib import Path

import pytest


@pytest.mark.parametrize("suffix", ["", "/", "/v1", "/v1/"])
def test_count_endpoint_normalizes_api_version(monkeypatch, suffix):
    script = Path(__file__).resolve().parents[1] / "cluster_scripts" / "count_summary_tokens.py"
    spec = importlib.util.spec_from_file_location("count_summary_tokens", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    requests = []

    def open_request(request, *, timeout):
        requests.append(request)
        assert timeout == 5
        return io.BytesIO(b'{"input_tokens": 17}')

    monkeypatch.setattr(module.urllib.request, "urlopen", open_request)
    assert module.count_via_api(
        "summary", base_url="https://api.example.com" + suffix,
        model="example-model", api_key="test-key", timeout=5,
    ) == 10
    assert requests[0].full_url == "https://api.example.com/v1/messages/count_tokens"
