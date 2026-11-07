"""Run-scoped model gateway.

Only this process reads the real provider key.  It accepts the two OpenAI
compatible request shapes used by the adapters and forwards them to either the
legacy Harbor endpoint or the operator supplied Hy4/OpenRouter endpoint.
"""
import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .core import Engine, Problem


def _load_env(path):
    values = {}
    path = Path(path)
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.removeprefix('export ').split('=', 1)
        values[key.strip()] = value.strip().strip('"\'')
    return values


def credentials():
    """Return normalized credentials without ever logging their values.

    Demo precedence is explicit: HY_API/HY_URL from the project .env are used
    when present; Harbor's local OPENAI_* file remains a fallback for the
    existing Codex mock.  HY_URL may be a base (/v1) or a complete chat or
    responses endpoint.
    """
    project = Path(os.environ.get('QIQC_PROJECT_ENV', '/root/QQ-Challenge/.env'))
    legacy = Path(os.environ.get('QIQC_HARBOR_ENV', '/root/.config/harbor/.env'))
    values = {**_load_env(legacy), **_load_env(project), **os.environ}
    key = values.get('HY_API') or values.get('OPENAI_API_KEY')
    base = values.get('HY_URL') or values.get('OPENAI_BASE_URL')
    if not key or not base:
        raise RuntimeError('Provider configuration unavailable')
    return {'OPENAI_API_KEY': key, 'OPENAI_BASE_URL': base.rstrip('/')}


def upstream_url(base, endpoint):
    parsed = urlsplit(base)
    path = parsed.path.rstrip('/')
    suffix = '/' + endpoint
    if path.endswith(suffix):
        return base
    return base + suffix


def _usage_from_event(event):
    if not isinstance(event, dict):
        return None
    if event.get('type') == 'response.completed':
        return event.get('response', {}).get('usage')
    if isinstance(event.get('usage'), dict):
        return event['usage']
    return None


def factory(settings=None):
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    e = Engine(settings)
    creds = credentials()
    semaphore = asyncio.Semaphore(2)
    base = creds['OPENAI_BASE_URL']

    @app.exception_handler(Problem)
    async def issue(request, exc):
        return JSONResponse({'error': exc.message}, status_code=exc.status)

    @app.post('/v1/{endpoint:path}')
    async def forward(endpoint: str, request: Request):
        if endpoint not in ['responses', 'chat/completions']:
            raise Problem('Endpoint not allowed', 404)
        raw = await request.body()
        if len(raw) > 4 * 1024 * 1024:
            raise Problem('Request too large', 413)
        try:
            body = json.loads(raw)
        except Exception:
            raise Problem('Invalid JSON', 400)
        if not isinstance(body, dict):
            raise Problem('Expected a JSON object', 400)
        token = request.headers.get('authorization', '').removeprefix('Bearer ')
        grant = e.authorize_model(token, body.get('model'))
        if body.get('previous_response_id'):
            raise Problem('Cross-request response references disabled in demo', 400)
        await semaphore.acquire()
        client = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10), follow_redirects=False, trust_env=False)
        response = None
        try:
            for attempt in range(3):
                req = client.build_request(
                    'POST', upstream_url(base, endpoint), content=raw,
                    headers={'Authorization': 'Bearer ' + creds['OPENAI_API_KEY'],
                             'Content-Type': 'application/json',
                             'Accept': 'text/event-stream' if body.get('stream') else 'application/json'},
                )
                response = await client.send(req, stream=True)
                if response.status_code not in [429, 502, 503, 504] or attempt == 2:
                    break
                e.record_usage(grant, response.status_code)
                await response.aclose()
                await asyncio.sleep(min(2 ** attempt, 4))
            if response.status_code >= 300:
                status = response.status_code
                await response.aclose(); await client.aclose()
                e.record_usage(grant, status)
                semaphore.release()
                return JSONResponse({'error': {'message': 'Model provider temporarily unavailable', 'type': 'upstream_error'}}, status_code=502 if status >= 500 else status)
            if not body.get('stream'):
                data = await response.aread()
                try:
                    usage = json.loads(data).get('usage')
                except Exception:
                    usage = None
                e.record_usage(grant, response.status_code, usage)
                await response.aclose(); await client.aclose()
                semaphore.release()
                return Response(data, media_type='application/json')
        except (httpx.HTTPError, asyncio.TimeoutError):
            if response:
                await response.aclose()
            await client.aclose(); e.record_usage(grant, 504); semaphore.release()
            return JSONResponse({'error': {'message': 'Model provider timeout', 'type': 'upstream_error'}}, status_code=504)
        except BaseException:
            await client.aclose(); semaphore.release()
            raise

        async def stream():
            usage = None; pending = b''; status = 200
            try:
                async for chunk in response.aiter_bytes():
                    pending += chunk
                    while b'\n' in pending:
                        line, pending = pending.split(b'\n', 1)
                        if line.startswith(b'data: '):
                            try:
                                usage = _usage_from_event(json.loads(line[6:])) or usage
                            except (ValueError, AttributeError):
                                pass
                    if len(pending) > 4 * 1024 * 1024:
                        raise RuntimeError('Oversized stream record')
                    yield chunk
            except asyncio.CancelledError:
                status = 200 if usage is not None else 499
                raise
            except (httpx.HTTPError, RuntimeError):
                status = 502
                raise
            finally:
                e.record_usage(grant, status, usage)
                semaphore.release()
                await response.aclose(); await client.aclose()
        return StreamingResponse(stream(), media_type='text/event-stream', headers={'Cache-Control': 'no-store'})
    return app
