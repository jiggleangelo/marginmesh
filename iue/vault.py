import hashlib
import secrets
import threading
import time
from abc import ABC, abstractmethod


class VaultFull(RuntimeError):
    pass


class Vault(ABC):
    @abstractmethod
    def put(self, kind, fields): ...

    @abstractmethod
    def count(self, kind): ...

    @abstractmethod
    def delete(self, receipt): ...


class MemoryVault(Vault):
    def __init__(self, max_records=1000, retention_seconds=86400):
        if max_records <= 0:
            raise ValueError('max_records must be positive')
        self.__records = []
        self.__max_records = max_records
        self.__retention = retention_seconds
        self.__lock = threading.Lock()
        self.__salt = secrets.token_bytes(32)

    def __prune(self, now):
        cutoff = now - self.__retention
        self.__records[:] = [row for row in self.__records if row[0] >= cutoff]

    def __digest(self, value):
        return hashlib.sha256(self.__salt + value.encode('utf-8')).hexdigest()

    def put(self, kind, fields):
        now = time.time()
        receipt = secrets.token_hex(12)
        protected = {}
        for key, value in fields.items():
            value = str(value).strip()
            if value:
                protected[key] = self.__digest(value[:254])
        with self.__lock:
            self.__prune(now)
            if len(self.__records) >= self.__max_records:
                del self.__records[:len(self.__records) - self.__max_records + 1]
            self.__records.append((now, kind, protected, receipt))
        return receipt

    def count(self, kind):
        with self.__lock:
            self.__prune(time.time())
            return sum(row[1] == kind for row in self.__records)

    def delete(self, receipt):
        with self.__lock:
            self.__prune(time.time())
            before = len(self.__records)
            self.__records[:] = [row for row in self.__records if not secrets.compare_digest(row[3], receipt)]
            return len(self.__records) != before

    def __repr__(self):
        return 'MemoryVault(<private>)'

    __str__ = __repr__


class NullVault(Vault):
    def put(self, kind, fields): return 'disabled'
    def count(self, kind): return 0
    def delete(self, receipt): return False
