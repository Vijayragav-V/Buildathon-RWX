"""Build conservative read-site indexes for literal data in the pinned checkout."""
import ast
from collections import defaultdict
import json
from pathlib import Path
import sys


def name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def literal(node):
    try:
        ast.literal_eval(node)
        return True
    except (TypeError, ValueError, SyntaxError):
        return False


class Reads:
    def __init__(self, root, mapping, early=None):
        self.early = early or {}
        self.mapping = mapping
        self.uses = defaultdict(list)
        self.strings = set()
        self.assignments = []
        self.constructors = set()
        self.imports = {}
        self.global_names = defaultdict(set)
        for folder in ('sqlglot', 'tests'):
            for path in sorted((root / folder).rglob('*.py')):
                tree = ast.parse(path.read_bytes())
                file = path.relative_to(root).as_posix()
                module = file[:-3].replace('/', '.').removesuffix('.__init__')
                self.imports[file] = {}
                for top in tree.body:
                    if isinstance(top, ast.ImportFrom) and top.level == 0:
                        for alias in top.names:
                            self.imports[file][alias.asname or alias.name] = (top.module, alias.name)
                    if isinstance(top, ast.ClassDef) and not top.decorator_list and not top.keywords:
                        args = [n.args for n in top.body if isinstance(n, ast.FunctionDef) and n.name == '__init__']
                        if len(args) == 1:
                            for arg in args[0].args + args[0].kwonlyargs:
                                self.constructors.add((module, top.name, arg.arg))
                    if isinstance(top, (ast.Assign, ast.AnnAssign)) and top.value and literal(top.value):
                        for target in top.targets if isinstance(top, ast.Assign) else [top.target]:
                            if isinstance(target, ast.Name) and target.id.startswith('_') and target.id[1:].isupper():
                                self.global_names[file].add(target.id)
                parent = {child:n for n in ast.walk(tree) for child in ast.iter_child_nodes(n)}
                for node in ast.walk(tree):
                    if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                        self.uses[name(node)].append((file, node, parent))
                    elif isinstance(node, ast.Constant) and type(node.value) is str:
                        container = parent.get(node)
                        assignment = parent.get(container)
                        slots = (isinstance(container, (ast.Tuple, ast.List)) and isinstance(assignment, ast.Assign)
                                 and any(name(t) == '__slots__' for t in assignment.targets))
                        if not slots:
                            self.strings.add(node.value)
                    elif isinstance(node, ast.ImportFrom):
                        self.strings.update(a.name for a in node.names)
                    elif isinstance(node, ast.ClassDef):
                        for stmt in node.body:
                            if isinstance(stmt, (ast.Assign, ast.AnnAssign)) and isinstance(stmt.value, ast.Dict) and literal(stmt.value):
                                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                                self.assignments.extend(t.id for t in targets if isinstance(t, ast.Name) and t.id.isupper())

    def site(self, file, node, parent):
        # A collection-time consumer can propagate values to every later test.
        fn = node
        while fn in parent and not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn = parent[fn]
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.lineno <= fn.lineno:
            return None
        # No hit is ambiguous (untraced import-time execution, multiline bytecode,
        # or an uncaptured reader). Fall back instead of treating it as zero tests.
        lines = self.mapping['lines'].get(file, {})
        hits = {i for n in range(node.lineno, node.end_lineno+1) for i in lines.get(str(n), ())}
        positions = set(range(node.lineno, node.end_lineno+1))
        if positions & self.early.get(file, set()):
            return None
        children = set(self.mapping.get('child_imports', {}).get(file, ()))
        if hits:
            return hits | children
        if fn.decorator_list:
            return None
        entered = self.mapping.get('entered', {}).get(file)
        if entered is not None and f'{fn.name}:{fn.lineno}' not in entered:
            return children
        return None

    def dictionary(self, field):
        pending, seen, readers = [field], set(), set()
        while pending:
            variable = pending.pop()
            if variable in seen:
                continue
            seen.add(variable)
            if len(seen) > 5 or variable in self.strings:
                return None
            for file, node, parents in self.uses[variable]:
                p = parents[node]
                if isinstance(p, (ast.Assign, ast.AnnAssign)) and p.value is node:
                    targets = p.targets if isinstance(p, ast.Assign) else [p.target]
                    for target in targets:
                        if name(target) is None or name(target).lower() != field.lower():
                            return None
                        pending.append(name(target))
                    continue
                if isinstance(p, ast.keyword):
                    call = parents[p]
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name):
                        origin = self.imports[file].get(call.func.id)
                        if origin and p.arg and (*origin, p.arg) in self.constructors and p.arg.lower() == field.lower():
                            pending.append(p.arg)
                            continue
                    return None
                if isinstance(p, ast.Compare) and len(p.ops)==1 and isinstance(p.ops[0], (ast.In, ast.NotIn)) and p.comparators[0] is node:
                    continue
                # Truth-only contexts preserve the result for identical key sets.
                expr = node
                while isinstance(parents.get(expr), ast.BoolOp):
                    expr = parents[expr]
                context = parents.get(expr)
                if (isinstance(context, (ast.If, ast.While, ast.IfExp)) and context.test is expr
                        or isinstance(context, ast.UnaryOp) and isinstance(context.op, ast.Not)
                        or isinstance(context, ast.Call) and isinstance(context.func, ast.Name) and context.func.id in ('bool','len') and context.args == [expr]):
                    continue
                if isinstance(p, ast.Subscript) and p.value is node or isinstance(p, ast.Attribute) and p.attr in ('get','items','values') and isinstance(parents[p], ast.Call):
                    site = self.site(file, p, parents)
                    if site is None:
                        return None
                    readers.update(site)
                else:
                    return None
        return sorted(readers) if readers else None

    def globals(self):
        out = {}
        for filename, names in self.global_names.items():
            for field in names - self.strings:
                uses = self.uses[field]
                readers = set()
                for file, node, parents in uses:
                    if file != filename or not isinstance(node, ast.Name):
                        break
                    hits = self.site(file, node, parents)
                    if hits is None:
                        break
                    readers.update(hits)
                else:
                    if readers:
                        out[filename + ':' + field] = sorted(readers)
        return out


def collection_lines(root):
    import contextlib
    import coverage
    import os
    import pytest
    import importlib
    # Catch import-time readers, including lazy dialect imports that may be
    # initialized before any individual test starts.
    sys.path.insert(0, str(root))
    os.chdir(root)
    cov = coverage.Coverage(config_file=False, data_file=None, source=[str(root/'sqlglot'), str(root/'tests')])
    with open(os.devnull, 'w') as sink, contextlib.redirect_stdout(sink):
        cov.start()
        for path in sorted((root/'sqlglot').rglob('*.py')):
            if path.name != '__main__.py':
                module = '.'.join(path.relative_to(root).with_suffix('').parts).removesuffix('.__init__')
                importlib.import_module(module)
        rc = pytest.main(['tests', '--collect-only', '-q', '-p', 'no:cacheprovider'])
        cov.stop()
    if rc != 0:
        return None
    data = cov.get_data()
    return {str(Path(p).relative_to(root)): set(data.lines(p)) for p in data.measured_files()}


def build(root, mapping, early):
    if early is None:
        return {}
    index = Reads(root, mapping, early)
    data = {f:r for f in set(index.assignments) if (r := index.dictionary(f)) is not None}
    return {'dictionary_values': data, 'private_globals': index.globals()}


if __name__ == '__main__':
    source, previous, output = map(Path, sys.argv[1:])
    mapping = json.loads(previous.read_text())
    output = output.resolve(); source = source.resolve()
    extra = build(source, mapping, collection_lines(source))
    mapping.update(extra)
    output.write_text(json.dumps(mapping, separators=(',', ':')))
