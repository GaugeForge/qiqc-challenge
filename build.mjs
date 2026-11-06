import {cp, lstat, mkdir, readdir, rm} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
await import('./generate.mjs');
const root=path.dirname(fileURLToPath(import.meta.url));
await rm(path.join(root,'dist'),{recursive:true,force:true});
await mkdir(path.join(root,'dist'),{recursive:true});
// Only allowlisted public source and download files are deployable. The
// historical documents page and its original downloads are copied unchanged;
// the generator must never rewrite them.
for(const entry of await readdir(path.join(root,'src'))){
  if(entry === 'downloads') continue;
  if(!['index.html','documents.html','styles.css','responsive.css','theme.css','app.js','shared.js','facts.json','assets','404.html','_headers'].includes(entry)) throw Error(`Unexpected public source: ${entry}`);
  await cp(path.join(root,'src',entry),path.join(root,'dist',entry),{recursive:true});
}
const downloads=[
  'participant-guide.html','participant-guide.md','participant-guide.pdf',
  'partner-handbook.html','partner-handbook.md','partner-handbook.pdf',
  'participant-manual.en.md','participant-manual.en.pdf'
];
await mkdir(path.join(root,'dist/downloads'),{recursive:true});
for(const name of downloads){
  const source=path.join(root,'src','downloads',name);
  const info=await lstat(source);
  if(!info.isFile() || info.isSymbolicLink()) throw Error(`Download must be a regular file: ${name}`);
  await cp(source,path.join(root,'dist','downloads',name));
}
console.log('Built public website/dist');
