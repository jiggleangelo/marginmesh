import hashlib
import json
import os
import secrets
import shutil
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path


class StoreError(RuntimeError):
    status = 413


class CapacityError(StoreError):
    pass


class DataFull(StoreError):
    status = 507


class Missing(StoreError):
    status = 404


class RateLimit(StoreError):
    status = 429


class Conflict(StoreError):
    status = 409


class WorkspaceStore:
    def __init__(self, root, cap_mb=200, clock=time.time):
        self.root = Path(root).resolve()
        self.ws = self.root / 'ws'
        self.ws.mkdir(parents=True, exist_ok=True)
        self.cap = int(cap_mb) * 1024 * 1024
        self.clock = clock
        self.guard = threading.Lock()
        self.locks = {}
        self.writes = {}
        self.creations = deque()
        self.cache = OrderedDict()
        self.total = self._size()

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    def path(self, token):
        return self.ws / self.digest(token)

    def resolve(self, token):
        path = self.path(token)
        if not path.is_dir() or (path / '.deleted').exists():
            raise Missing('Workspace not found')
        return path

    def _size(self):
        return sum(p.stat().st_size for p in self.root.rglob('*') if p.is_file())

    def _atomic(self, path, value):
        raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        tmp = path.with_name(path.name + '.tmp')
        with open(tmp, 'wb') as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return len(raw)

    def _lock(self, path):
        with self.guard:
            if not path.is_dir():
                raise Missing('Workspace not found')
            return self.locks.setdefault(path.name, threading.Lock())

    def _prune(self, bucket, window=3600):
        now = self.clock()
        while bucket and now - bucket[0] >= window:
            bucket.popleft()
        return now

    def create(self, state=None, sample=False):
        with self.guard:
            now = self._prune(self.creations)
            if len(self.creations) >= 60:
                raise RateLimit('Workspace creation limit reached; try again later')
            if sum(1 for p in self.ws.iterdir() if p.is_dir()) >= 3000:
                raise CapacityError('The workspace limit has been reached')
            if self.total >= self.cap:
                raise DataFull('The application data cap has been reached')
            token = secrets.token_urlsafe(24)
            path = self.path(token)
            while path.exists():
                token = secrets.token_urlsafe(24)
                path = self.path(token)
            path.mkdir()
            self.creations.append(now)
        base = state or {'models': [], 'endpoints': [], 'chips': [], 'scenarios': {},
                         'created_at': now, 'sample': bool(sample), 'version': 1}
        base.setdefault('created_at', now)
        base.setdefault('version', 1)
        base['sample'] = bool(sample)
        meta = {'created': now, 'last_used': now, 'written': False}
        try:
            state_size = self._atomic(path / 'state.json', base)
            meta_size = self._atomic(path / 'meta.json', meta)
            with self.guard:
                self.total += state_size + meta_size
        except Exception:
            shutil.rmtree(path, ignore_errors=True)
            raise
        return token

    def read(self, token):
        path = self.resolve(token)
        with self._lock(path):
            self.resolve(token)
            try:
                state = json.loads((path / 'state.json').read_text(encoding='utf-8'))
                meta = json.loads((path / 'meta.json').read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError, TypeError):
                raise Missing('Workspace not found')
            now = self.clock()
            if now - meta.get('last_used', 0) >= 3600:
                old = (path / 'meta.json').stat().st_size
                meta['last_used'] = now
                new = self._atomic(path / 'meta.json', meta)
                with self.guard:
                    self.total += new - old
            return state

    def allow_write(self, path):
        with self.guard:
            bucket = self.writes.setdefault(path.name, deque())
            now = self._prune(bucket)
            if len(bucket) >= 120:
                raise RateLimit('Workspace write limit reached; try again later')
            bucket.append(now)

    def save(self, token, state):
        path = self.resolve(token)
        self.allow_write(path)
        with self._lock(path):
            self.resolve(token)
            state_path = path / 'state.json'
            meta_path = path / 'meta.json'
            try:
                stored = json.loads(state_path.read_text(encoding='utf-8'))
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError, TypeError):
                raise Missing('Workspace not found')
            supplied_version = int(state.get('version', 0))
            if supplied_version != int(stored.get('version', 0)):
                raise Conflict('Workspace changed in another tab; reload and try again')
            state['version'] = supplied_version + 1
            raw = json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            if len(raw) > 5 * 1024 * 1024:
                raise CapacityError('This workspace would exceed 5 MB')
            old_state = state_path.stat().st_size
            old_meta = meta_path.stat().st_size
            meta.update(last_used=self.clock(), written=True)
            meta_raw = json.dumps(meta, separators=(',', ':')).encode('utf-8')
            with self.guard:
                if self.total - old_state - old_meta + len(raw) + len(meta_raw) > self.cap:
                    raise DataFull('The application data cap has been reached')
            new_state = self._atomic(state_path, state)
            new_meta = self._atomic(meta_path, meta)
            with self.guard:
                self.total += new_state + new_meta - old_state - old_meta
                self.cache.pop(path.name, None)
        return state

    def delete(self, token):
        path = self.resolve(token)
        with self._lock(path):
            if not path.is_dir():
                raise Missing('Workspace not found')
            try:
                (path / '.deleted').write_bytes(b'')
            except OSError:
                pass
            size = sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
            shutil.rmtree(path, ignore_errors=True)
            with self.guard:
                self.total = max(0, self.total - size)
                self.locks.pop(path.name, None)
                self.writes.pop(path.name, None)
                self.cache.pop(path.name, None)

    def sweep(self, now=None):
        now = self.clock() if now is None else now
        for path in list(self.ws.iterdir()):
            if not path.is_dir():
                continue
            try:
                meta = json.loads((path / 'meta.json').read_text(encoding='utf-8'))
                expired = now - meta.get('last_used', 0) > 90 * 86400
                unwritten = not meta.get('written') and now - meta.get('created', 0) > 86400
                if expired or unwritten:
                    with self._lock(path):
                        if path.is_dir():
                            shutil.rmtree(path, ignore_errors=True)
                    with self.guard:
                        self.locks.pop(path.name, None)
                        self.writes.pop(path.name, None)
                        self.cache.pop(path.name, None)
            except (OSError, ValueError, TypeError, Missing):
                continue
        with self.guard:
            self.total = self._size()

    def cached(self, token, state, builder):
        path = self.resolve(token)
        key = path.name
        version = state.get('version', 0)
        with self.guard:
            found = self.cache.get(key)
            if found and found[0] == version:
                self.cache.move_to_end(key)
                return found[1]
        value = builder(state)
        with self.guard:
            self.cache[key] = (version, value)
            self.cache.move_to_end(key)
            while len(self.cache) > 256:
                self.cache.popitem(last=False)
        return value
