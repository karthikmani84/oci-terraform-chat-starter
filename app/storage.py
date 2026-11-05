"""Private Object Storage persistence. Terraform state uses its native OCI backend."""
from __future__ import annotations
import copy
import json
import threading


class Missing(KeyError):
    pass


class MemoryStore:
    def __init__(self):
        self.data = {}
        self.lock = threading.RLock()

    def get(self, key):
        with self.lock:
            if key not in self.data: raise Missing(key)
            return self.data[key]

    def put(self, key, value):
        with self.lock: self.data[key] = bytes(value)

    def json(self, key, default=None):
        try: return json.loads(self.get(key))
        except Missing: return copy.deepcopy(default)

    def save(self, key, value):
        self.put(key, json.dumps(value, sort_keys=True, ensure_ascii=True).encode())

    def keys(self, prefix):
        with self.lock: return sorted(k for k in self.data if k.startswith(prefix))


class ObjectStore(MemoryStore):
    def __init__(self, client, namespace, bucket):
        self.client, self.namespace, self.bucket = client, namespace, bucket

    def get(self, key):
        try: response = self.client.get_object(self.namespace, self.bucket, key)
        except Exception as exc:
            if getattr(exc, 'code', None) == 'ObjectNotFound': raise Missing(key) from None
            raise
        try: return response.data.content
        finally: response.data.close()

    def put(self, key, value):
        self.client.put_object(self.namespace, self.bucket, key, bytes(value))

    def keys(self, prefix):
        keys, start = [], None
        while True:
            kw = {'prefix': prefix, 'limit': 1000}
            if start: kw['start'] = start
            response = self.client.list_objects(self.namespace, self.bucket, **kw).data
            keys.extend(x.name for x in response.objects)
            start = response.next_start_with
            if not start: return keys
