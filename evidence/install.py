"""Install the pinned baseline database only when its inputs match this image."""
import hashlib
import json
import lzma
from pathlib import Path
import platform
import sqlite3
import sys


def install(archive, destination, revision, lock):
    data = lzma.decompress(archive.read_bytes())
    if hashlib.sha256(data).hexdigest() != '16b34212534393c32afce53f20d8f1ebc0605fa4ae1a479d56c6996828e5af17':
        raise ValueError('Pinned baseline database checksum mismatch')
    if hashlib.sha256(lock.read_bytes()).hexdigest() != '4201e26c5e438697189dbfb809917eb8618d488a6926eee0f49858aad971d212':
        raise ValueError('Dependency lock differs from the collection environment')
    if (sys.platform, platform.machine(), sys.implementation.name) != ('linux', 'x86_64', 'cpython'):
        raise ValueError('Baseline map requires CPython on Linux AMD64')

    destination.write_bytes(data)
    db = sqlite3.connect(destination.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        metadata = {key: json.loads(value) for key, value in db.execute(
            "SELECT family, metadata FROM families WHERE family IN ('commit','version','python','pytest_exit')"
        )}
        if metadata != dict(commit=revision, version=2, python=platform.python_version(), pytest_exit=0):
            raise ValueError(f'Incompatible baseline metadata: {metadata}')
        db.execute('SELECT family, key, value FROM records LIMIT 0')
        if db.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('Invalid SQLite dependency database')
    finally:
        db.close()


if __name__ == '__main__':
    install(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
