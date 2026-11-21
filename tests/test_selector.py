import ast
import difflib
from pathlib import Path
import tempfile
import unittest

from selector import Selector, apply_section, only_generator_tail, parse_patch, schedule, PIN


def patch(old, new, filename='sqlglot/example.py'):
    return f'diff --git a/{filename} b/{filename}\n' + ''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), fromfile='a/'+filename, tofile='b/'+filename))


class SelectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root/'sqlglot').mkdir()
        self.source = 'VALUE = 2\n\ndef f(x):\n    if x:\n        return 1\n    return 0\n\ndef g():\n    return 3\n'
        (self.root/'sqlglot/example.py').write_text(self.source)
        self.tests = [{'number': 7, 'id': 'tests/a.py::test_a', 'seconds': .1}, {'number': 19, 'id': 'tests/a.py::test_b', 'seconds': .2}]
        self.mapping = {'commit': PIN, 'tests': [t['id'] for t in self.tests], 'lines': {'sqlglot/example.py': {'4': [0], '5': [0], '6': [0], '9': [1]}}, 'reads': {}, 'uncertain': [], 'import_calls': {'sqlglot/example.py': []}}
        self.selector = Selector(self.tests, self.mapping, self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_function_scope_and_renumbered_tests(self):
        self.assertEqual(self.selector.select(patch(self.source, self.source.replace('return 1', 'return 4'))), {7})

    def test_import_time_assignment_is_conservative(self):
        self.assertEqual(self.selector.select(patch(self.source, self.source.replace('VALUE = 2', 'VALUE = 0'))), {7, 19})

    def test_python_comment_only(self):
        self.assertEqual(self.selector.select(patch(self.source, '# comment\n'+self.source)), set())

    def test_mixed_comment_and_fixture(self):
        name = 'tests/fixtures/input.sql'
        (self.root/'tests/fixtures').mkdir(parents=True)
        (self.root/name).write_text('SELECT 1;\n')
        self.mapping['reads'][name] = [1]
        change = patch(self.source, '# comment\n'+self.source) + patch('SELECT 1;\n', '# directive changed\nSELECT 2;\n', name)
        self.assertEqual(self.selector.select(change), {19})

    def test_syntax_error_and_unknown_patch(self):
        for text in [patch(self.source, 'not valid python !\n'), 'GIT binary patch\n']:
            self.assertEqual(self.selector.select(text), {7, 19})

    def test_unmatched_source_falls_back(self):
        self.assertEqual(self.selector.select(patch(self.source.replace('VALUE = 2', 'VALUE = 5'), self.source)), {7, 19})

    def test_new_tests_are_included(self):
        self.tests.append({'number': 31, 'id': 'tests/new.py::test_new', 'seconds': 1})
        selector = Selector(self.tests, self.mapping, self.root)
        self.assertEqual(selector.select(patch(self.source, self.source.replace('return 1', 'return 9'))), {7, 31})

    def test_multiple_hunks_and_insertions(self):
        after = self.source.replace('return 1', 'return 9').replace('return 3', 'return 8\n    # note')
        section = parse_patch(patch(self.source, after))[0]
        actual, _ = apply_section(self.source, section)
        self.assertEqual(actual, after)

    def test_code_moved_to_import_scope_is_conservative(self):
        after = self.source.replace('    return 0', 'raise RuntimeError("import failure")')
        self.assertEqual(self.selector.select(patch(self.source, after)), {7, 19})

    def test_default_argument_is_conservative(self):
        after = self.source.replace('def f(x):', 'def f(x=1/0):')
        self.assertEqual(self.selector.select(patch(self.source, after)), {7, 19})

    def test_import_time_call_overrides_partial_or_absent_runtime_hits(self):
        name = 'sqlglot/example.py'
        old = '@decorate\ndef config():\n    return 1\nSETTING = config()\n'
        (self.root/name).write_text(old)
        self.mapping['lines'][name] = {'3': [0]}
        self.mapping['entered'] = {name: []}
        self.mapping['import_calls'][name] = ['config:1']
        change = patch(old, old.replace('return 1', 'return 2'))
        self.assertEqual(self.selector.select(change), {7, 19})
        del self.mapping['import_calls']
        self.assertEqual(self.selector.select(change), {7, 19})

    def test_lambda_creation_and_import_time_invocation(self):
        name = 'sqlglot/example.py'
        old = 'f = lambda x: x + 1\n'
        (self.root/name).write_text(old)
        self.mapping['lines'][name] = {'1': [0]}
        change = patch(old, old.replace('+ 1', '+ 2'))
        self.assertEqual(self.selector.select(change), {7})
        self.mapping['import_calls'][name] = ['<lambda>:1']
        self.assertEqual(self.selector.select(change), {7, 19})

    def test_collected_factory_does_not_execute_nested_callback(self):
        name = 'sqlglot/example.py'
        old = 'def factory():\n    def fn(x):\n        return x + 1\n    return fn\nf = factory()\n'
        (self.root/name).write_text(old)
        self.mapping['import_calls'][name] = ['factory:1']
        self.mapping['lines'][name] = {'3': [0]}
        change = patch(old, old.replace('+ 1', '+ 2'))
        self.assertEqual(self.selector.select(change), {7})
        self.mapping['import_calls'][name].append('fn:2')
        self.assertEqual(self.selector.select(change), {7, 19})

    def test_import_time_call_precedes_identity_shortcut(self):
        name = 'sqlglot/example.py'
        old = 'def f(x):\n    return x is None\nSETTING = f(None)\n'
        (self.root/name).write_text(old)
        self.mapping['identity'] = {f'{name}:2:11': True}
        self.mapping['import_calls'][name] = ['f:1']
        self.assertEqual(self.selector.select(patch(old, old.replace(' is ', ' == '))), {7, 19})

    def test_class_literal_includes_readers_without_constructor(self):
        old = 'class Dialect:\n    FLAG = True\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['classes'] = {'sqlglot/example.py:Dialect': [1]}
        self.mapping['scalar_readers'] = {'FLAG': [0]}
        self.assertEqual(self.selector.select(patch(old, old.replace('True', 'False'))), {7})

    def test_unobserved_class_falls_back(self):
        old = 'class Dialect:\n    FLAG = True\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.assertEqual(self.selector.select(patch(old, old.replace('True', 'False'))), {7, 19})

    def test_class_metaclass_change_falls_back(self):
        old = 'class Dialect(metaclass=Old):\n    FLAG = True\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['classes'] = {'sqlglot/example.py:Dialect': [1]}
        new = old.replace('Old', 'New').replace('True', 'False')
        self.assertEqual(self.selector.select(patch(old, new)), {7, 19})

    def test_observed_identity_equivalence(self):
        old = 'def f(x):\n    return x is None\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['identity'] = {'sqlglot/example.py:2:11': True}
        self.assertEqual(self.selector.select(patch(old, old.replace(' is ', ' == '))), set())

    def test_unsafe_identity_keeps_tests(self):
        old = 'def f(x):\n    return x is None\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['identity'] = {'sqlglot/example.py:2:11': False}
        self.mapping['lines']['sqlglot/example.py'] = {'2': [0]}
        self.assertEqual(self.selector.select(patch(old, old.replace(' is ', ' == '))), {7})

    def test_comparison_observer_excludes_custom_equality(self):
        from tools.identity_usage import comparison_equivalent
        class Custom:
            def __eq__(self, other):
                raise AssertionError('custom equality must not execute')
        self.assertFalse(comparison_equivalent(Custom(), None))
        self.assertTrue(comparison_equivalent(object(), None))
        self.assertTrue(comparison_equivalent(None, None))
        self.assertFalse(comparison_equivalent(''.join(['equal', ' value']), ''.join(['equal', ' value'])))

    def test_class_factory_arguments_have_import_time_effects(self):
        old = 'class A:\n    FLAG = initialize(strict=True)\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['classes'] = {'sqlglot/example.py:A': [0]}
        self.mapping['scalar_readers'] = {'FLAG': [0]}
        self.assertEqual(self.selector.select(patch(old, old.replace('True', 'False'))), {7, 19})

    def test_class_import_arithmetic_falls_back(self):
        old = 'class Dialect:\n    FLAG = 1 / 2\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['classes'] = {'sqlglot/example.py:Dialect': [1]}
        self.assertEqual(self.selector.select(patch(old, old.replace('1 / 2', '1 / 0'))), {7, 19})

    def test_value_index_preserves_membership_changes(self):
        old = 'class A:\n    TABLE = {"x": 1, "y": 2}\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['classes'] = {'sqlglot/example.py:A': [0, 1]}
        self.mapping['dictionary_values'] = {'TABLE': [1]}
        self.assertEqual(self.selector.select(patch(old, old.replace('1,', '3,'))), {19})
        self.assertEqual(self.selector.select(patch(old, old.replace('"x"', '"z"'))), {7, 19})

    def test_private_global_only_when_indexed(self):
        old = '_NAME: str = "old"\n'; (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['private_globals'] = {'sqlglot/example.py:_NAME': [0]}
        self.assertEqual(self.selector.select(patch(old, old.replace('old', 'new'))), {7})
        self.assertEqual(self.selector.select(patch(old, old.replace('str', 'float'))), {7, 19})

    def test_composite_declaration_and_method_preserves_both_dependencies(self):
        original = 'class A:\n    FLAG = 1\n    def f(self):\n        return 1\n'
        (self.root/'sqlglot/example.py').write_text(original)
        self.mapping['lines']['sqlglot/example.py'] = {'4': [1]}
        self.mapping['scalar_readers'] = {'FLAG': [0]}
        self.mapping['runtime_bodies'] = {'sqlglot/example.py:f:3': True}
        self.selector.all.add(40)
        change = patch(original, original.replace('= 1', '= 2').replace('return 1', 'return 2'))
        self.assertEqual(self.selector.select(change), {7, 19})
        self.mapping['runtime_bodies'].clear()
        self.assertEqual(self.selector.select(change), {7, 19, 40})
        self.assertEqual(self.selector.select(patch(original, original.replace('= 1', '= 2')
                                                   .replace('f(self)', 'f(self, x)'))), {7, 19, 40})

    def test_tail_insertion_uses_whole_runtime_body(self):
        self.mapping['runtime_bodies'] = {'sqlglot/example.py:f:3': True}
        change = patch(self.source, self.source.replace('    return 0\n', '    return 0\n    extra_local = 5\n'))
        self.assertEqual(self.selector.select(change), {7})
        self.mapping['runtime_bodies'].clear()
        self.assertEqual(self.selector.select(change), {7, 19})
        yield_change = patch(self.source, self.source.replace('    return 0\n', '    return 0\n    yield 5\n'))
        self.mapping['runtime_bodies'] = {'sqlglot/example.py:f:3': True}
        self.assertEqual(self.selector.select(yield_change), {7, 19})

    def test_existing_generator_tail_retains_whole_body_and_rejects_promotion(self):
        old = 'def f():\n    yield 1\n\ndef g(): pass\n'
        (self.root/'sqlglot/example.py').write_text(old)
        self.mapping['runtime_bodies'] = {'sqlglot/example.py:f:1': True}
        self.mapping['lines']['sqlglot/example.py'] = {'2': [1]}
        edit = patch(old, old.replace('yield 1', 'yield 1\n    yield 2'))
        self.assertEqual(self.selector.select(edit), {7, 19})
        self.mapping['generator_users'] = {'sqlglot/example.py:f:1': [1]}
        self.assertEqual(self.selector.select(edit), {19})
        self.mapping['generator_users']['sqlglot/example.py:f:1'].append(0)
        self.assertEqual(self.selector.select(edit), {7, 19})
        cases = (
            ('def f():\n    def g(): yield 1', '    yield 2'),
            ('def f():\n    def g(x=(yield 1)): pass', '    yield 2'),
            ('def f():\n    yield 1', '    yield dynamic()'),
            ('def f():\n    yield 1', '    local = 2'),
        )
        for source, statement in cases:
            with self.subTest(source=source, statement=statement):
                before = ast.parse(source).body[0]
                after = ast.parse(source + '\n' + statement).body[0]
                self.assertFalse(only_generator_tail(before, after))

    def test_inert_tail_proof_covers_both_sides_of_insertion(self):
        self.mapping['runtime_bodies'] = {'sqlglot/example.py:f:3': True}
        new = self.source.replace('    return 0\n','    return 0\n    return 5\n')
        self.assertEqual(self.selector.select(patch(self.source,new)), set())

    def test_compile_verification_retains_observable_locals_and_generator_changes(self):
        import ast
        from selector import inert_insertions
        original = 'def f():\n    return 1\n'
        for statement, expected in [('return 2', True), ('variable = 2', False), ('yield 2', False)]:
            new = original + '    ' + statement + '\n'
            self.assertEqual(inert_insertions(ast.parse(original), new, parse_patch(patch(original,new))[0],
                                              'module.py'), expected)

    def test_deferred_factory_requires_verified_site_and_same_call(self):
        old = 'class A:\n    TABLE = {"k": build(True)}\n'
        (self.root/'sqlglot/example.py').write_text(old)
        change = patch(old, old.replace('True', 'False'))
        self.assertEqual(self.selector.select(change), {7, 19})
        self.mapping['deferred_calls'] = {'sqlglot/example.py:2:18': [0]}
        self.assertEqual(self.selector.select(change), {7})
        for new in (old.replace('build', 'other'), old.replace('True', '0'), old.replace('"k"', '"q"')):
            self.assertEqual(self.selector.select(patch(old, new)), {7, 19})

    def test_side_effect_or_eager_callback_default_disallows_factory(self):
        import ast
        from closure_index import captures_only
        for body in ['seen.append(flag)\n return lambda: flag',
                     'def cb(value=touch(flag)): return value\n return cb']:
            tree = ast.parse('def factory(flag):\n '+body+'\n')
            self.assertIsNone(captures_only(tree.body[0]))

    def test_schedule_accounts_for_worker_overhead(self):
        self.assertEqual(len(schedule(self.tests)), 1)
        tests = [{'number': i, 'seconds': 10} for i in range(8)]
        workers = schedule(tests)
        self.assertEqual(len(workers), 8)
        self.assertEqual(sorted(n for w in workers for n in w), list(range(8)))
        self.assertEqual(schedule([]), [])


if __name__ == '__main__':
    unittest.main()
