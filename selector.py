"""Baseline dependency selection; no patch identities or answer labels are used."""
from __future__ import annotations

import argparse
import ast
import json
import math
from pathlib import Path
import re
import sys
import sqlite3

from dependency_store import load as load_dependencies

PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def best_schedule(tests):
    """Find the best LPT schedule, pruning worker counts by a proven cost bound."""
    if not tests:
        return 0, []
    jobs = sorted(((math.floor(t['seconds'] * 1000 + .5), t['number']) for t in tests),
                  key=lambda job: (-job[0], job[1]))
    total = sum(duration for duration, _ in jobs)
    largest = jobs[0][0]
    best = None
    # A worker must finish the longest test, and one worker must carry at least
    # the mean load. Prefer fewer workers on equal cost, as in the original search.
    for count in range(1, min(8, len(jobs)) + 1):
        bound = max(largest, (total + count - 1) // count) + 2000 * count
        if best is not None and bound >= best[0]:
            continue
        workers, loads = [[] for _ in range(count)], [0] * count
        for duration, number in jobs:
            i = loads.index(min(loads))
            workers[i].append(number)
            loads[i] += duration
        workers = [w for w in workers if w]
        cost = max(loads) + 2000 * len(workers)
        if best is None or cost < best[0]:
            best = cost, workers
    return best


def schedule(tests):
    return best_schedule(tests)[1]


def parse_patch(text):
    """Parse file sections without treating header-like hunk contents as headers."""
    sections = []
    current = None
    hunk = None
    for line in text.splitlines(keepends=True):
        if line.startswith('diff --git '):
            current = {'old': None, 'new': None, 'hunks': []}
            sections.append(current)
            hunk = None
        elif current is not None:
            match = HUNK.match(line)
            if match:
                hunk = [int(match[1]), int(match[2] or 1), int(match[3]), int(match[4] or 1), []]
                current['hunks'].append(hunk)
            elif hunk is not None:
                if line.startswith((' ', '+', '-')):
                    hunk[4].append(line)
                elif line.startswith('\\ No newline'):
                    if hunk[4]:
                        hunk[4][-1] = hunk[4][-1].rstrip('\r\n')
                else:
                    raise ValueError('Unsupported patch hunk')
            elif line.startswith('--- '):
                current['old'] = line[4:].strip().removeprefix('a/')
            elif line.startswith('+++ '):
                current['new'] = line[4:].strip().removeprefix('b/')
    if not sections or any(not s['hunks'] for s in sections):
        raise ValueError('Unsupported or empty patch')
    return sections


def apply_section(source, section):
    original = source.splitlines(keepends=True)
    result, changed, cursor = [], set(), 0
    for start, old_count, new_start, new_count, body in section['hunks']:
        pos = start - 1 if old_count else start
        if pos < cursor or pos > len(original):
            raise ValueError('Invalid hunk offset')
        result.extend(original[cursor:pos])
        if (new_start - 1 if new_count else new_start) != len(result):
            raise ValueError('Invalid new hunk offset')
        old_used = new_used = 0
        replacing = False
        for line in body:
            kind, value = line[0], line[1:]
            if kind in ' -':
                if pos >= len(original) or original[pos] != value:
                    raise ValueError('Patch does not match pinned source')
                if kind == '-':
                    replacing = True
                    changed.add(pos + 1)
                else:
                    replacing = False
                    result.append(value)
                pos += 1
                old_used += 1
            else:
                # Include both sides of an insertion, including an empty function's boundary.
                if not replacing:
                    changed.update((max(1, pos), pos + 1))
                result.append(value)
            if kind in ' +':
                new_used += 1
        if (old_used, new_used) != (old_count, new_count):
            raise ValueError('Hunk length mismatch')
        cursor = pos
    result.extend(original[cursor:])
    return ''.join(result), changed


def inert_insertions(original_tree, modified, section, filename):
    """Verify that inserted statements disappear from the actual compiled program."""
    import marshal
    inserted = set()
    for _, _, new_start, _, body in section['hunks']:
        line = new_start
        for entry in body:
            if entry.startswith('-'):
                return False
            if entry.startswith('+'):
                inserted.add(line)
            line += 1
    if not inserted:
        return False

    class RemoveInserted(ast.NodeTransformer):
        def generic_visit(self, node):
            if isinstance(node, ast.stmt) and all(n in inserted for n in range(node.lineno, node.end_lineno + 1)):
                return None
            return super().generic_visit(node)

    tree = ast.parse(modified)
    try:
        actual = compile(tree, filename, 'exec', dont_inherit=True)
        # Preserve the patched coordinates on every surviving AST node. The
        # control program then differs only by the inserted statements.
        control = RemoveInserted().visit(tree)
        if ast.dump(control) != ast.dump(original_tree):
            return False
        reference = compile(control, filename, 'exec', dont_inherit=True)
        return marshal.dumps(actual) == marshal.dumps(reference)
    except (TypeError, ValueError, SyntaxError):
        return False


def import_structure(node):
    """Retain import-time expressions, but ignore deferred function/lambda bodies."""
    if isinstance(node, ast.AST):
        return (type(node).__name__, tuple(
            (field, import_structure(value)) for field, value in ast.iter_fields(node)
            if not (field == 'body' and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)))
        ))
    if isinstance(node, list):
        return tuple(import_structure(v) for v in node)
    return node


def literal_shape(node):
    if isinstance(node, ast.Constant):
        return ('Constant', type(node.value).__name__)
    if isinstance(node, ast.AST):
        return (type(node).__name__, tuple((k, literal_shape(v)) for k, v in ast.iter_fields(node)))
    if isinstance(node, list):
        return tuple(literal_shape(v) for v in node)
    return node


def safe_literal_edit(old, new):
    """Do not narrow edits that can change import-time arithmetic or call arguments."""
    if ast.dump(old) == ast.dump(new):
        return True
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Constant):
        return type(old.value) is type(new.value)
    if isinstance(old, (ast.Call, ast.BinOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Subscript)):
        return False
    for field in old._fields:
        a, b = getattr(old, field), getattr(new, field)
        if isinstance(a, ast.AST):
            if not safe_literal_edit(a, b):
                return False
        elif isinstance(a, list):
            if len(a) != len(b):
                return False
            for x, y in zip(a, b):
                if isinstance(x, ast.AST):
                    if not safe_literal_edit(x, y):
                        return False
                elif x != y:
                    return False
        elif a != b:
            return False
    return True


def same_literal_keys(old, new):
    if not isinstance(old, ast.Dict) or not isinstance(new, ast.Dict):
        return False
    if any(k is None for k in old.keys + new.keys):
        return False
    try:
        ast.literal_eval(old)
        ast.literal_eval(new)
    except (ValueError, TypeError):
        return False
    return [ast.dump(k) for k in old.keys] == [ast.dump(k) for k in new.keys]


def same_header(old, new, omit):
    if type(old) is not type(new):
        return False
    def freeze(value):
        if isinstance(value, ast.AST):
            return ast.dump(value)
        if isinstance(value, list):
            return tuple(freeze(item) for item in value)
        return value
    return all(freeze(getattr(old, key)) == freeze(getattr(new, key)) for key in old._fields if key != omit)


def only_generator_tail(old, new):
    """A literal yield appended to an existing generator cannot change its calling convention."""
    if len(new.body) <= len(old.body):
        return False

    def yields_in_block(stmts):
        for stmt in stmts:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Yield, ast.YieldFrom)):
                return True
            if isinstance(stmt, (ast.If, ast.For, ast.While)):
                if yields_in_block(stmt.body) or yields_in_block(stmt.orelse):
                    return True
        return False

    tail = new.body[len(old.body):]
    return (all(ast.dump(a) == ast.dump(b) for a, b in zip(old.body, new.body))
            and yields_in_block(old.body)
            and all(isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Yield)
                    and (stmt.value.value is None or isinstance(stmt.value.value, ast.Constant))
                    for stmt in tail))


def changed_classes(before, after, bodies, prefix=''):
    if len(before) != len(after):
        return None
    changed = set()
    for old, new in zip(before, after):
        if ast.dump(old) == ast.dump(new):
            continue
        if isinstance(old, ast.ClassDef) and isinstance(new, ast.ClassDef):
            if not same_header(old, new, 'body'):
                return None
            inner = changed_classes(old.body, new.body, bodies, prefix + old.name + '.')
            if inner is None:
                return None
            changed.update(inner)
        elif isinstance(old, (ast.FunctionDef, ast.AsyncFunctionDef)) and same_header(old, new, 'body'):
            # A generator changes its caller's result type before its body runs.
            if (old.decorator_list or ast.get_docstring(old) != ast.get_docstring(new)
                    or (any(isinstance(n, (ast.Yield, ast.YieldFrom))
                            for fn in (old, new) for n in ast.walk(fn))
                        and not only_generator_tail(old, new))):
                return None
            bodies.append(old)
        elif isinstance(old, (ast.Assign, ast.AnnAssign)) and literal_shape(old) == literal_shape(new) and safe_literal_edit(old, new):
            if not same_header(old, new, 'value'):
                return None
            targets = old.targets if isinstance(old, ast.Assign) else [old.target]
            value = old.value
            if isinstance(value, ast.UnaryOp) and isinstance(value.op, (ast.UAdd, ast.USub)):
                value = value.operand
            simple = len(targets) == 1 and isinstance(targets[0], ast.Name) and isinstance(value, ast.Constant)
            dictionary = same_literal_keys(old.value, new.value)
            if not prefix and not (simple or dictionary):
                return None
            field = targets[0].id if len(targets) == 1 and isinstance(targets[0], ast.Name) and (simple or dictionary) else None
            changed.add((prefix[:-1], field, dictionary))
        else:
            return None
    return changed


def deferred_changes(old, new, filename, index, readers):
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Call):
        hits = index.get(f'{filename}:{old.lineno}:{old.col_offset}')
        if hits is not None and ast.dump(old.func) == ast.dump(new.func):
            old_args = old.args + [kw.value for kw in old.keywords]
            new_args = new.args + [kw.value for kw in new.keywords]
            if (len(old.args) == len(new.args) and [k.arg for k in old.keywords] == [k.arg for k in new.keywords]
                    and all(ast.dump(a) == ast.dump(b) or
                            isinstance(a, ast.Constant) and isinstance(b, ast.Constant)
                            and type(a.value) is bool and type(b.value) is bool
                            for a, b in zip(old_args, new_args))):
                if ast.dump(old) != ast.dump(new):
                    readers.update(hits)
                return True
    if isinstance(old, ast.AST):
        return all(deferred_changes(getattr(old, key), getattr(new, key), filename, index, readers)
                   for key in old._fields)
    if isinstance(old, list):
        return len(old) == len(new) and all(deferred_changes(a, b, filename, index, readers)
                                            for a, b in zip(old, new))
    return old == new


def observed_equivalent(old, new, filename, observations):
    if isinstance(old, ast.Compare) and isinstance(new, ast.Compare):
        if len(old.ops) == len(new.ops) == 1:
            pair = (type(old.ops[0]), type(new.ops[0]))
            if pair in ((ast.Is, ast.Eq), (ast.IsNot, ast.NotEq)):
                key = f'{filename}:{old.lineno}:{old.col_offset}'
                return (observations.get(key) is True
                        and ast.dump(old.left) == ast.dump(new.left)
                        and [ast.dump(n) for n in old.comparators] == [ast.dump(n) for n in new.comparators])
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.AST):
        return all(observed_equivalent(getattr(old, field), getattr(new, field), filename, observations)
                   for field in old._fields)
    if isinstance(old, list):
        return len(old) == len(new) and all(observed_equivalent(a, b, filename, observations) for a, b in zip(old, new))
    return old == new


class Selector:
    def __init__(self, tests, mapping, source):
        self.tests = tests
        self.all = {t['number'] for t in tests}
        self.source = source.resolve()
        self.mapping = mapping
        if mapping.get('commit') != PIN:
            raise ValueError('Dependency map has wrong baseline')
        self.by_id = {t['id']: t['number'] for t in tests}
        self.index = {i: self.by_id[nodeid] for i, nodeid in enumerate(mapping['tests']) if nodeid in self.by_id}
        self.uncertain = {self.by_id[n] for n in mapping.get('uncertain', []) if n in self.by_id}
        self.uncertain.update(self.by_id[n] for n in self.by_id.keys() - set(mapping['tests']))
        self.cache = {}

    def translate(self, indices):
        return {self.index[i] for i in indices if i in self.index}

    def baseline(self, filename):
        if filename not in self.cache:
            path = (self.source / filename).resolve()
            if not path.is_relative_to(self.source):
                raise ValueError('Unsafe path')
            text = path.read_text()
            tree = ast.parse(text) if filename.endswith('.py') else None
            self.cache[filename] = text, tree
        return self.cache[filename]

    def body_users(self, filename, bodies):
        coverage = self.mapping['lines'].get(filename, {})
        users = set()
        for body in bodies:
            key = f'{filename}:{body.name}:{body.lineno}'
            if key not in self.mapping.get('runtime_bodies', {}):
                return self.all
            hits = {i for line in range(body.lineno, body.end_lineno + 1)
                    for i in coverage.get(str(line), ())}
            if not hits:
                return self.all
            if any(isinstance(n, (ast.Yield, ast.YieldFrom)) for n in ast.walk(body)):
                observed = self.mapping.get('generator_users', {}).get(key)
                if observed is None:
                    return self.all
                hits.update(observed)
            users.update(self.translate(hits))
        return users

    def select_section(self, section):
        filename = section['old']
        if not filename or filename != section['new'] or filename == '/dev/null':
            return self.all
        original, tree = self.baseline(filename)
        modified, changed = apply_section(original, section)
        if tree is None:
            readers = self.mapping['reads'].get(filename)
            return (self.translate(readers) | self.uncertain) if readers else self.all
        try:
            after = ast.parse(modified)
        except (SyntaxError, ValueError):
            return self.all
        # Compare complete files, including string literals and docstrings. Mixed patches
        # still process every section; SQL fixture comments are never discarded.
        if ast.dump(tree, include_attributes=False) == ast.dump(after, include_attributes=False):
            return set()
        child = self.translate(self.mapping.get('child_imports', {}).get(filename, ()))
        equivalent = observed_equivalent(tree, after, filename, self.mapping.get("identity", {}))
        selected = set(self.uncertain)
        if import_structure(tree) != import_structure(after):
            if equivalent:
                return selected | child
            bodies = []
            classes = changed_classes(tree.body, after.body, bodies)
            if not classes:
                readers = set()
                if deferred_changes(tree, after, filename, self.mapping.get('deferred_calls', {}), readers):
                    return selected | child | self.translate(readers)
                return self.all
            selected.update(child)
            for cls, field, dictionary in classes:
                if not cls:
                    readers = self.mapping.get('private_globals', {}).get(f'{filename}:{field}')
                    if readers is None:
                        return self.all
                    selected.update(self.translate(readers))
                    continue
                # Class initialization and direct class-field reads are independent.
                # A cached instance or a classmethod can read a setting without
                # constructing the class in the current test.
                index = 'dictionary_values' if dictionary else 'scalar_readers'
                readers = self.mapping.get(index, {}).get(field)
                if readers is None:
                    return self.all
                selected.update(self.translate(readers))
            return selected | self.body_users(filename, bodies)
        lines = self.mapping['lines'].get(filename, {})
        scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
        entered = self.mapping.get('entered', {}).get(filename)
        early = self.mapping.get('import_calls', {}).get(filename)
        for lineno in changed:
            containing = [n for n in scopes if n.lineno <= lineno <= n.end_lineno]
            if not containing:
                # An insertion can straddle the old final statement and a blank
                # line outside its body. Attribute it using the complete new AST.
                bodies = []
                declarations = changed_classes(tree.body, after.body, bodies)
                if declarations == set() and bodies:
                    if inert_insertions(tree, modified, section, filename):
                        return self.uncertain | child
                    return selected | child | self.body_users(filename, bodies)
                return self.all
            # An import/collection call can initialize shared objects before
            # any test owns a coverage context. Narrow line hits cannot bound
            # its consumers. Include enclosing scopes when a line is ambiguous.
            scope = min(containing, key=lambda n: n.end_lineno - n.lineno)
            # Import-time factories often define callbacks without calling them.
            # A lambda boundary can share its line with the containing expression.
            ambiguous = isinstance(scope, ast.Lambda) and lineno in (scope.lineno, scope.end_lineno)
            for fn in containing if ambiguous else (scope,):
                name = '<lambda>' if isinstance(fn, ast.Lambda) else fn.name
                first = min([fn.lineno] + [d.lineno for d in getattr(fn, 'decorator_list', ())])
                if early is None:
                    if f'{filename}:{name}:{fn.lineno}' not in self.mapping.get('runtime_bodies', {}):
                        return self.all
                elif f'{name}:{first}' in early:
                    return self.all
            if equivalent:
                selected.update(child)
                continue
            if not isinstance(scope, ast.Lambda) and lineno < scope.body[0].lineno:
                return self.all
            if entered is not None and isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                first = min([scope.lineno] + [d.lineno for d in scope.decorator_list])
                if f'{scope.name}:{first}' not in entered:
                    selected.update(child)
                    continue
            # Cover the whole expression, not the entire function. A multiline
            # condition may emit a trace event only on its first physical line.
            expressions = [n for n in ast.walk(scope) if isinstance(n, ast.expr)
                           and n.lineno <= lineno <= n.end_lineno]
            expression = max(expressions, key=lambda n: n.end_lineno - n.lineno, default=None)
            span = range(expression.lineno, expression.end_lineno + 1) if expression else [lineno]
            affected = set()
            for line in span:
                affected.update(lines.get(str(line), ()))
            if not affected:
                for line in range(scope.lineno, scope.end_lineno + 1):
                    affected.update(lines.get(str(line), ()))
            if not affected:
                return self.all
            selected.update(self.translate(affected))
        return selected | self.uncertain

    def select(self, patch):
        selected = set()
        try:
            for section in parse_patch(patch):
                selected.update(self.select_section(section))
        except (ValueError, OSError, SyntaxError, KeyError, TypeError, UnicodeError):
            return self.all
        return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, default=Path('/data'))
    parser.add_argument('--source', type=Path, default=Path('/opt/sqlglot'))
    parser.add_argument('--map', type=Path, default=Path('/app/dependencies.sqlite'))
    args = parser.parse_args()
    tests = json.loads((args.data / 'tests.json').read_text())['tests']
    try:
        selector = Selector(tests, load_dependencies(args.map), args.source)
    except (OSError, ValueError, KeyError, sqlite3.DatabaseError):
        selector = None
        print('Dependency map unavailable; selecting all tests.', file=sys.stderr)
    from replay import ReplayBudget
    patches = sorted((args.data / 'commits').glob('*.patch'))
    budget = ReplayBudget(len(patches))
    by_number = {t['number']: t for t in tests}
    pending = []
    for patch in patches:
        change = patch.read_text()
        try:
            chosen = selector.select(change) if selector else set(by_number)
            proposal = budget.proposal(selector, chosen, by_number) if selector else None
        except sqlite3.DatabaseError:
            chosen, proposal = set(by_number), None
        priority = proposal[0] if proposal else 0.
        pending.append((-priority, patch.name, patch, change, chosen, proposal))
    for _, _, patch, change, chosen, proposal in sorted(pending):
        if proposal:
            chosen = budget.refine(selector, chosen, change, by_number, proposal)
        workers = schedule([t for t in tests if t['number'] in chosen])
        print(json.dumps({'commit': patch.stem, 'workers': workers}, separators=(',', ':')))


if __name__ == '__main__':
    main()
