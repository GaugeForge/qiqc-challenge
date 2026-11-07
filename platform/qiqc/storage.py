"""Immutable rollout storage with a local fallback and an R2 S3 adapter.

Only rollout objects use this adapter. Submission archives and the database stay
on the private VPS data directory. R2 credentials are read from an operator-only
environment file and never returned to the application or browser.
"""
import contextlib
import hashlib
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def _load_env_file(path):
    result = {}
    path = Path(path)
    if not path.exists():
        return result
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        result[key.strip()] = value.strip().strip('"\'')
    return result


class ObjectStore:
    """Store rollout archives immutably.

    Set ``QIQC_R2_ENABLED=1`` or provide all R2 fields in the runtime env file
    to select R2. In the absence of a complete configuration, the private local
    store remains available for offline demo and unit tests.
    """

    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        configured = _load_env_file(os.environ.get('QIQC_R2_ENV', '/root/QQ-Challenge/runtime/legacy-platform-data/r2.env'))
        values = {**configured, **{k: v for k, v in os.environ.items() if k.startswith('R2_')}}
        required = ['R2_ENDPOINT', 'R2_ACCESS_KEY_ID', 'R2_SECRET_ACCESS_KEY', 'R2_BUCKET']
        self.r2 = bool(os.environ.get('QIQC_R2_ENABLED') == '1' and all(values.get(k) for k in required))
        if self.r2:
            try:
                import boto3
                self.client = boto3.client(
                    's3', endpoint_url=values['R2_ENDPOINT'],
                    aws_access_key_id=values['R2_ACCESS_KEY_ID'],
                    aws_secret_access_key=values['R2_SECRET_ACCESS_KEY'],
                    region_name=values.get('R2_REGION', 'auto'),
                )
            except ImportError as exc:
                raise RuntimeError('R2 is enabled but boto3 is not installed') from exc
            self.bucket = values['R2_BUCKET']
        else:
            self.client = None
            self.bucket = None

    @property
    def provider(self):
        return 'cloudflare-r2' if self.r2 else 'local-private-substitute'

    @staticmethod
    def _validate_key(key):
        parts = PurePosixPath(key).parts
        if not parts or '..' in parts or key.startswith('/') or '\\' in key:
            raise ValueError('Unsafe object key')

    def path(self, key):
        if self.r2:
            raise ValueError('R2 objects do not have a persistent local path')
        self._validate_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError('Object outside store')
        return path

    def put_file(self, key, source):
        self._validate_key(key)
        source = Path(source)
        sha, size = digest(source), source.stat().st_size
        info = {'key': key, 'sha256': sha, 'size': size, 'provider': self.provider}
        if not self.r2:
            target = self.path(key)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if target.exists():
                if target.stat().st_size != size or digest(target) != sha:
                    raise ValueError('Immutable object collision')
                return info
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix='.upload-')
            try:
                with os.fdopen(fd, 'wb') as out, source.open('rb') as inp:
                    shutil.copyfileobj(inp, out)
                    out.flush()
                    os.fsync(out.fileno())
                if digest(tmp) != sha:
                    raise OSError('Upload integrity mismatch')
                try:
                    os.link(tmp, target)
                except FileExistsError:
                    if digest(target) != sha:
                        raise ValueError('Concurrent immutable object collision')
                dfd = os.open(target.parent, os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            finally:
                Path(tmp).unlink(missing_ok=True)
            return info

        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
            metadata = {k.lower(): v for k, v in (head.get('Metadata') or {}).items()}
            if head.get('ContentLength') != size or metadata.get('sha256') != sha:
                raise ValueError('Immutable R2 object collision')
            return info
        except self.client.exceptions.NoSuchKey:
            pass
        except Exception as exc:
            if getattr(exc, 'response', {}).get('Error', {}).get('Code') not in ('404', 'NoSuchKey'):
                raise
        self.client.upload_file(str(source), self.bucket, key, ExtraArgs={'Metadata': {'sha256': sha}})
        head = self.client.head_object(Bucket=self.bucket, Key=key)
        metadata = {k.lower(): v for k, v in (head.get('Metadata') or {}).items()}
        if head.get('ContentLength') != size or metadata.get('sha256') != sha:
            raise OSError('R2 upload integrity verification failed')
        return info

    @contextlib.contextmanager
    def local_path(self, key):
        """Yield a verified local path, cleaning remote temporary data."""
        self._validate_key(key)
        if not self.r2:
            yield self.path(key)
            return
        fd, name = tempfile.mkstemp(prefix='qiqc-rollout-', suffix='.tar.gz')
        os.close(fd)
        try:
            self.client.download_file(self.bucket, key, name)
            yield Path(name)
        finally:
            Path(name).unlink(missing_ok=True)
