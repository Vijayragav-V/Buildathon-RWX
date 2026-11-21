"""Find direct consumers of scalar class settings in the pinned sources."""
import ast
from collections import defaultdict


def reference_map(root, coverage):
    trees = []
    names = set()
    for folder in ('sqlglot', 'tests'):
        for path in sorted((root / folder).rglob('*.py')):
            tree = ast.parse(path.read_bytes())
            trees.append((str(path.relative_to(root)), tree))
            for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
                for stmt in cls.body:
                    if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                        targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                        for target in targets:
                            if isinstance(target, ast.Name) and target.id.isupper():
                                names.add(target.id)
    reads = defaultdict(set)
    excluded = set()
    for filename, tree in trees:
        class Visitor(ast.NodeVisitor):
            in_method = False
            def visit_FunctionDef(self, node):
                # Decorators/defaults execute in the surrounding scope.
                for part in node.decorator_list + [node.args]:
                    self.visit(part)
                previous, self.in_method = self.in_method, True
                for part in node.body:
                    self.visit(part)
                self.in_method = previous
            visit_AsyncFunctionDef = visit_FunctionDef
            def visit_ClassDef(self, node):
                previous, self.in_method = self.in_method, False
                self.generic_visit(node)
                self.in_method = previous
            def visit_Constant(self, node):
                if isinstance(node.value, str) and node.value in names:
                    excluded.add(node.value)
            def visit_Name(self, node):
                if isinstance(node.ctx, ast.Load) and node.id in names:
                    excluded.add(node.id)
            def visit_Attribute(self, node):
                if node.attr in names and isinstance(node.ctx, ast.Load):
                    if not self.in_method or filename not in coverage:
                        excluded.add(node.attr)
                    else:
                        hits = set()
                        for line in range(node.lineno, node.end_lineno + 1):
                            hits.update(coverage[filename].get(str(line), ()))
                        # An unmeasured access can be construction/import code or
                        # a continuation line. Keep normal class coverage in that case.
                        if hits:
                            reads[node.attr].update(hits)
                        else:
                            excluded.add(node.attr)
                self.generic_visit(node)
        Visitor().visit(tree)
    return {key: sorted(ids) for key, ids in reads.items() if key not in excluded}
