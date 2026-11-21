"""Compile the pinned JSON evidence into individually addressable, read-only records."""
import json
from pathlib import Path
import sqlite3
import sys


_MISSING = object()


class Records:
    def __init__(self, connection, name):
        self.connection = connection
        self.name = name
        self.cache = {}

    def get(self, key, default=None):
        if key not in self.cache:
            row = self.connection.execute(
                'SELECT value FROM records WHERE family=? AND key=?', (self.name, key)
            ).fetchone()
            self.cache[key] = _MISSING if row is None else json.loads(row[0])
        value = self.cache[key]
        return default if value is _MISSING else value

    def __contains__(self, key):
        return self.get(key, _MISSING) is not _MISSING


def load(path):
    if path.suffix != '.sqlite':
        return json.loads(path.read_bytes())
    connection = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    mapping = {}
    for name, value in connection.execute('SELECT family, metadata FROM families'):
        mapping[name] = Records(connection, name) if value is None else json.loads(value)
    return mapping


def compile_map(source, destination):
    mapping = json.loads(source.read_bytes())
    with sqlite3.connect(destination) as db:
        db.execute('CREATE TABLE families(family TEXT PRIMARY KEY, metadata TEXT) WITHOUT ROWID')
        db.execute('CREATE TABLE records(family TEXT, key TEXT, value BLOB NOT NULL, '
                   'PRIMARY KEY(family,key)) WITHOUT ROWID')
        for name, values in sorted(mapping.items()):
            metadata = None if isinstance(values, dict) else json.dumps(values, separators=(',', ':'))
            db.execute('INSERT INTO families VALUES (?,?)', (name, metadata))
            if isinstance(values, dict):
                db.executemany('INSERT INTO records VALUES (?,?,?)',
                               ((name, key, json.dumps(value, separators=(',', ':')))
                                for key, value in sorted(values.items())))


if __name__ == '__main__':
    compile_map(Path(sys.argv[1]), Path(sys.argv[2]))
