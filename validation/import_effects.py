"""Compare fresh pinned and patched import states in separate interpreters at the same path.

This diagnostic never removes tests. Opaque objects and external side effects cannot
be certified by a namespace comparison.
"""
import argparse
import hashlib
import importlib
import json
import marshal
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types


def snapshot(targets):
    """Inspect concrete namespaces without running application descriptors or repr."""
    for target in targets:
        importlib.import_module(target)
    values, aliases, opaque = {}, {}, set()
    classes = set()

    def atom(value, seen):
        kind = type(value)
        if kind in (bool, int, str, bytes, float, complex, type(None)):
            return ['atom', marshal.dumps(value).hex()]
        if isinstance(value, type):
            return ['class', type.__getattribute__(value, '__module__'),
                    type.__getattribute__(value, '__qualname__')]
        if kind is types.ModuleType:
            return ['module', vars(value).get('__name__')]
        if kind in (types.FunctionType, classmethod, staticmethod):
            fn = value if kind is types.FunctionType else value.__func__
            if not isinstance(fn, types.FunctionType):
                raise ValueError
            if id(fn) in seen or len(seen) >= 15:
                raise ValueError
            nested = seen | {id(fn)}
            return [kind.__name__, fn.__module__, fn.__qualname__,
                    hashlib.sha256(marshal.dumps(fn.__code__)).hexdigest(),
                    atom(fn.__defaults__, nested), atom(fn.__kwdefaults__, nested),
                    [atom(c.cell_contents, nested) for c in fn.__closure__ or ()]]
        if kind not in (tuple, list, set, frozenset, dict) or id(value) in seen or len(seen) >= 15:
            raise ValueError
        nested = seen | {id(value)}
        if kind is dict:
            content = [[atom(k, nested), atom(v, nested)] for k, v in value.items()]
        else:
            content = [atom(v, nested) for v in value]
            if kind in (set, frozenset):
                content.sort(key=json.dumps)
        return [kind.__name__, content]

    def inspect_namespace(owner, ns):
        for name, value in ns.items():
            if name.startswith('__') and name.endswith('__'):
                continue
            path = f'{owner}.{name}'
            if type(value) in (dict, list, set) or type(value) is types.FunctionType or isinstance(value, type):
                aliases.setdefault(id(value), []).append(path)
            try:
                signature = atom(value, set())
                values[path] = hashlib.sha256(json.dumps(signature, separators=(',', ':')).encode()).hexdigest()
            except (ValueError, TypeError, RecursionError):
                opaque.add(path)
            if isinstance(value, type) and id(value) not in classes:
                module = type.__getattribute__(value, '__module__')
                qualname = type.__getattribute__(value, '__qualname__')
                if path == f'{module}.{qualname}':
                    classes.add(id(value))
                    bases = type.__getattribute__(value, '__bases__')
                    values[path+'.__bases__'] = json.dumps([atom(base, set()) for base in bases])
                    inspect_namespace(path, type.__getattribute__(value, '__dict__'))

    for mod in sorted(sys.modules.copy()):
        if mod == 'sqlglot' or mod.startswith('sqlglot.'):
            inspect_namespace(mod, vars(sys.modules[mod]))
    groups = {p: sorted(group) for group in aliases.values() if len(group) > 1 for p in group}
    return dict(values=values, aliases=groups, opaque=sorted(opaque))


def compare(source, patch, modules):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from replay import overlay
    from selector import parse_patch, apply_section
    edits, baseline = {}, {}
    for part in parse_patch(patch.read_text()):
        name = part['old']
        if (not name or name != part['new'] or not name.startswith('sqlglot/')
                or not name.endswith('.py') or '..' in Path(name).parts or name in edits):
            raise ValueError('Expected distinct edits to existing SQLGlot modules')
        baseline[name] = (source/name).read_text()
        edits[name] = apply_section(baseline[name], part)[0]
    targets = modules or [name[:-3].replace('/', '.').removesuffix('.__init__') for name in edits]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)/'source'
        overlay(source, root, baseline)
        env = dict(os.environ, PYTHONHASHSEED='0', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(root))
        cmd = [sys.executable, str(Path(__file__).resolve()), '--capture', *targets]

        def capture():
            result = subprocess.run(cmd, cwd=root, env=env, check=True, capture_output=True, timeout=30)
            return json.loads(result.stdout)

        before = capture()
        for name, text in edits.items():
            path = root/name
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('Expected private overlay file')
            path.write_text(text)
        after = capture()
    out = {}
    for kind in ('values', 'aliases'):
        a, b = before[kind], after[kind]
        out[kind] = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    out['opaque'] = sorted(set(before['opaque']) | set(after['opaque']))
    return out


if __name__ == '__main__':
    if sys.argv[1:2] == ['--capture']:
        sys.path.insert(0, os.getcwd())
        print(json.dumps(snapshot(sys.argv[2:])))
    else:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('patch', type=Path)
        parser.add_argument('--source', type=Path, default=Path('/opt/sqlglot'))
        parser.add_argument('--module', action='append')
        args = parser.parse_args()
        print(json.dumps(compare(args.source.resolve(), args.patch, args.module)))
