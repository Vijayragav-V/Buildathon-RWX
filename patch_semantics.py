"""Compare paired syntax trees while sharing the proof for unchanged source declarations."""
import ast
import copy
import marshal
import warnings
from types import CodeType


class SourceComparison:
    def __init__(self, old, new):
        self.old = old.split('\n')
        self.new = new.split('\n')

    def same_statement(self, left, right):
        if type(left) is not type(right) or not isinstance(left, ast.stmt):
            return False
        if (left.col_offset, left.end_col_offset) != (right.col_offset, right.end_col_offset):
            return False
        # Decorator expressions can begin before the definition's lineno.
        a = getattr(left, 'decorator_list', ())
        b = getattr(right, 'decorator_list', ())
        if (a or b) and [ast.dump(x) for x in a] != [ast.dump(x) for x in b]:
            return False
        return self.old[left.lineno-1:left.end_lineno] == self.new[right.lineno-1:right.end_lineno]

    def differences(self, left, right):
        scopes = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        if type(left) is not type(right) or not isinstance(left, scopes):
            return left, right
        if len(left.body) != len(right.body):
            return left, right
        a, b = copy.copy(left), copy.copy(right)
        a.body, b.body = [], []
        for x, y in zip(left.body, right.body):
            if not self.same_statement(x, y):
                x, y = self.differences(x, y)
                a.body.append(x)
                b.body.append(y)
        return a, b


def constant_aliases(code):
    # Current marshal formats encode reference-count-dependent optimization flags.
    # Preserve actual sharing explicitly, including nested constants and code objects.
    seen = {}
    pattern = []

    def visit(value):
        key = id(value)
        previous = seen.get(key)
        if previous is not None:
            pattern.append(previous)
            return
        seen[key] = len(seen)
        pattern.append(seen[key])
        if isinstance(value, CodeType):
            visit(value.co_consts)
        elif type(value) is tuple:
            for element in value:
                visit(element)
        elif type(value) is frozenset:
            for element in sorted(value, key=lambda v: marshal.dumps(v, 2)):
                visit(element)

    visit(code)
    return pattern


def same_compiled_module(old, new, filename):
    """Include code objects, constant types, names, exception tables and source positions."""
    if len(old.encode()) != len(new.encode()) or old.count('\n') != new.count('\n'):
        return False
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            a = compile(old, filename, 'exec', dont_inherit=True)
            b = compile(new, filename, 'exec', dont_inherit=True)
        return (marshal.dumps(a, 2) == marshal.dumps(b, 2)
                and constant_aliases(a) == constant_aliases(b))
    except (SyntaxError, ValueError, Warning, OverflowError):
        return False
