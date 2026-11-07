"""Bounded, non-executing archive inspection, also used inside quarantine Docker."""
import hashlib
import io
import json
import re
import stat
import struct
import sys
import zipfile
import unicodedata
from pathlib import PurePosixPath

RULESET_VERSION = "qiqc-archive/2"

MAX_ZIP = 20 * 1024 * 1024
MAX_EXPANDED = 100 * 1024 * 1024
MANIFEST = {'schema':'qiqc-harness/v1-demo','name':'Codex reference','entrypoint':'adapter.sh','description':'Organizer-owned reference; real local execution only on the mock task.'}
REFERENCE_FILES = {
    'harness.json': json.dumps(MANIFEST, indent=2)+'\n',
    'adapter.sh': '#!/bin/sh\n# Contract example. The demo maps this exact package hash to Harbor built-in Codex.\n# Entrypoints from uploaded packages are NEVER executed on the VPS.\nexec codex exec --skip-git-repo-check --model "$QIQC_MODEL" "$(cat "$QIQC_TASK_INPUT")"\n',
    'README.md': '# Codex reference harness\n\nThis exact package selects the organizer-owned Harbor Codex adapter for a local mock task.\nModified packages require manual review and can only use simulated execution in this demo.\nProduction ABI, E2B isolation and Hy4 compatibility still require validation.\n',
}


def reference_package():
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
        for name,body in REFERENCE_FILES.items():
            info=zipfile.ZipInfo(name, (2026,10,14,0,0,0))
            info.compress_type=zipfile.ZIP_DEFLATED
            info.external_attr=(stat.S_IFREG|0o644)<<16
            z.writestr(info, body)
    return stream.getvalue()


# Participant-facing harness.json. Only `runtime` is required; everything else
# is derived from the selected runtime family below. Old full-form manifests
# remain valid when every declared value uses the accepted compatibility window.
SCHEMA = 'qiqc-harness/v1'
DEFAULT_ENTRYPOINT = 'adapter/run.sh'
RUNTIMES = {
    # `runtime_versions` is the participant-facing compatibility window.  The
    # deployed snapshot is tracked separately because changing the accepted
    # manifest versions must not silently claim that a new CLI is installed.
    'codex': {'adapter':'codex','label':'Codex','runtime_package':'@openai/codex',
              'runtime_version':'0.144.4', 'deployed_version':'0.144.4',
              'latest_available':'0.166.0',
              'runtime_versions':['0.166.0','0.165.0','0.164.0','0.163.0','0.162.1','0.162.0','0.144.4'],
              'model_interfaces':['responses']},
    'deepseek': {'adapter':'deepseek-harness','label':'DeepSeek Harness','runtime_package':'@deepseek-ai/dsh',
                 'runtime_version':'0.2.1-rc.2', 'deployed_version':'0.2.1-rc.2',
                 'latest_available':'0.2.2-rc.1',
                 'runtime_versions':['0.2.2-rc.1','0.2.1-rc.5','0.2.1-rc.4','0.2.1-rc.3','0.2.1-rc.2'],
                 'model_interfaces':['chat/completions','responses']},
    'pi': {'adapter':'pi-agent','label':'Pi Agent','runtime_package':'@earendil-works/pi-coding-agent',
           'runtime_version':'1.3.0', 'deployed_version':'1.3.0',
           'latest_available':'1.5.0',
           'runtime_versions':['1.5.0','1.4.0','1.3.0','1.2.0','1.1.0','1.0.4'],
           'model_interfaces':['chat/completions','responses']},
}
RUNTIME_ALIASES = {'codex':'codex','deepseek':'deepseek','deepseek-harness':'deepseek','pi':'pi','pi-agent':'pi'}
FIXED_FIELDS = {'runtime_kind':'native_cli','task_transport':'native-http/v1','trajectory':'native_jsonl'}
PARTICIPANT_FIELDS = ('runtime','entrypoint','name','description')
LEGACY_FIELDS = ('schema','adapter','runtime_kind','runtime_package','runtime_version','model_interfaces','trajectory','task_transport','version')
REPORTED_FIELDS = ('schema','name','entrypoint','runtime','adapter','runtime_kind','runtime_package','runtime_version','model_interfaces','trajectory','task_transport')
RUNTIME_CHOICES = 'codex, deepseek or pi'


def _runtime_id(value, field):
    if not isinstance(value,str) or value not in RUNTIME_ALIASES:
        raise ValueError('harness.json: "%s" must be one of %s' % (field, RUNTIME_CHOICES))
    return RUNTIME_ALIASES[value]


def normalize_manifest(manifest):
    """Validate a qiqc-harness/v1 manifest and return the complete derived form.

    Raises ValueError with a participant-safe English message. Messages never
    echo submitted values; they only name known fields and organizer constants.
    """
    if not isinstance(manifest,dict):
        raise ValueError('harness.json: the file must contain one JSON object, for example {"runtime": "codex"}')
    if set(manifest)-set(PARTICIPANT_FIELDS)-set(LEGACY_FIELDS):
        raise ValueError('harness.json: unsupported field. Allowed fields are runtime, entrypoint, name and description; keys, budgets, models and task selection are not allowed')
    if 'schema' in manifest and manifest['schema']!=SCHEMA:
        raise ValueError('harness.json: "schema" is optional; if present it must be qiqc-harness/v1')
    runtime=None
    if 'runtime' in manifest: runtime=_runtime_id(manifest['runtime'],'runtime')
    if 'adapter' in manifest:
        legacy=_runtime_id(manifest['adapter'],'adapter')
        if runtime is not None and legacy!=runtime:
            raise ValueError('harness.json: "runtime" and the legacy "adapter" field name different runtimes; keep only "runtime"')
        runtime=legacy
    if runtime is None:
        raise ValueError('harness.json: "runtime" is required and must be one of '+RUNTIME_CHOICES)
    spec=RUNTIMES[runtime]
    for field,expected in FIXED_FIELDS.items():
        if field in manifest and manifest[field]!=expected:
            raise ValueError('harness.json: "%s" must be %s or be omitted (it is derived automatically)' % (field, expected))
    if 'runtime_package' in manifest and manifest['runtime_package']!=spec['runtime_package']:
        raise ValueError('harness.json: "runtime_package" does not match the %s runtime package supported by the platform; remove the field' % runtime)
    if 'runtime_version' in manifest and manifest['runtime_version'] not in spec['runtime_versions']:
        raise ValueError('harness.json: "runtime_version" is not a supported %s version; use a version listed in the participant manual or omit the field' % runtime)
    interfaces=list(spec['model_interfaces'])
    if 'model_interfaces' in manifest:
        declared=manifest['model_interfaces']
        if (not isinstance(declared,list) or not declared or len(set(map(str,declared)))!=len(declared)
                or any(not isinstance(x,str) or x not in spec['model_interfaces'] for x in declared)):
            raise ValueError('harness.json: "model_interfaces" is not supported by the %s runtime (allowed: %s); remove the field' % (runtime, ', '.join(spec['model_interfaces'])))
        interfaces=declared
    if 'version' in manifest and (not isinstance(manifest['version'],str) or len(manifest['version'])>80):
        raise ValueError('harness.json: "version" is no longer needed; the platform numbers versions v1, v2, ... by upload order. Remove the field')
    entry=manifest.get('entrypoint',DEFAULT_ENTRYPOINT)
    if not isinstance(entry,str) or not entry:
        raise ValueError('harness.json: "entrypoint" must be a relative path such as adapter/run.sh')
    if not entry.startswith('adapter/'): raise ValueError('Entrypoint must be inside adapter/')
    if not entry.endswith('.sh'): raise ValueError('Adapter entrypoint must be a shell script ending in .sh')
    default_name=spec['label'] if spec['label'].endswith('Harness') else spec['label']+' harness'
    name=manifest.get('name',default_name)
    if not isinstance(name,str) or not 1<=len(name)<=80 or any(unicodedata.category(c).startswith('C') for c in name):
        raise ValueError('harness.json: "name" is optional; if present it must be 1–80 printable characters')
    description=manifest.get('description','')
    if not isinstance(description,str) or len(description)>500:
        raise ValueError('harness.json: "description" is optional; if present it must be text of at most 500 characters')
    normalized={'schema':SCHEMA,'name':name,'entrypoint':entry,'runtime':runtime,'adapter':spec['adapter'],
                'runtime_kind':FIXED_FIELDS['runtime_kind'],'runtime_package':spec['runtime_package'],
                'runtime_version':spec['runtime_version'],'model_interfaces':interfaces,
                'trajectory':FIXED_FIELDS['trajectory'],'task_transport':FIXED_FIELDS['task_transport']}
    if description: normalized['description']=description
    return normalized


def safe_name(name):
    if not name or '\\' in name or ':' in name or name.startswith('/') or any(unicodedata.category(c).startswith('C') for c in name):
        raise ValueError('Unsafe archive path')
    parts=PurePosixPath(name).parts
    if '..' in parts or any(part.startswith('.') and part not in ['.gitignore'] for part in parts):
        raise ValueError('Hidden or escaping archive path')
    if name.rstrip('/') != '/'.join(parts):
        raise ValueError('Non-canonical archive path')



def reviewer_snippet(text, budget=2048):
    """Untrusted code stays data; redact secrets before storage/model access."""
    text=re.sub(r'(?s)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)', '[REDACTED PRIVATE KEY]', text)
    text=re.sub(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [REDACTED]', text)
    text=re.sub(r'''(?i)(?:[a-z0-9_]*(?:api[_-]?key|access[_-]?token|secret[_-]?key)|HY_API|QIQC_MODEL_TOKEN)[\s"']*[:=][\s"']*[^\s,"'\\}]+''', '[REDACTED CREDENTIAL]', text)
    return text.encode('utf-8')[:budget].decode('utf-8',errors='ignore')


_REFERENCE_HOSTS = {
    'arxiv.org', 'doi.org', 'dx.doi.org', 'github.com', 'gitlab.com',
    'openreview.net', 'pypi.org', 'nature.com', 'science.org', 'acm.org',
    'ieeexplore.ieee.org', 'wikipedia.org', 'docs.python.org',
}
_INTERNAL_HOSTS = {'qsim', 'localhost', '127.0.0.1', '0.0.0.0', '::1'}
_NETWORK_CONTEXT = re.compile(r'(?i)(?:curl|wget|requests?\.|httpx|urllib|urlopen|fetch\s*\(|axios|socket|http\.client|open_connection)')
_URL = re.compile(r'(?i)https?://([^/\s\"\'<>):]+)(?::\d+)?(?:/[^\s\"\'<>]*)?')


def _external_url_risk(filename, text):
    """Return precise network findings without rejecting references or qsim URLs.

    A URL in documentation or a citation is data. Internal service addresses
    are part of the harness contract. Only an external URL used in an
    executable network context is reported for review.
    """
    suffix = PurePosixPath(filename).suffix.lower()
    doc_file = suffix in {'.md', '.rst', '.txt', '.adoc', '.tex', '.bib'} or PurePosixPath(filename).name.lower() in {'readme', 'changelog', 'license'}
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        for match in _URL.finditer(line):
            host = match.group(1).lower().rstrip('.')
            root = '.'.join(host.split('.')[-2:]) if '.' in host else host
            if host in _INTERNAL_HOSTS or host.split('.')[0] in _INTERNAL_HOSTS:
                continue
            if doc_file or root in _REFERENCE_HOSTS or host in _REFERENCE_HOSTS:
                continue
            if not _NETWORK_CONTEXT.search(line):
                continue
            findings.append({'line': number, 'explanation': 'External URL is used in executable network code and requires review.', 'match': match.group(0)})
    return findings


def inspect_archive(data):
    if len(data)>MAX_ZIP: raise ValueError('Compressed package exceeds 20 MiB')
    risks=[]; manifest=None; names={}; snippets=[]; total=0
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        infos=z.infolist()
        if len(infos)>1000: raise ValueError('Too many archive entries')
        for info in infos:
            # ZipInfo truncates filenames at NUL; inspect the original as well.
            safe_name(info.orig_filename)
            safe_name(info.filename)
            name=unicodedata.normalize('NFC', info.filename.rstrip('/')).casefold()
            if name in names: raise ValueError('Duplicate archive path')
            names[name] = info.is_dir()
            # Alternate path/link encodings can disagree with Python's name
            # when a later extractor honors Unix or Unicode extra records.
            extra = info.extra
            while extra:
                if len(extra) < 4:
                    raise ValueError('Malformed extra metadata')
                kind, size = struct.unpack('<HH', extra[:4])
                if len(extra) < 4 + size:
                    raise ValueError('Truncated extra metadata')
                if kind in {0x000d, 0x5855, 0x756e, 0x7075}:
                    raise ValueError('Alternate path and Unix link metadata unsupported')
                extra = extra[4 + size:]
            mode=info.external_attr>>16
            if stat.S_IFMT(mode) not in [0, stat.S_IFREG, stat.S_IFDIR]:
                raise ValueError('Links and special files are forbidden')
            if info.flag_bits & 1: raise ValueError('Encrypted archives are unsupported')
            if info.compress_type not in [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED]:
                raise ValueError('Unsupported compression method')
            total+=info.file_size
            if info.file_size>MAX_ZIP or total>MAX_EXPANDED or info.file_size>max(1,info.compress_size)*100:
                raise ValueError('Expanded size or compression ratio exceeds limits')
            if stat.S_IFMT(mode) == stat.S_IFDIR and not info.is_dir():
                raise ValueError('Directory metadata contradicts path')
            if info.is_dir():
                if info.file_size or stat.S_IFMT(mode) == stat.S_IFREG:
                    raise ValueError('Directory has content or regular-file metadata')
                z.read(info)  # Verify local headers and CRC even for empty directories.
                continue
            # Fully validate bounded content/CRC, but never extract or execute it.
            body=z.read(info)
            if len(body)!=info.file_size: raise ValueError('Archive size mismatch')
            if info.filename=='harness.json':
                if len(body)>16384: raise ValueError('Manifest too large')
                manifest=json.loads(body)
            if len(body)<200000:
                text=body.decode('utf-8',errors='replace')
                for finding in _external_url_risk(info.filename, text):
                    risks.append({'file':info.filename, 'rule':'external_endpoint', 'line':finding['line'], 'explanation':finding['explanation']})
                for pattern,label,explanation in [
                    ('docker.sock','Docker 控制接口','Docker control socket access requires review.'),
                    ('169.254.169.254','云元数据接口','Cloud metadata endpoint access requires review.'),
                    ('PRIVATE KEY','疑似私钥','Private key material was found in the archive.'),
                    ('webhook','Webhook','Webhook configuration requires review.'),
                    ('OPENAI_API_KEY=sk-','疑似硬编码凭证','A hard-coded API credential pattern was found.'),
                ]:
                    position = text.find(pattern)
                    if position >= 0:
                        risks.append({'file':info.filename,'rule':label,'line':text.count('\n', 0, position)+1,'explanation':explanation})
                remaining=16384-sum(len(item['text'].encode('utf-8')) for item in snippets)
                if len(snippets)<15 and remaining>0:
                    snippets.append({'file':info.filename,'text':reviewer_snippet(text,min(2048,remaining)),'untrusted':'true'})
        for name in names:
            for parent in PurePosixPath(name).parents:
                if str(parent) in names and not names[str(parent)]:
                    raise ValueError('File path conflicts with child entry')
        if not isinstance(manifest,dict):
            raise ValueError('harness.json: the file must contain one JSON object, for example {"runtime": "codex"}')
        if manifest.get('schema')=='qiqc-harness/v1-demo':
            # Historical organizer fixtures only; kept for stored-version compatibility.
            if not isinstance(manifest.get('name'),str) or not 1<=len(manifest['name'])<=80:
                raise ValueError('Manifest requires a name of 1–80 characters')
            entry=manifest.get('entrypoint')
            if not isinstance(entry,str): raise ValueError('Manifest requires entrypoint')
            safe_name(entry)
            if entry not in z.namelist() or z.getinfo(entry).is_dir(): raise ValueError('Entrypoint file missing')
        else:
            manifest=normalize_manifest(manifest)
            entry=manifest['entrypoint']
            safe_name(entry)
            if entry not in z.namelist() or z.getinfo(entry).is_dir(): raise ValueError('Entrypoint file missing')
            if any(name!='harness.json' and PurePosixPath(name).parts[0] not in ('adapter','harness') for name in names):
                raise ValueError('Only harness/, adapter/ and harness.json are allowed at the root')
            if not any(name.startswith('harness/') and not directory for name,directory in names.items()):
                raise ValueError('Harness directory is empty')

    return {'ruleset_version':RULESET_VERSION, 'manifest':{k:manifest[k] for k in REPORTED_FIELDS if k in manifest},'files':len(infos),'expanded_bytes':total,'risks':risks,'snippets':snippets,'ai_review':'not_connected_manual_review_required','reference':hashlib.sha256(data).digest()==hashlib.sha256(reference_package()).digest()}


if __name__=='__main__':
    try:
        with open(sys.argv[1],'rb') as f: data=f.read(MAX_ZIP+1)
        print(json.dumps({'ok':True,'report':inspect_archive(data)},ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok':False,'error_kind':'invalid_package','error':str(exc)[:300]},ensure_ascii=False))
        sys.exit(1)
