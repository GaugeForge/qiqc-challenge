"""Verify the sealed policy describes the actual gateway/client timeout chain."""
import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'competition-platform/platform/scripts/science'))
import native_controller


def policy(monkeypatch):
    monkeypatch.setattr(native_controller, 'file_hash', lambda _: 'a' * 64)
    return native_controller.inventory(ROOT, ROOT)['native_model_gateway_policy']


def test_runtime_timeout_chain_matches_sealed_policy(monkeypatch):
    value = policy(monkeypatch)
    gateway = (ROOT / 'competition-platform/platform-go/cmd/qiqc-model-gateway/main.go').read_text()
    proxy = (ROOT / 'competition-platform/platform-go/internal/modelproxy/proxy.go').read_text()
    candidate = (ROOT / 'competition-platform/platform/scripts/science/native_candidate.py').read_text()
    http_source = (ROOT / 'benchmark-assets/adapters/science-probe/native_model_http.py').read_text()
    bridge = (ROOT / 'benchmark-assets/adapters/science-probe/native_worker_bridge.py').read_text()
    overhead = int(re.search(r'WriteTimeout:\s*\*requestTimeout\s*\+\s*(\d+)\s*\*\s*time.Second', gateway)[1])
    accounting = int(re.search(r'context.WithTimeout\(context.WithoutCancel\(ctx\),\s*(\d+)\s*\*\s*time.Second', proxy)[1])
    request = int(re.search(r'"-request-timeout",\s*"(\d+)s"', candidate)[1])
    client_calls = [node for node in ast.walk(ast.parse(http_source)) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'AsyncClient']
    assert len(client_calls) == 1
    client = ast.literal_eval(next(k.value for k in client_calls[0].keywords if k.arg == 'timeout'))
    relay = int(re.search(r'relay_sandbox.REPLY_TIMEOUT=(\d+)', bridge)[1])
    assert value['request_timeout_seconds'] == request
    assert value['usage_record_timeout_seconds'] == accounting
    assert value['server_write_timeout_seconds'] == request + overhead
    assert value['http_client_reply_timeout_seconds'] == client
    assert value['relay_reply_timeout_seconds'] == relay
    assert request + accounting < request + overhead < client < relay
    shutdown = int(re.search(r'context.WithTimeout\(context.Background\(\),\s*(\d+)\s*\*\s*time.Second', gateway)[1])
    assert shutdown > accounting
