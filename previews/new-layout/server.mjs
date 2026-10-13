import http from 'node:http';
import {readFile} from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
const root=path.dirname(fileURLToPath(import.meta.url)),site=path.resolve(root,'../../src/assets');
const types={'.html':'text/html; charset=utf-8','.css':'text/css','.js':'application/javascript','.svg':'image/svg+xml','.png':'image/png','.ttf':'font/ttf'};
http.createServer(async(req,res)=>{try{const url=new URL(req.url,'http://localhost');const shared=url.pathname.startsWith('/site-assets/');const base=shared?site:root;const relative=shared?url.pathname.slice(13):url.pathname==='/'?'index.html':url.pathname.slice(1);const file=path.resolve(base,relative);if(!file.startsWith(base+path.sep))throw Error('invalid path');const content=await readFile(file);res.writeHead(200,{'Content-Type':types[path.extname(file)]||'application/octet-stream','Cache-Control':'no-cache'});res.end(content);}catch{res.writeHead(404);res.end('Not found');}}).listen(4175,'127.0.0.1',()=>console.log('New layout preview: http://127.0.0.1:4175/'));
