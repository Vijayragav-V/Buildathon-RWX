"""Baseline dependency selection; no patch identities or answer labels are used."""
from __future__ import annotations

import argparse
import ast
import json
import math
from pathlib import Path
import re
import sys

PIN = 'eb11aec4be8cb2122afe713a4816662c3f0fb2fa'
HUNK = re.compile(r'^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@')


def schedule(tests):
    """Try every allowed worker count with integer-millisecond LPT scheduling."""
    if not tests:
        return []
    ordered = sorted(tests, key=lambda t: (-math.floor(t['seconds'] * 1000 + .5), t['number']))
    best = None
    for count in range(1, min(8, len(tests)) + 1):
        workers, loads = [[] for _ in range(count)], [0] * count
        for test in ordered:
            i = min(range(count), key=lambda j: (loads[j], j))
            workers[i].append(test['number'])
            loads[i] += math.floor(test['seconds'] * 1000 + .5)
        workers = [w for w in workers if w]
        key = (max(loads) + 2000 * len(workers), len(workers))
        if best is None or key < best[0]:
            best = key, workers
    return best[1]


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


def safe_literal_edit(old, new, inside_call=False):
    """Do not narrow edits that can change import-time arithmetic or call arguments."""
    if ast.dump(old) == ast.dump(new):
        return True
    if type(old) is not type(new):
        return False
    if isinstance(old, ast.Constant):
        return type(old.value) is type(new.value) and (not inside_call or isinstance(old.value, bool))
    if isinstance(old, (ast.BinOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Subscript)):
        return False
    for field in old._fields:
        a, b = getattr(old, field), getattr(new, field)
        if isinstance(a, ast.AST):
            if not safe_literal_edit(a, b, inside_call or isinstance(old, ast.Call)):
                return False
        elif isinstance(a, list):
            if len(a) != len(b):
                return False
            for x, y in zip(a, b):
                if isinstance(x, ast.AST):
                    if not safe_literal_edit(x, y, inside_call or isinstance(old, ast.Call)):
                        return False
                elif x != y:
                    return False
        elif a != b:
            return False
    return True


def changed_classes(before, after, prefix=''):
    if len(before) != len(after):
        return None
    changed = set()
    for old, new in zip(before, after):
        if ast.dump(old) == ast.dump(new):
            continue
        if isinstance(old, ast.ClassDef) and isinstance(new, ast.ClassDef):
            if old.name != new.name or ast.dump(ast.Module(body=old.decorator_list + old.bases + old.keywords, type_ignores=[])) != ast.dump(ast.Module(body=new.decorator_list + new.bases + new.keywords, type_ignores=[])):
                return None
            inner = changed_classes(old.body, new.body, prefix + old.name + '.')
            if inner is None:
                return None
            changed.update(inner)
        elif prefix and isinstance(old, (ast.Assign, ast.AnnAssign)) and literal_shape(old) == literal_shape(new) and safe_literal_edit(old, new):
            changed.add(prefix[:-1])
        else:
            return None
    return changed


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
        self.uncertain.update(self.by_id[n] for n in self.by_id if n not in set(mapping['tests']))
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
        if observed_equivalent(tree, after, filename, self.mapping.get("identity", {})):
            return set(self.uncertain)
        if import_structure(tree) != import_structure(after):
            classes = changed_classes(tree.body, after.body)
            if not classes:
                return self.all
            selected = set(self.uncertain)
            for cls in classes:
                users = self.mapping.get('classes', {}).get(filename + ':' + cls)
                if not users:
                    return self.all
                selected.update(self.translate(users))
            return selected
        lines = self.mapping['lines'].get(filename, {})
        scopes = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))]
        selected = set()
        for lineno in changed:
            containing = [n for n in scopes if n.lineno <= lineno <= n.end_lineno]
            if not containing:
                return self.all
            scope = min(containing, key=lambda n: n.end_lineno - n.lineno)
            if not isinstance(scope, ast.Lambda) and lineno < scope.body[0].lineno:
                return self.all
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
    parser.add_argument('--map', type=Path, default=Path('/app/dependencies.json'))
    args = parser.parse_args()
    tests = json.loads((args.data / 'tests.json').read_text())['tests']
    try:
        selector = Selector(tests, json.loads(args.map.read_text()), args.source)
    except (OSError, ValueError, KeyError):
        selector = None
        print('Dependency map unavailable; selecting all tests.', file=sys.stderr)
    for patch in sorted((args.data / 'commits').glob('*.patch')):
        chosen = selector.select(patch.read_text()) if selector else {t['number'] for t in tests}
        workers = schedule([t for t in tests if t['number'] in chosen])
        print(json.dumps({'commit': patch.stem, 'workers': workers}, separators=(',', ':')))


if __name__ == '__main__':
    main()
