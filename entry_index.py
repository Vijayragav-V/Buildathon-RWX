"""Index positive evidence for literal-key reads of a pinned class setting."""
import ast
from collections import defaultdict


def build(root, mapping):
    fields = {name.lower(): name for name in mapping.get('dictionary_values', {})}
    found = defaultdict(lambda: defaultdict(set))
    for filename, lines in mapping['lines'].items():
        path = root / filename
        if path.suffix != '.py' or not path.is_file():
            continue
        for node in ast.walk(ast.parse(path.read_bytes())):
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
                owner, key = node.value, node.slice
            elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and node.func.attr == 'get' and node.args):
                owner, key = node.func.value, node.args[0]
            else:
                continue
            if not isinstance(key, ast.Constant) or type(key.value) is not str:
                continue
            name = getattr(owner, 'id', getattr(owner, 'attr', '')).lower()
            if name in fields:
                hits = found[fields[name]][key.value]
                for lineno in range(node.lineno, node.end_lineno + 1):
                    hits.update(lines.get(str(lineno), ()))
    return {field: {key: sorted(ids) for key, ids in entries.items() if ids}
            for field, entries in found.items()}


def changed_keys(before, after, owner, field):
    for name in owner.split('.'):
        a = [n for n in before.body if isinstance(n, ast.ClassDef) and n.name == name]
        b = [n for n in after.body if isinstance(n, ast.ClassDef) and n.name == name]
        if len(a) != 1 or len(b) != 1:
            return ()
        before, after = a[0], b[0]

    def entries(cls):
        writes = [n.value for n in cls.body if isinstance(n, (ast.Assign, ast.AnnAssign))
                  and any(isinstance(t, ast.Name) and t.id == field for t in
                          (n.targets if isinstance(n, ast.Assign) else [n.target]))]
        if len(writes) != 1 or not isinstance(writes[0], ast.Dict):
            return None
        result = {}
        for k, v in zip(writes[0].keys, writes[0].values):
            if not isinstance(k, ast.Constant) or type(k.value) is not str or k.value in result:
                return None
            result[k.value] = ast.dump(v)
        return result

    a, b = entries(before), entries(after)
    if a is None or b is None or list(a) != list(b):
        return ()
    return tuple(k for k in a if a[k] != b[k])
