import {cp, mkdir, readdir, rm} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root=path.dirname(fileURLToPath(import.meta.url));
await rm(path.join(root,'dist'),{recursive:true,force:true});
await mkdir(path.join(root,'dist'),{recursive:true});
// Only allowlisted public source is deployable. The historical participant and
// partner documents are versioned under src/downloads and are copied as-is.
for(const entry of await readdir(path.join(root,'src'))){
  if(!['index.html','documents.html','styles.css','responsive.css','theme.css','app.js','shared.js','facts.json','assets','downloads','404.html','_headers'].includes(entry)) throw Error(`Unexpected public source: ${entry}`);
  await cp(path.join(root,'src',entry),path.join(root,'dist',entry),{recursive:true});
}
console.log('Built public website/dist');
