from array import array
import builtins
from collections import Counter, defaultdict, deque
from collections.abc import (
    Collection, Hashable, Iterable, Iterator, Mapping, Sequence,
)
from contextlib import AbstractContextManager
from contextvars import ContextVar, copy_context
from datetime import datetime
from functools import partial
import gc
import html
from importlib import import_module
from itertools import chain, count
from operator import itemgetter
import os
from pathlib import Path
from pprint import pformat
import sys
from tempfile import gettempdir
import threading
from types import (
    ClassMethodDescriptorType, FunctionType, GetSetDescriptorType,
    MemberDescriptorType, MethodDescriptorType, NoneType, WrapperDescriptorType,
)
from typing import Any, Callable, cast, Self
import warnings


__all__ = (
    'IMMORTAL_REFCOUNT', 'SLOT_SIZE',
    'debug_logger', 'descriptor_types_to_ignore', 'native_modules',
    'Addresses', 'Cell', 'CellData', 'CustomHash', 'DuplicateObjectsDict',
    'Instances', 'TypeStats', 'TypeStatsDict',
    'custom_hash', 'deep_get_referents', 'detect_duplicate_values_of',
    'get_instance_dict', 'import_all_stdlib_modules', 'limited_repr', 'main',
    'qual_name', 'render_gc_stats', 'render_html_table',
    'render_plain_text_table', 'render_process_stats',
    'types_by_duplicate_objects',
)


_is_interned = getattr(sys, '_is_interned', None)  # added in Python 3.13


def fake_print(*args: object, sep: str = ' ', end: str = '\n', flush: bool = False) -> None:
    pass


debug_logger: ContextVar[Callable[..., None]] = ContextVar(
    'debug_logger', default=fake_print
)


def determine_immortal_refcount() -> int | None:
    """Determine the reference count of immortal objects, if they exist.
    """
    count1 = sys.getrefcount(None)
    extra_ref = None  # noqa: F841
    count2 = sys.getrefcount(None)
    if count2 == count1:
        return count1
    else:
        return None


IMMORTAL_REFCOUNT = determine_immortal_refcount()


def determine_slot_size() -> int:
    """Determine how many octets of memory a slot occupies.
    """
    A = type('A', (), {'__slots__': ()})
    B = type('B', (), {'__slots__': ('x',)})
    return sys.getsizeof(B()) - sys.getsizeof(A())


SLOT_SIZE = determine_slot_size()


def qual_name(cls: type) -> str:
    """Return the qualified name of the given class, except for builtins.
    """
    return (
        cls.__name__ if cls.__module__ == 'builtins' else
        f"{cls.__module__}.{cls.__name__}"
    )


def render_process_stats() -> str:
    """Get some basic statistics on the current process. Unix only.
    """
    if sys.platform == 'win32':  # for mypy
        return ""
    try:
        import resource
    except ImportError:
        return ""
    ru = resource.getrusage(resource.RUSAGE_SELF)
    total_time = ru.ru_utime + ru.ru_stime
    u2s_ratio = ru.ru_utime / total_time
    res_mem = ru.ru_idrss or '<unknown>'
    res_mem_peak = ru.ru_maxrss or '<unknown>'
    if '<unknown>' in (res_mem, res_mem_peak):
        # Problem: https://stackoverflow.com/q/7205806
        # Solution: simple Linux way to get the current process' memory footprint
        # Doc: https://www.kernel.org/doc/html/latest/filesystems/proc.html
        try:
            with open('/proc/self/status', 'r') as f:
                for line in f:
                    key, value = map(str.strip, line.split(':', 1))
                    if key == 'VmRSS':
                        res_mem = value
                    elif key == 'VmHWM':
                        res_mem_peak = value
        except FileNotFoundError:
            pass
    return (
        f"Elapsed time: {total_time:.3f}s ({u2s_ratio:.1%} in userland). "
        f"Resident memory: {res_mem} now, {res_mem_peak} at peak. "
        f"Active threads: {threading.active_count()}."
    )


def render_gc_stats() -> str:
    """Get some basic statistics from the garbage collector.
    """
    n_gc_objects = len(gc.get_objects())
    gc_stats = gc.get_stats()
    n_collected = sum(map(itemgetter('collected'), gc_stats))
    n_runs = sum(map(itemgetter('collections'), gc_stats))
    n_uncollectable = sum(map(itemgetter('uncollectable'), gc_stats))
    return (
        f"The garbage collector is tracking {n_gc_objects} objects and has "
        f"previously deleted {n_collected} others in {n_runs} runs. It has "
        f"found {n_uncollectable} uncollectable objects."
    )


native_modules: dict[str, bool] = dict.fromkeys(sys.builtin_module_names, True)
"A cache. The key is a module name, the value indicates whether the module is native."
native_modules['__main__'] = False


def is_native_callable(obj: object) -> bool:
    """Determine whether the given object is a callable from a native module.
    """
    if not callable(obj):
        return False
    if isinstance(obj, partial):
        obj = obj.func
    # Get the name of the module this object's definition is from. It's either
    # `obj.__module__` (for a type or function) or `obj.__class__.__module__`
    # (for everything else).
    module_name = getattr(obj, '__module__', None) or obj.__class__.__module__
    # Check whether this module name is already in our cache.
    if (r := native_modules.get(module_name)) is not None:
        return r
    # Get the module's bytecode from its loader. If no bytecode is returned,
    # then the module is assumed to be native.
    module = sys.modules[module_name]
    r = module.__loader__.get_code(module.__spec__.name) is None  # type: ignore
    native_modules[module_name] = r
    return r


descriptor_types_to_ignore: set[type] = {
    ClassMethodDescriptorType, classmethod, FunctionType, MethodDescriptorType,
    WrapperDescriptorType,
}
"A set of descriptor types that aren't worth calling in our case, because they "
"never return objects we're interested in (as of Python 3.13)."


iterables_to_skip : set[type] = {array, bytearray, bytes, str}
"A set of types whose instances aren't worth calling `iter` on in our case."


def deep_get_referents(
    *objects: object,
    limit: int = 0,
    seen: set[int] | None = None,
    attributes_to_skip: set[int] = set(),
    descriptor_types_to_ignore: set[type] = descriptor_types_to_ignore,
    iterables_to_skip: set[type] = iterables_to_skip,
) -> Iterator[object]:
    """Yields the preexisting objects directly or indirectly referenced by `obj`.

    Also yields objects newly created and cached by native code, except empty
    instance dictionaries (the `__dict__` attribute of an object).

    Doesn't find unexposed objects created by native code.

    Doesn't find the local data of threads other than the one it runs in.
    """
    debug = debug_logger.get()
    getrefcount = sys.getrefcount

    if (np := sys.modules.get('numpy')):
        iterables_to_skip.add(np.ndarray)
    del np

    if seen is None:
        seen = set()
    queue: deque[object] = deque()

    def add_to_queue(obj: object) -> bool:
        """Add `obj` to the queue if it hasn't already been seen.

        Returns `True` if the object was added to the queue, `False` if the object
        already is or has been in the queue.
        """
        obj_id = id(obj)
        if obj_id in seen:
            return False
        queue.append(obj)
        seen.add(obj_id)
        return True

    def extend_queue(f: Callable[[], Iterable[object]]) -> None:
        """Add to the queue the objects consistently returned by `f()`.

        `f()` is called twice to detect whether each object is consistently returned.
        """
        objects_by_address: dict[int, object] = {}
        object_counts: dict[int, int] = defaultdict(int)
        try:
            for obj in chain(f(), f()):
                obj_address = id(obj)
                objects_by_address.setdefault(obj_address, obj)
                object_counts[obj_address] += 1
        except NotImplementedError:
            # urllib3._collections.RecentlyUsedContainer notably raises this
            return
        for obj_address, occurrences in object_counts.items():
            if occurrences >= 2:
                add_to_queue(objects_by_address[obj_address])

    for obj in objects:
        add_to_queue(obj)
    examined_classes = set()
    i = 0
    while i < limit or not limit:
        try:
            obj = queue.popleft()
        except IndexError:
            break
        yield obj
        if (obj_dict := getattr(obj, '__dict__', None)) is not None:
            if obj_dict is not obj.__dict__:
                # This `__dict__` is dynamically created every time it's accessed.
                # Example: `type.__dict__` (which is a mappingproxy).
                # We assume that the keys and values are stable even though the
                # dict itself isn't.
                extend_queue(obj_dict.keys)
                extend_queue(obj_dict.values)
            elif obj_dict is obj.__class__.__dict__.get('__dict__'):
                # This `__dict__` is a class variable, not an instance dict.
                add_to_queue(obj_dict)
            elif obj_dict:
                # This `__dict__` is a non-empty instance dict.
                add_to_queue(obj_dict)
        del obj_dict
        if isinstance(obj, type):
            mro = obj.__mro__
        else:
            mro = obj.__class__.__mro__
        for cls in mro:
            add_to_queue(cls)
            try:
                cls_dict_items = list(cls.__dict__.items())
            except Exception as e:
                warnings.warn(
                    f"calling {qual_name(cls)}.__dict__.items() raised {e!r}"
                )
                continue
            for k, v in cls_dict_items:
                getter = getattr(v, '__get__', None)
                if getter is None:
                    continue
                if isinstance(getter, property):
                    getter = getter.fget
                if not is_native_callable(getter):
                    # Don't try to access non-native properties. They're too
                    # slow, unsafe, and unlikely to return preexisting objects
                    # that we wouldn't find elsewhere.
                    continue
                if k in {'__class__', '__dict__', '__text_signature__'}:
                    continue
                if id(v) in attributes_to_skip:
                    continue
                descriptor_type = v.__class__
                if descriptor_type in descriptor_types_to_ignore:
                    continue
                if descriptor_type is staticmethod and cls in examined_classes:
                    continue
                debug(f'calling {v}.__get__({object.__repr__(obj)}) ', end='')
                try:
                    attr = getter(obj, cls)
                except DeprecationWarning:
                    debug('→ emitted a DeprecationWarning')
                    attributes_to_skip.add(id(v))
                    continue
                except Exception as e:
                    debug(f'→ raised {e!r}')
                    continue
                refcount = getrefcount(attr)
                if refcount > 2:
                    if add_to_queue(attr):
                        debug(
                            '→ returned a previously unseen object of type '
                            f'{qual_name(type(attr))}'
                        )
                    elif isinstance(attr, (NoneType, bool)):
                        debug(f'→ returned {attr}')
                    else:
                        debug('→ returned an already seen object')
                else:
                    debug('→ returned a newly created object')
                del attr, refcount
            examined_classes.add(cls)
        del cls, mro
        if isinstance(obj, Mapping):
            if is_native_callable(obj.keys):
                extend_queue(obj.keys)
            if is_native_callable(obj.values):
                extend_queue(obj.values)
        elif isinstance(obj, Collection):
            if is_native_callable(obj.__iter__):
                if obj.__class__ not in iterables_to_skip:
                    extend_queue(lambda: iter(obj))
        i += 1
    if queue:
        queue_by_type = sorted(
            Counter(map(type, queue)).items(),
            key=itemgetter(1),
            reverse=True,
        )
        warnings.warn(
            f"There are {len(queue)} objects left in the queue. Top types: {', '.join(
                f"{cls!r} {count}" for cls, count in queue_by_type[:5]
            )}"
        )


class Cell[Value: float | int | str]:
    """A table cell
    """

    __slots__ = {
        'value': "The always visible value of the cell.",
        'unit':
            "If the value of the cell is a dimensioned quantity. Example: 'B' for "
            "bytes. There is one special value: '%', for a percentage.",
        'details': "Additional information meant to be displayed on demand.",
    }

    def __init__(self, value: Value, *, unit: str = '', details: str = ''):
        self.value = value
        self.unit = unit
        self.details = details

    def __str__(self) -> str:
        value: float | int | str = self.value
        if self.unit == '%':
            return f"{value:.1%}"
        if not isinstance(value, str):
            value = '{:_}'.format(value)
        if self.unit:
            return f"{value} {self.unit}"
        else:
            return value


type CellData = Cell[Any] | float | int | str


def limited_repr(obj: object, ellipsis_at: int) -> Cell[str]:
    """Return a `Cell` representing `obj`.

    If `repr(obj)` is longer than `ellipsis_at` characters, then `Cell.value`
    contains a partial representation and `Cell.details` contains a full
    representation.
    """
    try:
        obj_repr = repr(obj)
    except Exception:
        obj_repr = object.__repr__(obj)
    if len(obj_repr) > ellipsis_at:
        try:
            obj_pretty_repr = pformat(obj)
            if obj_pretty_repr.startswith('(') and obj_pretty_repr.endswith(')'):
                if not obj_repr.startswith('('):
                    obj_pretty_repr = obj_pretty_repr[1:-1]
        except Exception:
            obj_pretty_repr = obj_repr
        return Cell(obj_repr[:ellipsis_at - 1] + '…', details=obj_pretty_repr)
    else:
        return Cell(obj_repr)


def render_html_table(
    headers: tuple[CellData, ...],
    data: Sequence[tuple[CellData, ...]],
    totals: tuple[CellData, ...],
    table_id: str,
    ellipsis_threshold: int = 0,
) -> Iterator[str]:
    """Render the given data as an HTML table.
    """
    escape = html.escape
    table_id = escape(table_id)

    def render_cell(cell: CellData, col_i: int) -> Iterator[str]:
        if isinstance(cell, Cell):
            if cell.details:
                yield '<details name="'
                yield table_id
                yield 'c'
                yield str(col_i)
                yield '"><summary>'
            yield escape(str(cell))
            if cell.details:
                yield '</summary><div class="details">'
                yield escape(cell.details).replace('\n', '<br>')
                yield '</div></details>'
        else:
            yield escape(str(Cell(cell)))

    def generate() -> Iterator[str]:
        if not data:
            return
        yield '<table id="'
        yield table_id
        yield '">\n'
        yield '<tr>'
        for col_i, cell in enumerate(headers, 1):
            yield '<th>'
            yield from render_cell(cell, col_i)
            yield '</th>'
        yield '</tr>\n'
        for row in data:
            yield '<tr>'
            for col_i, cell in enumerate(row, 1):
                yield '<td>'
                yield from render_cell(cell, col_i)
                yield '</td>'
            yield '</tr>\n'
        yield '<tr>'
        for col_i, cell in enumerate(totals, 1):
            yield '<td>'
            yield from render_cell(cell, col_i)
            yield '</td>'
        yield '</tr>\n'
        yield '</table>\n'

    return generate()


def render_plain_text_table(
    headers: tuple[CellData, ...],
    data: Sequence[tuple[CellData, ...]],
    totals: tuple[CellData, ...],
    maximum_number_of_rows: int = 0,
) -> str:
    """Render the given data as a plain text (Markdown-like) table.

    Dumb implementation, doesn't do anything to try to prevent overflow and
    doesn't even support multi-line cell content.

    The `Cell.details` attributes are ignored.
    """
    if not data:
        return ''

    def stringify(v: CellData) -> str:
        return str(v if isinstance(v, Cell) else Cell(v))

    rows: deque[tuple[str, ...]] = deque()
    rows.append(tuple(map(stringify, headers)))
    widths = list(map(len, rows[0]))
    rows.extend(tuple(map(stringify, row)) for row in data[:maximum_number_of_rows])
    rows.append(tuple(map(stringify, totals)))
    n_cols = len(headers)
    temp_row = [''] * n_cols
    for row_i, row in enumerate(rows):
        for col_i, v in enumerate(row):
            temp_row[col_i] = v
            widths[col_i] = max(widths[col_i], len(v))
        rows[row_i] = tuple(temp_row)
    rows.insert(1, (
        ( f"top {maximum_number_of_rows}".center(widths[0], '-')
          if len(data) > maximum_number_of_rows else
          '-' * widths[0]
        ),
        *('-' * widths[i] for i in range(1, n_cols))
    ))
    rows.insert(-1, (
        'total'.center(widths[0], '.'),
        *('.' * widths[i] for i in range(1, n_cols))
    ))
    totals_row = len(rows) - 1
    return '\n' + '\n'.join(
        f"| {" | ".join(
            (
                v.ljust(widths[col_i])
                if col_i == 0 and row_i != totals_row else
                v.rjust(widths[col_i])
            ) for col_i, v in enumerate(row))
        } |" for row_i, row in enumerate(rows)
    ) + '\n'


class Addresses:
    """Counts memory addresses and stores (some of) them.
    """

    __slots__ = {
        'count': "The number of times the `add` method has been called.",
        'low': "The lowest addresses passed to the `add` method.",
        'high': "The highest addresses passed to the `add` method.",
        'half_limit': "Half of the maximum number of addresses to store.",
    }

    def __init__(self, limit: int = 10):
        self.low: list[int] = []
        self.high: list[int] = []
        self.half_limit = limit // 2
        self.count = 0

    def add(self, addr: int) -> None:
        """Add an address.

        This method assumes that the address hasn't already been added.
        """
        self.count += 1
        half_limit = self.half_limit
        low, high = self.low, self.high
        low_len, high_len = len(low), len(high)
        if low_len < half_limit or addr < low[-1]:
            if low_len == half_limit:
                if high_len < half_limit:
                    high.append(low[-1])
                    high.sort(reverse=True)
                low[-1] = addr
            else:
                low.append(addr)
            low.sort()
        elif high_len < half_limit or addr > high[-1]:
            if high_len == half_limit:
                high[-1] = addr
            else:
                high.append(addr)
            high.sort(reverse=True)

    @property
    def is_sample(self) -> bool:
        """Whether the stored addresses are a sample or the complete set.
        """
        return self.count > (self.half_limit * 2)

    def __str__(self) -> str:
        return '\n'.join((
            *(f"0x{addr:x}" for addr in self.low),
            *(('…',) if self.is_sample else ()),
            *(f"0x{addr:x}" for addr in reversed(self.high)),
        ))


class Instances:
    """Stores statistics on objects.
    """

    __slots__ = {
        'addresses': "The memory addresses of the objects.",
        'interned': "The number of interned instances. Only strings can be interned.",
        'refcounts': "The reference counts of the objects.",
    }

    def __init__(self) -> None:
        self.addresses = Addresses()
        self.interned = 0
        self.refcounts: dict[int, int] = defaultdict(int)

    def add(self, instance: object, refcount: int) -> None:
        """Add an object.

        This method assumes that the object hasn't already been added.
        """
        self.addresses.add(id(instance))
        if type(instance) is str and _is_interned and _is_interned(instance):
            self.interned += 1
        self.refcounts[refcount] += 1

    def render_refcounts(self) -> str:
        """Render the collected object reference counts in plain text.
        """
        return '\n'.join(
            f"{'immortal' if refcount == IMMORTAL_REFCOUNT else refcount} "
            f"({occurrences}×)"
            for refcount, occurrences in sorted(self.refcounts.items())
        )


def get_instance_dict(o: object) -> dict[str, object] | None:
    """Returns the `__dict__` attribute of the given object, or None.

    Simple implementation: doesn't try to determine whether the dict actually
    exists or if it's created by the request to access it. Thus, it only returns
    `None` if the object doesn't have a `__dict__` slot at all.
    """
    o_dict = getattr(o, '__dict__', None)
    if o_dict is not o.__class__.__dict__.get('__dict__'):
        return o_dict
    else:
        return None


class CustomHash:
    """A hashable wrapper for unhashable objects.
    """

    __slots__ = {
        'original': "The wrapped object.",
        'reduced': "A hashable representation of the wrapped object.",
    }

    def __init__(self, original: object, reduced: Hashable) -> None:
        self.original = original
        self.reduced = reduced

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CustomHash):
            return NotImplemented
        try:
            return self.original == other.original
        except Exception:
            return self.reduced == other.reduced

    def __ge__(self, other: object) -> bool:
        if not isinstance(other, CustomHash):
            raise TypeError(f"expected CustomHash, got {type(other)}")
        try:
            ge = self.reduced >= other.reduced  # type: ignore[operator]
        except Exception:
            pass
        else:
            if isinstance(ge, bool):
                return ge
        return id(self.original) >= id(other.original)

    def __gt__(self, other: object) -> bool:
        if not isinstance(other, CustomHash):
            raise TypeError(f"expected CustomHash, got {type(other)}")
        try:
            gt = self.reduced > other.reduced  # type: ignore[operator]
        except Exception:
            pass
        else:
            if isinstance(gt, bool):
                return gt
        return id(self.original) > id(other.original)

    def __hash__(self) -> int:
        return hash(self.reduced)

    def __le__(self, other: object) -> bool:
        if not isinstance(other, CustomHash):
            raise TypeError(f"expected CustomHash, got {type(other)}")
        try:
            le = self.reduced <= other.reduced  # type: ignore[operator]
        except Exception:
            pass
        else:
            if isinstance(le, bool):
                return le
        return id(self.original) <= id(other.original)

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, CustomHash):
            raise TypeError(f"expected CustomHash, got {type(other)}")
        try:
            lt = self.reduced < other.reduced  # type: ignore[operator]
        except Exception:
            pass
        else:
            if isinstance(lt, bool):
                return lt
        return id(self.original) < id(other.original)

    def __repr__(self) -> str:
        return f"CustomHash({self.original!r})"


def custom_hash(obj: object) -> Hashable:
    """Return a hashable representation of the given object.

    If the object is hashable, returns it as-is.
    If the object is a dict, returns a sorted tuple of its items.
    If the object is a deque, list or non-hashable tuple, returns a hashable tuple.
    If the object is a set, returns a frozenset.
    If the object is a bytearray, returns bytes.
    Otherwise, builds a representation of the object somewhat similar to the one
    its `__reduce__` method is supposed to return, without calling that method,
    and wraps it in a `CustomHash` object.

    Can fail with a `RecursionError: maximum recursion depth exceeded` exception.
    """
    parents: dict[int, int] = {}

    def reduce(obj: object) -> Hashable:
        try:
            hash(obj)
        except Exception:
            pass
        else:
            return obj
        obj_address = id(obj)
        if obj_address in parents:
            return CustomHash(Ellipsis, Ellipsis)
        parents[obj_address] = len(parents)
        r: Hashable
        if type(obj) is dict:
            try:
                r = tuple(sorted(
                    ((k, reduce(v)) for k, v in obj.items()),
                    key=itemgetter(0),
                ))
            except TypeError:
                r = tuple(sorted(
                    ((CustomHash(k, k), reduce(v)) for k, v in obj.items()),
                    key=itemgetter(0),
                ))
        elif type(obj) is deque or type(obj) is list or type(obj) is tuple:
            r = tuple(reduce(el) for el in obj)
        elif type(obj) is set:
            r = frozenset(obj)
        elif type(obj) is bytearray:
            r = bytes(obj)
        else:
            getrefcount = sys.getrefcount
            debug = debug_logger.get()
            obj_dict = get_instance_dict(obj)
            if obj_dict is None:
                dict_attributes = None
            else:
                dict_attributes = reduce(obj_dict)
            slot_attributes = []
            elements: list[Hashable] = []
            items: list[tuple[tuple[Hashable, Hashable], ...]] = []
            for cls in obj.__class__.__mro__:
                for k, v in cls.__dict__.items():
                    if k in {'__class__', '__dict__', '__text_signature__', '__weakref__'}:
                        continue
                    if type(v) not in {GetSetDescriptorType, MemberDescriptorType}:
                        continue
                    debug(f'calling {v}.__get__({object.__repr__(obj)}) ', end='')
                    try:
                        attr = v.__get__(obj, cls)
                    except Exception as e:
                        debug(f'→ raised {e!r}')
                    else:
                        refcount = getrefcount(attr)
                        if refcount > 2:
                            debug(f'→ returned an object of type {qual_name(type(attr))}')
                            slot_attributes.append((k, reduce(attr)))
                        else:
                            debug('→ returned a newly created object')
                items_method = cls.__dict__.get('items')
                iter_method = cls.__dict__.get('__iter__')
                if items_method and is_native_callable(items_method):
                    try:
                        _items = tuple(items_method.__get__(obj, cls)())
                    except Exception as e:
                        debug(
                            f"{qual_name(cls)}.items({object.__repr__(obj)}) "
                            f"raised {e!r}"
                        )
                    else:
                        items.append(tuple(
                            (reduce(k), reduce(v))
                            for k, v in _items
                        ))
                elif iter_method and is_native_callable(iter_method):
                    if cls in (array, bytearray, bytes):
                        elements.append(bytes(cast(bytes, obj)))
                    else:
                        try:
                            _elements = tuple(iter_method.__get__(obj, cls)())
                        except Exception as e:
                            debug(
                                f"{qual_name(cls)}.__iter__({object.__repr__(obj)}) "
                                f"raised {e!r}"
                            )
                        else:
                            elements.append(reduce(_elements))
                            del _elements
            slot_attributes.sort(key=itemgetter(0))
            r = (
                obj.__class__,
                tuple(slot_attributes),
                dict_attributes,
                tuple(elements),
                tuple(items),
            )
        parents.popitem()
        return CustomHash(obj, r)

    return reduce(obj)


class TypeStats:
    """Stores some statistics on a specific type.
    """

    __slots__ = {
        'type': "The type the statistics are about.",
        'missing_slots':
            "The names of the attributes that should be slots but aren't, and "
            "the number of times they appeared in instance dicts.",
        'instances': "Some statistics on the instances of this type.",
        'equal_instances':
            "Some statistics on instances of this type grouped by equal values.",
        'instances_intrinsic_memory_usage':
            "The sum of the numbers returned by `sys.getsizeof` for the encountered "
            "instances of this type.",
        'instances_dicts_memory_usage':
            "The sum of the numbers returned by `sys.getsizeof(instance.__dict__)` "
            "for the encountered instances of this type.",
    }

    def __init__(self, typ: type):
        self.type = typ
        self.missing_slots = defaultdict(int)
        for attr_name in getattr(typ, '__static_attributes__', ()):
            # Note: __static_attributes__ was added in Python 3.13
            if attr_name.startswith('__') and not attr_name.endswith('__'):
                attr_name = f"_{typ.__name__.lstrip('_')}{attr_name}"
            if not hasattr(typ.__dict__.get(attr_name), '__get__'):
                self.missing_slots[attr_name] = 0
        self.instances = Instances()
        self.equal_instances: dict[Hashable, Instances] = defaultdict(Instances)
        self.instances_intrinsic_memory_usage = 0
        self.instances_dicts_memory_usage = 0

    @property
    def estimated_savings(self) -> tuple[int, float]:
        """Estimated octets of memory savable by adding slots to the type.
        """
        savable_memory = 0
        memory_reduction = .0
        if (instances_count := self.instances.addresses.count):
            if (dicts_memory_usage := self.instances_dicts_memory_usage):
                savable_memory = dicts_memory_usage - (
                    len(self.missing_slots) * SLOT_SIZE * instances_count
                )
                memory_reduction = savable_memory / dicts_memory_usage
        elif self.missing_slots:
            dict_memory_usage = sys.getsizeof(self.missing_slots)
            savable_memory = dict_memory_usage - (
                len(self.missing_slots) * SLOT_SIZE
            )
            memory_reduction = savable_memory / dict_memory_usage
        return savable_memory, memory_reduction

    @property
    def instances_count(self) -> int:
        "alias of `self.instances.addresses.count`"
        return self.instances.addresses.count


type DuplicateObjectsTableData = tuple[
    tuple[str, str, str],
    list[tuple[Cell[str], Cell[int], Cell[int]]],
    tuple[int, Cell[int], int]
]
type DuplicateObjectsDict = dict[type, DuplicateObjectsTableData]


detect_duplicate_values_of: set[type] = {
    bytes, complex, float, frozenset, int, str, tuple,
}
"The default set of types whose instances will be inserted as keys in "
"dictionaries to detect duplicates."


class TypeStatsDict(dict[type, TypeStats]):
    """Stores statistics on all encountered types.
    """

    def __missing__(self, typ: type) -> TypeStats:
        stats = self[typ] = TypeStats(typ)
        return stats

    @classmethod
    def build(
        cls,
        limit: int = 10_000_000,
        detect_duplicate_values_of: set[type] = detect_duplicate_values_of,
        attributes_to_skip: set[int] = set(),
        custom_hash: Callable[[object], Hashable] | None = custom_hash,
    ) -> Self:
        """
        Compile statistics on all the objects in memory that are directly or
        indirectly referenced by any of the loaded modules.
        """
        getrefcount, getsizeof = sys.getrefcount, sys.getsizeof
        stats_by_type = cls()
        skip = {id(sys.modules[TypeStatsDict.__module__])}
        found_refcount_1 = False
        for o in deep_get_referents(
            builtins, sys.modules, limit=limit, seen=skip,
            attributes_to_skip=attributes_to_skip,
        ):
            typ = type(o)
            stats = stats_by_type[typ]
            stats.instances_intrinsic_memory_usage += getsizeof(o)
            if (o_dict := get_instance_dict(o)):
                for key in o_dict.keys():
                    stats.missing_slots[key] += 1
                # Note: we don't count empty instance dicts because they're
                # most likely being created by our attempts to access them.
                stats.instances_dicts_memory_usage += getsizeof(o_dict)
            del o_dict
            is_type = typ is type
            if is_type:
                # Record the existence of a type, even if we don't find any
                # instances of it.
                stats_by_type[cast(type, o)]
            refcount = getrefcount(o)
            if refcount != IMMORTAL_REFCOUNT:
                # Here we want to subtract the references we're responsible for.
                # One for each function holding a reference (this `build` function,
                # `deep_get_referents` and `getrefcount`), and an extra one if the
                # object is a type and is thus stored as a key in `stats_by_type`.
                refcount -= (4 if is_type else 3)
                assert refcount >= 0, "adjusted reference count is invalid"
                if refcount == 1 and not found_refcount_1:
                    found_refcount_1 = True
            stats.instances.add(o, refcount)
            if typ in detect_duplicate_values_of:
                try:
                    if custom_hash:
                        equal_instances = stats.equal_instances[custom_hash(o)]
                    else:
                        equal_instances = stats.equal_instances[o]
                except RecursionError:
                    warnings.warn(
                        f"hashing an object of type {qual_name(typ)} triggered "
                        "a RecursionError exception; consider calling "
                        "sys.setrecursionlimit to increase the limit"
                    )
                except TypeError as e:
                    if not str(e).startswith('unhashable type:'):
                        raise
                else:
                    equal_instances.add(o, refcount)
                    del equal_instances
        assert found_refcount_1, "refcount adjusment is almost certainly wrong"
        return stats_by_type

    def types_by_intrinsic_memory_usage(self) -> tuple[
        tuple[str, str, str, str],
        list[tuple[Cell[str], Cell[int], Cell[int], Cell[int]]],
        tuple[int, Cell[int], int, Cell[int]]
    ]:
        """Returns the data for a table of types by intrinsic memory usage.
        """
        rows = sorted((
            ( cls
            , qual_name(cls)
            , stats.instances_count
            , stats.instances_intrinsic_memory_usage
            , stats
            )
            for cls, stats in self.items()
            if stats.instances_count
        ), key=lambda t: (-t[3], -t[2], t[1]))
        total_instances = sum(map(itemgetter(2), rows))
        total_intrinsic_memory_usage = sum(map(itemgetter(3), rows))
        return (
            ( "type"
            , "cumulated memory usage"
            , "instances found"
            , "average memory usage per instance"
            ),
            [
                ( Cell(cls_qual_name, details=f"{cls!r} at 0x{id(cls):x}")
                , Cell(intrinsic_memory_usage, unit='B')
                , Cell(instances_count, details=(
                      f"refcounts:\n{stats.instances.render_refcounts()}\n\n"
                      f"addresses:\n{stats.instances.addresses}"
                  ))
                , Cell(intrinsic_memory_usage//instances_count, unit='B')
                )
                for cls, cls_qual_name, instances_count, intrinsic_memory_usage, stats
                in rows
            ],
            ( len(rows)
            , Cell(total_intrinsic_memory_usage, unit='B')
            , total_instances
            , Cell(total_intrinsic_memory_usage//total_instances, unit='B')
            ),
        )

    def uninstantiated_types(self) -> list[tuple[type, str]]:
        """Returns the list of types for which no instance was encountered.
        """
        r = [
            (cls, qual_name(cls))
            for cls, stats in self.items()
            if not stats.instances_count and not issubclass(cls, BaseException)
        ]
        r.sort(key=itemgetter(1))
        return r

    def classes_lacking_slots(self) -> tuple[
        tuple[
            tuple[str, str, str, str, str, str],
            list[tuple[Cell[str], Cell[int], Cell[int], Cell[float], Cell[int], int]],
            tuple[int, Cell[int], Cell[int], Cell[float] | str, int, int]
        ],
        tuple[
            tuple[str, str, str, str, str],
            list[tuple[Cell[str], Cell[int], Cell[int], Cell[float], Cell[int]]],
            tuple[int, str, str, str, int]
        ]
    ]:
        """Returns the data for two tables of classes lacking slots.

        The first dataset is for classes we found instances of. The second dataset
        is for uninstantiated classes.
        """
        getsizeof = sys.getsizeof
        instantiated_classes = sorted((
            (cls, qual_name(cls), *estimated_savings, stats)
            for cls, stats in self.items()
            if stats.instances_count and
               (estimated_savings := stats.estimated_savings)[1] > 0
        ), key=lambda t: (-t[2], -t[3], t[1]))
        uninstantiated_classes = sorted((
            (cls, qual_name(cls), *estimated_savings, stats)
            for cls, stats in self.items()
            if not stats.instances_count and
               (estimated_savings := stats.estimated_savings)[1] > 0
        ), key=lambda t: (-t[2], -t[3], t[1]))
        total_savable_memory = total_dicts_memory_usage = 0
        total_missing_slots = total_instances_count = 0
        for _, _, savable_memory, memory_reduction, stats in instantiated_classes:
            total_savable_memory += savable_memory
            total_dicts_memory_usage += stats.instances_dicts_memory_usage
            total_missing_slots += len(stats.missing_slots)
            total_instances_count += stats.instances_count
            del savable_memory, memory_reduction, stats
        return ((
            ( "class"
            , "savable memory"
            , "__dict__ memory usage"
            , "memory reduction"
            , "missing slots"
            , "instances found"
            ),
            [
                ( Cell(cls_qual_name, details=f"{cls!r} at 0x{id(cls):x}")
                , Cell(savable_memory, unit='B')
                , Cell(stats.instances_dicts_memory_usage, unit='B')
                , Cell(memory_reduction, unit='%')
                , Cell(
                      len(stats.missing_slots),
                      details='\n'.join(
                          f"{attr_name} "
                          f"(in {occurrences/stats.instances_count:.1%} of instances)"
                          for attr_name, occurrences
                          in sorted(stats.missing_slots.items())
                      ),
                  )
                , stats.instances_count
                )
                for cls, cls_qual_name, savable_memory, memory_reduction, stats
                in instantiated_classes
            ],
            ( len(instantiated_classes)
            , Cell(total_savable_memory, unit='B')
            , Cell(total_dicts_memory_usage, unit='B')
            , Cell(total_savable_memory / total_dicts_memory_usage, unit='%')
              if total_dicts_memory_usage else 'NaN'
            , total_missing_slots
            , total_instances_count
            ),
        ), (
            ( "class"
            , "savable memory per instance"
            , "__dict__ memory usage per instance"
            , "memory reduction per instance"
            , "detected missing slots"
            ),
            [
                ( Cell(cls_qual_name, details=f"{cls!r} at 0x{id(cls):x}")
                , Cell(savable_memory, unit='B')
                , Cell(getsizeof(stats.missing_slots), unit='B')
                , Cell(memory_reduction, unit='%')
                , Cell(
                      len(stats.missing_slots),
                      details='\n'.join(sorted(stats.missing_slots)),
                  )
                )
                for cls, cls_qual_name, savable_memory, memory_reduction, stats
                in uninstantiated_classes
            ],
            ( len(uninstantiated_classes)
            , ''
            , ''
            , ''
            , sum(len(stats.missing_slots)
                  for _, _, _, _, stats in uninstantiated_classes)
            ),
        ))

    def duplicate_objects(self, ellipsis_at: int = 40) -> DuplicateObjectsDict:
        """Returns the data for tables of duplicate objects, per type.
        """
        getsizeof = sys.getsizeof
        table_data_by_type: DuplicateObjectsDict = {}
        for cls, stats in self.items():
            if not stats.equal_instances:
                continue
            rows = []
            total_number_of_instances = total_savable_memory = 0
            for obj, instances in stats.equal_instances.items():
                instances_count = instances.addresses.count
                if instances_count <= 1:
                    continue
                if isinstance(obj, CustomHash):
                    obj = obj.original
                estimated_savings = getsizeof(obj) * (instances_count - 1)
                total_number_of_instances += instances_count
                total_savable_memory += estimated_savings
                interned = (f"interned: {
                    instances.interned if _is_interned else 'unknown'
                }",) if cls is str else ()
                rows.append((
                    limited_repr(obj, ellipsis_at), estimated_savings,
                    instances_count, instances, interned
                ))
                del obj, instances_count, estimated_savings, instances
            rows.sort(key=lambda t: (-t[1], -t[2], t[0].details or t[0].value))
            table_data_by_type[cls] = (
                (qual_name(cls), "savable memory", "instances"),
                [
                    ( obj_repr
                    , Cell(estimated_savings, unit='B')
                    , Cell(instances_count, details='\n\n'.join((
                          *interned,
                          f"refcounts:\n{instances.render_refcounts()}",
                          f"addresses:\n{instances.addresses}",
                      )))
                    )
                    for obj_repr, estimated_savings, instances_count, instances, interned
                    in rows
                ],
                ( len(rows)
                , Cell(total_savable_memory, unit='B')
                , total_number_of_instances
                )
            )
        return table_data_by_type

    def write_tables(self, save_dir: Path, html_template: str) -> None:
        """Write the collected statistics to `save_dir`, as well as a summary to stdout.
        """
        os.makedirs(save_dir, exist_ok=True)
        types_by_intrinsic_memory_usage = self.types_by_intrinsic_memory_usage()
        uninstantiated_types = self.uninstantiated_types()
        classes_lacking_slots = self.classes_lacking_slots()
        duplicate_objects_by_type = self.duplicate_objects()
        print(
            f"Types by intrinsic memory usage:{render_plain_text_table(
                *types_by_intrinsic_memory_usage,
                maximum_number_of_rows=15,
            )}"
            "Uninstantiated types (excluding BaseException and its subclasses): "
            f"{len(uninstantiated_types)}\n"
            f"Duplicate objects by type:{render_plain_text_table(
                *types_by_duplicate_objects(duplicate_objects_by_type),
                maximum_number_of_rows=15,
            ) or ' none.'}"
            f"Instantiated classes lacking slots:{render_plain_text_table(
                *classes_lacking_slots[0],
                maximum_number_of_rows=15,
            ) or ' none.'}"
        )
        with open(save_dir / 'types_by_memory_usage.html', 'wb') as f:
            f.write(html_template.replace('$body$', '\n'.join((
                "<h2>Types by intrinsic memory usage</h2>",
                ''.join(render_html_table(*types_by_intrinsic_memory_usage, '')),
            ))).encode('utf-8'))
        with open(save_dir / 'uninstantiated_types.html', 'wb') as f:
            f.write(html_template.replace('$body$', '\n'.join((
                "<h2>Uninstantiated types <small>(excluding BaseException and its subclasses)</small></h2>",
                '\n'.join(
                    f'<details><summary>{cls_qual_name}</summary>'
                    f'<div class="details">{html.escape(repr(cls))} at 0x{id(cls):x}</div></details>'
                    for cls, cls_qual_name in uninstantiated_types
                ),
            ))).encode('utf-8'))
        with open(save_dir / 'classes_lacking_slots.html', 'wb') as f:
            f.write(html_template.replace('$body$', '\n'.join((
                "<h2>Instantiated classes lacking slots</h2>",
                ''.join(render_html_table(*classes_lacking_slots[0], '')) or '<p>None found.</p>',
                "<h2>Uninstantiated classes lacking slots</h2>",
                ''.join(render_html_table(*classes_lacking_slots[1], '')) or (
                    '<p>None found.</p>' if sys.version_info >= (3, 13, 0) else
                    '<p>None found (requires Python ≥ 3.13).</p>'
                ),
            ))).encode('utf-8'))
        for cls, table_data in duplicate_objects_by_type.items():
            with open(save_dir / f'duplicate_{qual_name(cls)}.html', 'wb') as f:
                f.write(html_template.replace('$body$', '\n'.join((
                    f"<h2>Duplicate {qual_name(cls)} instances</h2>",
                    ''.join(render_html_table(*table_data, '')) or '<p>None found.</p>',
                ))).encode('utf-8'))


def types_by_duplicate_objects(
    duplicate_objects_by_type: DuplicateObjectsDict,
) -> tuple[
    tuple[str, str, str],
    list[tuple[Cell[str], Cell[int], int]],
    tuple[int, Cell[int], int],
]:
    """Aggregate data on duplicate objects of different types into a single table.
    """
    r: list[tuple[Cell[str], Cell[int], int]] = []
    total_duplicate_instances = total_savable_memory = 0
    for cls, table_data in duplicate_objects_by_type.items():
        r.append((
            Cell(qual_name(cls), details=f"{cls!r} at 0x{id(cls):x}"),
            table_data[-1][1],
            table_data[-1][2] - len(table_data[1]),
        ))
        total_savable_memory += r[-1][1].value
        total_duplicate_instances += r[-1][2]
    r.sort(key=lambda t: (-t[1].value, -t[2], t[0].value))
    return (
        ("type", "savable memory", "duplicate instances"),
        r,
        ( len(duplicate_objects_by_type)
        , Cell(total_savable_memory, unit='B')
        , total_duplicate_instances
        )
    )


def import_all_stdlib_modules(
    skip: set[str] = {'antigravity', 'idlelib.idle', 'this'},
) -> None:
    """Import (almost) all the standard library modules. Python ≥ 3.10 only.
    """
    print("Starting import of (almost) all the standard library modules")
    from pkgutil import walk_packages
    preloaded_modules_count = len(sys.modules)
    queue = deque(sorted(sys.stdlib_module_names))
    failed_imports = []
    for i in count():
        assert i < 100_000, "stuck in an infinite loop?"
        try:
            module_name = queue.popleft()
        except IndexError:
            break
        if module_name in skip or module_name.endswith('.__main__'):
            continue
        try:
            module = import_module(module_name)
        except ImportError:
            failed_imports.append(module_name)
        else:
            if getattr(module, '__path__', None):
                queue.extend(sorted(map(
                    itemgetter(1),
                    walk_packages(module.__path__, module_name + '.'),
                )))
    print(
        f"{len(sys.modules) - preloaded_modules_count} modules have been imported, "
        f"increasing the total to {len(sys.modules)}."
    )
    if failed_imports:
        n_failed = len(failed_imports)
        failed_imports.sort()
        print(
            f"{n_failed} module{'s' if n_failed != 1 else ''} failed to load: "
            f"{', '.join(failed_imports)}"
        )


HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <style>
    body { font: 16px sans-serif; padding-bottom: 10vh; }
    .details {
        background-color: #eee;
        border: 1px solid #ccc;
        border-radius: 1ex;
        box-shadow: 0 0 1ex #ccc;
        font: 14px monospace;
        padding: .1em .75ex;
    }
    table { border-collapse: collapse; }
    tr {
        border-bottom: 1px dotted #aaa;
        &:first-child, &:last-child {
            border: 1px solid #ccc;
            border-width: 1px 0;
        }
        &:last-child {
            border-top: 1px solid #ccc;
            > td:first-child {
                text-align: right;
            }
        }
        &:hover {
            background-color: #f6f6f6;
        }
    }
    td, th {
        border-left: 1px solid #ccc;
        padding: .1em .75ex;
        position: relative;
        .details {
            max-width: 50vw;
            position: absolute;
            top: 100%;
            z-index: 1000;
        }
        &:first-child .details {
            left: 0;
            width: max-content;
        }
        &:not(:first-child) {
            text-align: right;
            .details {
                right: 0;
            }
        }
        &:last-child {
            border-right: 1px solid #ccc;
        }
    }
    </style>
    <title>Memory inventory</title>
</head>
<body>
$header$
$body$
</body>
</html>
"""


raise_deprecation_warnings = warnings.catch_warnings(
    category=DeprecationWarning, action='error'
)


def main(
    save_dir: str = './memory_inventory_python_{python_version}/',
    write_debug_log: bool = False,
    objects_limit: int = 10_000_000,
    html_template: str = HTML_TEMPLATE,
    attributes_to_skip: set[int] = set(),
    detect_duplicate_values_of: set[type] = detect_duplicate_values_of,
    warnings_context: AbstractContextManager[Any] = raise_deprecation_warnings,
) -> None:
    """Write statistics on objects in memory to `save_dir` as well as a summary to stdout.
    """
    isotime = datetime.now().isoformat()
    python_version = sys.version
    save_path = Path(save_dir.format(
        isotime=isotime,
        python_version=python_version.split()[0],
        tmpdir=gettempdir(),
    ))
    del save_dir

    escape = html.escape
    html_header = (
        f'Python version: {escape(python_version)}<br>\n'
        f'Date: {isotime}<br>\n'
        f'<details><summary>Loaded modules: {len(sys.modules)}</summary> '
        f'<div class="details">{escape(', '.join(sys.modules.keys()))}</div></details><br>'
    )
    html_template = html_template.replace('$header$', html_header)

    def collect_stats() -> TypeStatsDict:
        debug: Callable[..., None]
        if write_debug_log:
            debug_log = open(save_path / 'debug.log', 'w', encoding='utf-8')
            debug = partial(print, file=debug_log)
        else:
            debug_log = open(os.devnull, 'w')
            debug = fake_print
        with debug_log, warnings_context:
            debug_logger.set(debug)
            return TypeStatsDict.build(
                limit=objects_limit, attributes_to_skip=attributes_to_skip,
                detect_duplicate_values_of=detect_duplicate_values_of,
            )

    print(render_process_stats())
    print(render_gc_stats())
    stats_by_type = copy_context().run(collect_stats)
    stats_by_type.write_tables(save_path, html_template)
    del stats_by_type
    print(render_process_stats())
    print(f"The full results have been saved in {save_path.absolute()}{os.sep}")
