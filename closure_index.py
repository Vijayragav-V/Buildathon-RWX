"""Index dispatch callbacks created by side-effect-free factories on the pinned baseline."""
import ast
import importlib
import json
from pathlib import Path
import sys
from types import FunctionType, ModuleType


def captures_only(factory):
    """Creating the callback must only bind arguments, with no eager user code."""
    if factory.decorator_list or getattr(factory, 'type_params', ()):
        return None
    body = list(factory.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body.pop(0)
    if len(body) == 2 and isinstance(body[0], ast.FunctionDef) and isinstance(body[1], ast.Return):
        inner = body[0]
        if not isinstance(body[1].value, ast.Name) or body[1].value.id != inner.name:
            return None
        if inner.decorator_list or getattr(inner, 'type_params', ()):
            return None
    elif len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Lambda):
        inner = body[0].value
    else:
        return None
    if inner.args.defaults or any(x is not None for x in inner.args.kw_defaults):
        return None
    return inner


def dispatch_key(node, module):
    if isinstance(node, ast.Constant) and type(node.value) is str:
        return node.value
    if isinstance(node, ast.Name):
        return vars(module).get(node.id)
    if isinstance(node, ast.Attribute):
        parent = dispatch_key(node.value, module)
        if isinstance(parent, (ModuleType, type)):
            return vars(parent).get(node.attr)
    return None


def build(root, mapping):
    sys.path.insert(0, str(root))
    # A consumer inspecting closure cells could distinguish callbacks before invocation.
    for folder in ('sqlglot', 'tests'):
        for f in (root/folder).rglob('*.py'):
            s = f.read_text()
            if any(word in s for word in ('__closure__', 'getclosurevars', 'cloudpickle', 'dill.dumps')):
                return {}
    modules = {}
    factories = {}
    for path in sorted((root/'sqlglot').rglob('*.py')):
        if path.stem == '__main__':
            continue
        filename = path.relative_to(root).as_posix()
        tree = ast.parse(path.read_bytes())
        modname = filename[:-3].replace('/', '.').removesuffix('.__init__')
        module = importlib.import_module(modname)
        modules[filename] = tree, module
        # On 3.13 an evaluated annotation may itself read a factory argument.
        postponed = any(isinstance(n, ast.ImportFrom) and n.module == '__future__'
                        and any(a.name == 'annotations' for a in n.names) for n in tree.body)
        for fn in tree.body:
            if not isinstance(fn, ast.FunctionDef) or not postponed:
                continue
            inner = captures_only(fn)
            obj = vars(module).get(fn.name)
            if not inner or not isinstance(obj, FunctionType) or (Path(obj.__code__.co_filename), obj.__code__.co_firstlineno) != (path, fn.lineno):
                continue
            name = inner.name if isinstance(inner, ast.FunctionDef) else '<lambda>'
            key = f'{name}:{inner.lineno}'
            early = mapping.get('import_calls', {}).get(filename)
            if early is None or key in early:
                continue
            lines = mapping['lines'].get(filename, {})
            first = inner.body[0].lineno if isinstance(inner, ast.FunctionDef) else inner.lineno
            readers = {i for n in range(first, inner.end_lineno + 1) for i in lines.get(str(n), ())}
            entered = mapping.get('entered', {}).get(filename)
            if not readers and (entered is None or key in entered):
                continue
            readers.update(mapping.get('child_imports', {}).get(filename, ()))
            factories[obj] = filename, name, inner.lineno, sorted(readers)

    output = {}
    for filename, (tree, module) in modules.items():
        for cls in (x for x in tree.body if isinstance(x, ast.ClassDef)):
            real = vars(module).get(cls.name)
            if not isinstance(real, type):
                continue
            local_names = {n.id for stmt in cls.body for n in ast.walk(stmt)
                           if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
            local_names.update(n.name for n in cls.body if isinstance(n, (ast.ClassDef, ast.FunctionDef)))
            if any(isinstance(n, (ast.Import, ast.ImportFrom)) for n in cls.body):
                continue
            for stmt in cls.body:
                if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not isinstance(stmt.value, ast.Dict):
                    continue
                target = stmt.targets[0] if isinstance(stmt, ast.Assign) else stmt.target
                table = vars(real).get(target.id) if isinstance(target, ast.Name) else None
                if type(table) is not dict:
                    continue
                for k, call in zip(stmt.value.keys, stmt.value.values):
                    if not isinstance(call, ast.Call):
                        continue
                    if (not isinstance(call.func, ast.Name) or call.func.id in local_names
                            or any(kw.arg is None for kw in call.keywords)):
                        continue
                    values = [*call.args, *(kw.value for kw in call.keywords)]
                    if not all(isinstance(x, ast.Constant) for x in values):
                        continue
                    factory = vars(module).get(call.func.id)
                    evidence = factories.get(factory) if isinstance(factory, FunctionType) else None
                    key = dispatch_key(k, module)
                    callback = table.get(key) if isinstance(key, (str, type)) else None
                    if not evidence or not isinstance(callback, FunctionType):
                        continue
                    source, name, first, readers = evidence
                    code = callback.__code__
                    if (Path(code.co_filename) == root/source and code.co_name == name
                            and code.co_firstlineno == first):
                        output[f'{filename}:{call.lineno}:{call.col_offset}'] = readers
    return output


if __name__ == '__main__':
    root, dest = (Path(a).resolve() for a in sys.argv[1:])
    mapping = json.loads(dest.read_text())
    mapping['deferred_calls'] = build(root, mapping)
    dest.write_text(json.dumps(mapping, separators=(',', ':')))
