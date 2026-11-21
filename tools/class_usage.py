"""Record baseline class consumers, including inherited constructor calls."""
import ast
import functools
import importlib
import inspect


def install(root, collector):
    registry = {}
    for path in sorted((root / 'sqlglot').rglob('*.py')):
        if path.name == '__main__.py':
            continue
        relative = path.relative_to(root)
        module_name = '.'.join(relative.with_suffix('').parts)
        if module_name.endswith('.__init__'):
            module_name = module_name[:-9]
        module = importlib.import_module(module_name)
        tree = ast.parse(path.read_text())

        def register(body, parent, prefix=''):
            for node in body:
                if isinstance(node, ast.ClassDef):
                    cls = getattr(parent, node.name, None)
                    if not isinstance(cls, type):
                        continue
                    name = prefix + node.name
                    registry.setdefault(cls, set()).add(str(relative) + ':' + name)
                    register(node.body, cls, name + '.')
        register(tree.body, module)

    cache = {}
    seen_context = None
    seen_classes = set()
    def record(obj):
        nonlocal seen_context
        context = collector.context
        if not context:
            return
        cls = obj if isinstance(obj, type) else type(obj)
        if context != seen_context:
            seen_context = context
            seen_classes.clear()
        elif cls in seen_classes:
            return
        seen_classes.add(cls)
        keys = cache.get(cls)
        if keys is None:
            keys = tuple(key for base in cls.__mro__ for key in registry.get(base, ()))
            cache[cls] = keys
        context = collector.context
        if context:
            for key in keys:
                collector.classes.setdefault(key, set()).add(context)

    def wrap(original):
        @functools.wraps(original)
        def init(self, *args, **kwargs):
            record(self)
            return original(self, *args, **kwargs)
        return init

    for cls in registry:
        original = vars(cls).get('__init__')
        if inspect.isfunction(original):
            cls.__init__ = wrap(original)

    # Cached dialect instances can be used without constructing them again.
    from sqlglot.dialects.dialect import Dialect
    original = Dialect.get_or_raise.__func__
    @classmethod
    @functools.wraps(original)
    def get_or_raise(cls, *args, **kwargs):
        result = original(cls, *args, **kwargs)
        record(result)
        return result
    Dialect.get_or_raise = get_or_raise
