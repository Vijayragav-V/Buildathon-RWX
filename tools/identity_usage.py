"""Observe whether identity comparisons use identity-only equality types."""
import ast
import importlib.abc
import importlib.machinery
import sys


PURE_EQ = (object.__eq__, str.__eq__, int.__eq__, float.__eq__, bytes.__eq__, type(None).__eq__)
PURE_NE = (object.__ne__, str.__ne__, int.__ne__, float.__ne__, bytes.__ne__, type(None).__ne__)


def comparison_equivalent(left, right):
    safe = (type(left).__eq__ in PURE_EQ and type(right).__eq__ in PURE_EQ
            and type(left).__ne__ in PURE_NE and type(right).__ne__ in PURE_NE)
    return safe and ((left == right) == (left is right)) and ((left != right) == (left is not right))


def install(root, collector):
    def observe(left, right, key, negate):
        if collector.identity.get(key) is False:
            return (left is not right) if negate else (left is right)
        safe = comparison_equivalent(left, right)
        collector.identity[key] = collector.identity.get(key, True) and safe
        return (left is not right) if negate else (left is right)

    class Transform(ast.NodeTransformer):
        def __init__(self, filename):
            self.filename = filename

        def visit_Compare(self, node):
            self.generic_visit(node)
            if len(node.ops) == 1 and isinstance(node.ops[0], (ast.Is, ast.IsNot)):
                key = f'{self.filename}:{node.lineno}:{node.col_offset}'
                replacement = ast.Call(func=ast.Name(id='__blast_identity__', ctx=ast.Load()),
                    args=[node.left, node.comparators[0], ast.Constant(key), ast.Constant(isinstance(node.ops[0], ast.IsNot))], keywords=[])
                return ast.copy_location(replacement, node)
            return node

    class Loader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            filename = self.get_filename(fullname)
            from pathlib import Path
            relative = str(Path(filename).resolve().relative_to(root))
            tree = ast.parse(self.get_data(filename), filename)
            tree = ast.fix_missing_locations(Transform(relative).visit(tree))
            return compile(tree, filename, 'exec')

        def exec_module(self, module):
            module.__dict__['__blast_identity__'] = observe
            super().exec_module(module)

    class Finder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != 'sqlglot' and not fullname.startswith('sqlglot.'):
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec and isinstance(spec.loader, importlib.machinery.SourceFileLoader):
                spec.loader = Loader(fullname, spec.origin)
            return spec
    sys.meta_path.insert(0, Finder())
