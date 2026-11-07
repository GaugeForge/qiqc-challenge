from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .archive import reference_package, MAX_ZIP
from .core import Engine, Problem, Settings, AUDIT_CONTEXT, token_hash
from starlette.middleware.trustedhost import TrustedHostMiddleware

WEB=Path(__file__).resolve().parents[1]/'web'
SITE=Path(__file__).resolve().parents[2]/'dist'


def create_app(settings=None):
    app=FastAPI(title='QIQC local demo',docs_url=None,redoc_url=None,openapi_url=None)
    e=Engine(settings); app.state.engine=e
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=["127.0.0.1","localhost","testserver"])

    @app.exception_handler(Problem)
    async def problem(request,exc): return JSONResponse({'error':exc.message},status_code=exc.status)

    @app.middleware('http')
    async def boundaries(request,call_next):
        if request.method not in ['GET','HEAD','OPTIONS']:
            origin=request.headers.get('origin')
            if origin and urlsplit(origin).netloc!=request.headers.get('host'):
                return JSONResponse({'error':'不允许跨站修改请求'},status_code=403)
            if request.url.path!='/api/login' and request.headers.get('x-qiqc-request')!='1':
                return JSONResponse({'error':'缺少请求保护标记'},status_code=403)
        token=request.cookies.get('qiqc_session','')
        context=AUDIT_CONTEXT.set({'ip':request.client.host,'session':token_hash(token)[:16] if token else None})
        try: response=await call_next(request)
        finally: AUDIT_CONTEXT.reset(context)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['X-Frame-Options']='DENY'
        response.headers['Referrer-Policy']='same-origin'
        response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control']='no-store'
        return response

    def account(request,allow_change=False):
        u=e.session(request.cookies.get('qiqc_session'))
        if u['must_change'] and not allow_change: raise Problem('请先修改初始密码',403)
        return u

    async def body(request):
        try:
            data=await request.json()
            if not isinstance(data,dict): raise ValueError()
            return data
        except Exception: raise Problem('请求格式无效',400)

    @app.get('/api/config')
    def config():
        return {'name':'QIQC Challenge','mode':'local-demo-connected','timezone':'Asia/Singapore','support_email':'qiqc@gauge-forge.com','cloud_ready':False,'r2_ready':e.store.r2,'e2b_smoke_verified':True,'hy4_smoke_verified':True,'always_open':not e.is_closed(), 'competition_closed':e.is_closed(),'global_task_slots':e.settings.slots,'production_initial_slots':50,'model':e.settings.model,'storage':e.store.provider,'execution':'本地 Harbor + Codex mock；科学 task 尚未切换到 E2B','model_provider':'Hy4 / OpenRouter smoke 已通过，正式批次尚未切换','rule_version':'PRD 1.2','quotas':{'full':1,'public':3,'infra':10}}

    @app.post('/api/login')
    async def login(request:Request):
        data=await body(request)
        email=str(data.get('email',''))[:200]; password=str(data.get('password',''))[:200]
        token,user=await run_in_threadpool(e.login,email,password,request.client.host)
        response=JSONResponse(user)
        response.set_cookie('qiqc_session',token,httponly=True,samesite='strict',secure=e.settings.secure_cookies,max_age=43200,path='/')
        return response

    @app.post('/api/logout')
    def logout(request:Request):
        e.logout(request.cookies.get('qiqc_session',''))
        response=JSONResponse({'ok':True}); response.delete_cookie('qiqc_session',path='/'); return response

    @app.post('/api/password')
    async def password(request:Request):
        u=account(request,True); data=await body(request)
        await run_in_threadpool(e.change_password,u['id'],str(data.get('password','')),request.cookies['qiqc_session'])
        return {'ok':True}

    @app.get('/api/me')
    def me(request:Request): return account(request,True)

    @app.get('/api/versions')
    def versions(request:Request): return e.versions(account(request)['id'])

    @app.post('/api/versions')
    async def upload(request:Request):
        u=account(request)
        data=bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data)>MAX_ZIP: raise Problem('压缩包不能超过 20 MiB',413)
        return await run_in_threadpool(e.upload,u['id'],bytes(data))

    @app.get('/api/reference.zip')
    def reference(): return Response(reference_package(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="harness.zip"'})

    @app.get('/api/batches')
    def batches(request:Request): return e.batches(account(request)['id'])

    @app.post('/api/batches')
    async def confirm(request:Request):
        u=account(request); d=await body(request)
        return e.confirm(u['id'],str(d.get('version_id','')),str(d.get('kind','')),str(d.get('mode','')),str(d.get('idempotency_key','')))

    @app.get('/api/batches/{bid}')
    def get_batch(bid:str,request:Request): return e.get_batch(account(request)['id'],bid)

    @app.post('/api/batches/{bid}/cancel')
    def cancel(bid:str,request:Request): e.cancel(account(request)['id'],bid); return {'ok':True}

    @app.get('/api/batches/{bid}/traces/{ordinal}')
    def trace(bid:str,ordinal:int,request:Request): return e.trace(account(request)['id'],bid,ordinal)

    @app.get('/api/leaderboard')
    def leaderboard(): return e.leaderboard()

    @app.post('/api/appeals')
    async def appeal(request:Request):
        u=account(request); d=await body(request); e.appeal(u['id'],str(d.get('body',''))); return {'ok':True}

    @app.get('/api/my-appeal')
    def my_appeal(request:Request):
        u=account(request)
        with e.db() as c:
            a=c.execute('SELECT body,status,created,response FROM appeals WHERE team_id=?',(u['team_id'],)).fetchone()
            return dict(a) if a else None

    @app.get('/api/admin')
    def admin(request:Request): return e.admin_snapshot(account(request)['id'])

    @app.post('/api/admin/actions')
    async def actions(request:Request):
        u=account(request); d=await body(request)
        actor=str(d.get('actor','')); reason=str(d.get('reason','')); action=d.get('action'); target=str(d.get('target',''))
        if action=='review': e.review(u['id'],target,d.get('decision'),actor,reason)
        elif action=='pause': e.set_pause(u['id'],bool(d.get('paused')),actor,reason)
        elif action=='quota': e.adjust_quota(u['id'],target,d.get('kind'),d.get('delta'),actor,reason)
        elif action=='retry': e.retry_batch(u['id'],target,actor,reason)
        elif action=='invalidate': e.invalidate(u['id'],target,actor,reason)
        elif action=='pricing': e.set_pricing(u['id'],d.get('rates'),actor,reason)
        elif action=='appeal': e.answer_appeal(u['id'],target,actor,reason)
        elif action=='close_demo': e.close_demo(u['id'],actor,reason)
        else: raise Problem('未知管理操作',400)
        return {'ok':True}

    @app.get('/healthz')
    def health():
        with e.db() as c: c.execute('SELECT 1')
        return {'status':'ok','mode':'local-demo-connected','rollout_store':e.store.provider}

    @app.get('/')
    def homepage():
        if (SITE/'index.html').exists(): return FileResponse(SITE/'index.html')
        return HTMLResponse('<a href="/submit">Open QIQC local demo</a>')

    @app.get('/submit')
    @app.get('/leaderboard')
    @app.get('/admin')
    def page(): return FileResponse(WEB/'index.html')

    if WEB.exists(): app.mount('/platform-assets',StaticFiles(directory=WEB),name='platform-assets')

    @app.get('/{path:path}')
    def static_site(path:str):
        # Explicit public-root serving: never expose source, database, upload or env directories.
        target=(SITE/path).resolve()
        if not target.is_relative_to(SITE.resolve()) or not target.is_file() or any(part.startswith('.') for part in Path(path).parts):
            raise Problem('页面不存在',404)
        return FileResponse(target)
    return app


def factory(): return create_app()
