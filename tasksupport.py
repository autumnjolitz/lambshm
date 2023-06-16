import inspect
import json
import sys
import os
import functools
import types
import dataclasses
import shutil
import pprint
import typing
import importlib
import builtins
import importlib.util
from pathlib import Path
from typing import (
    Any,
    Literal,
    Tuple,
    Optional,
    Dict,
    TypeVar,
    Iterable,
    ValuesView,
    KeysView,
    Generic,
    Mapping,
    Union,
    Type,
)
from tempfile import NamedTemporaryFile
from invoke import task as _task
from invoke.context import Context
from collections import ChainMap
from contextlib import suppress
from collections.abc import MutableSet as AbstractSet

try:
    from typing import get_overloads
except ImportError:
    from typing_extensions import get_overloads

T = TypeVar("T")
U = TypeVar("U")
DEBUG_CODEGEN = "DEBUG_CODEGEN" in os.environ and os.environ["DEBUG_CODEGEN"].lower().startswith(
    ("1", "yes", "y", "on", "t")
)


class InvertedMapping(Generic[T, U], AbstractSet):
    _mapping: Mapping[T, U]
    _keys_view: Optional[KeysView]
    __slots__ = (
        "_mapping",
        "_keys_view",
        "_values_view",
        "_iter_values_func",
        "_contains_func",
        "_getitem_func",
    )

    # Start by filling-out the abstract methods
    def __init__(self, mapping, /, **kwargs):
        self._mapping = mapping
        self._iter_values_func = self._iter_via_iter
        self._contains_func = self._mapping_contains
        self._getitem_func = self._get_key_from_value_mapping

        with suppress(AttributeError):
            keys = mapping.keys()
            if isinstance(keys, KeysView):
                self._keys_view = keys
                self._iter_values_func = self._iter_view_values_view
        with suppress(AttributeError):
            values = mapping.values()
            if isinstance(values, ValuesView):
                self._values_view = values
                self._contains_func = self._view_contains
                self._getitem_func = self._get_key_from_value_view

    def __getitem__(self, key: Union[U, slice]) -> T:
        if isinstance(key, slice):
            if key.start and key.stop and key.start != key.stop:
                emit = False
                keys = []
                for mapkey in self._mapping:
                    if key.start == mapkey:
                        emit = True
                    if key.stop == mapkey:
                        if not emit:
                            return ()
                        emit = False
                    if emit:
                        keys.append(key)
                return tuple(self[key] for key in keys)
            if key.start:
                return self._getitem_func(key.start, all=True)
            if not any((key.start, key.stop, key.step)):
                return type(self)(self._mapping.copy())
            raise ValueError
        return self._getitem_func(key)

    def add(self, value: U):
        self._mapping[value] = value

    def discard(self, value: U) -> None:
        keys = self.getall(value)
        for key in keys:
            del self._mapping[key]

    def getall(self, value: U) -> Tuple[T, ...]:
        return self._get_key_from_value_mapping(value, all=True)

    def get(self, value: U, default: Optional[T] = None) -> Optional[T]:
        keys = self.getall(value)
        with suppress(ValueError):
            key, *_ = keys
            return key
        return default

    def _get_key_from_value_mapping(self, value: U, all: bool = False) -> Union[T, Tuple[T, ...]]:
        keys = []
        for map_key, map_value in self._mapping.items():
            if map_value == value:
                keys.append(map_key)
        if keys:
            if all:
                return tuple(keys)
            return keys[0]
        if all:
            return ()
        raise KeyError(value)

    def _get_key_from_value(self, value: U, all: bool = False) -> T:
        if value not in self._values_view:
            raise KeyError(value)
        return self._get_key_from_value_mapping(value, all=all)

    def __len__(self):
        return len(self._mapping)

    def __iter__(self):
        with suppress(AttributeError):
            yield from self._iter_values_func()

    def __contains__(self, value):
        with suppress(AttributeError):
            return self._contains_func(value)
        return False

    def _iter_view_values_view(self):
        yield from self._values_view

    def _iter_via_iter(self) -> Iterable[T]:
        for key in self._mapping:
            yield self._mapping[key]

    # Modify __contains__ and get() to work like dict
    # does when __missing__ is present.
    def _view_contains(self, value: Any) -> bool:
        return value in self._values_view

    def _mapping_contains(self, value: U) -> bool:
        for member in self:
            if member == value:
                return True
        return False

    def __str__(self):
        keys = repr(tuple(self))[1:-1]
        return "{%s}" % keys

    # Now, add the methods in dicts but not in MutableMapping
    def __repr__(self):
        return f"{type(self).__name__}({self._mapping!r})"


def is_context_param(
    param: inspect.Parameter, context_param_names: Tuple[str, ...] = ("c", "ctx", "context")
) -> Optional[Literal["name", "type", "name_and_type"]]:
    value = None
    if param.name in context_param_names:
        value = "name"
    if param.annotation:
        if param.annotation is Context:
            if value:
                value = f"{value}_and_type"
            else:
                value = "type"
        elif typing.get_origin(param.annotation) is typing.Union:
            if Context in typing.get_args(param.annotation):
                if value:
                    value = f"{value}_and_type"
                else:
                    value = "type"
    return value


if "slots" in inspect.signature(dataclasses.dataclass).parameters:
    thunk = dataclasses.dataclass(frozen=True, order=True, slots=True)
else:
    thunk = dataclasses.dataclass(frozen=True, order=True)


@thunk
class FoundType:
    in_namespace: bool = dataclasses.field(hash=True, compare=True)
    namespace_path: Tuple[str, ...] = dataclasses.field(hash=True, compare=True)
    namespace_values: Tuple[Any, ...] = dataclasses.field(hash=False, compare=False)

    @property
    def key(self):
        return self.namespace_path[0]

    @property
    def value(self):
        return self.namespace_values[0]


def is_literal(item) -> bool:
    with suppress(AttributeError):
        return (item.__module__, item.__name__) in (
            ("typing", "Literal"),
            ("typing_extensions", "Literal"),
        )
    return False


def is_type_container(item):
    origin = typing.get_origin(item)
    if origin is None:
        return False
    return True


def find_this() -> types.ModuleType:
    try:
        return sys.modules["tasks"]
    except KeyError:
        from . import tasks

        return tasks


def get_types_from(
    annotation,
    in_namespace: Optional[Dict[str, Any]] = None,
) -> Iterable[FoundType]:
    if in_namespace is None:
        in_namespace = vars(find_this())
    if annotation is inspect.Signature.empty:
        annotation = Any
    if isinstance(annotation, str):
        ns = {}
        exec(f"annotation = {annotation!s}", vars(find_this()), ns)
        annotation = ns["annotation"]

    if is_literal(annotation):
        return
    if annotation in (Any, Ellipsis):
        return
    type_name = None
    with suppress(AttributeError):
        type_name = annotation.__qualname__
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin is Literal:
        yield FoundType(
            "Literal" in in_namespace,
            [
                "Literal",
            ],
            [Literal],
        )
        for arg in args:
            if not isinstance(arg, type):
                arg = type(arg)
            if arg.__name__:
                yield FoundType(arg.__name__ in in_namespace, [arg.__name__], [arg])
        return
    if origin is not None and args is not None:
        for module in types, typing, builtins:
            for value in vars(module).values():
                if value is origin:
                    for arg in args:
                        yield from get_types_from(arg, in_namespace)
                    return
        else:
            if isinstance(origin, type):
                yield FoundType(origin.__name__ in in_namespace, [origin.__name__], [origin])
                for arg in args:
                    yield from get_types_from(arg)
                return
            raise NotImplementedError(f"Unsupported origin type {origin!r} {annotation}")
        assert not args and not origin
    if annotation is None:
        yield FoundType("None" in in_namespace, ["None"], [None])
        return
    assert isinstance(
        annotation, type
    ), f"not a type - {annotation!r} {type(annotation)} {annotation.__module__}"
    if type_name.split(".")[0] in vars(builtins):
        return
    if f"{annotation.__module__}.{annotation.__name__}" != annotation.__qualname__:
        type_name = f"{annotation.__module__}.{annotation.__name__}"
    path = []
    target = types.SimpleNamespace(**in_namespace)
    path_values = []
    for step in type_name.split("."):
        path.append(step)
        try:
            target = getattr(target, step)
        except AttributeError as e:
            try:
                target = getattr(find_this(), path[0])
            except AttributeError:
                try:
                    # print('trying', path, type_name)
                    target = importlib.import_module(".".join(path))
                except ImportError:
                    raise e from None
        path_values.append(target)

    yield FoundType(path[0] in in_namespace, path, path_values)


def reify_annotations_in(
    namespace: Dict[str, Any], signature: inspect.Signature
) -> inspect.Signature:
    for index, param in enumerate(signature.parameters):
        param = signature.parameters[param]
        for result in get_types_from(param.annotation, namespace):
            if result.in_namespace:
                continue
            namespace[result.key] = result.value
            # print('setting', result.key, 'to', result.value)
    for result in get_types_from(signature.return_annotation):
        if result.in_namespace:
            continue
        namespace[result.key] = result.value
    return signature


def sanitize_return(func, ns):
    NOT_SET = object()
    sig = inspect.signature(func)
    if sig.return_annotation is inspect.Signature.empty:
        returns = NOT_SET
        for overload_func in get_overloads(func):
            overload_signature = reify_annotations_in(ns, inspect.signature(overload_func))
            # print(overload_signature)
            if returns is NOT_SET:
                returns = overload_signature.return_annotation
                continue
            returns |= overload_signature.return_annotation
        if returns is not NOT_SET:
            sig = sig.replace(return_annotation=returns)
        else:
            sig = sig.replace(return_annotation=Any)
    return sig


def safe_annotation_string_from(annotation):
    if str(annotation).startswith("<class "):
        annotation = annotation.__name__
    return annotation


def extract_key_from(
    keyname,
    args,
    kwargs,
    signature,
    delete_if_not_in_signature: bool = False,
    wrapper_signature=None,
) -> Optional[Any]:
    try:
        value = kwargs[keyname]
    except KeyError:
        if keyname in signature.parameters:
            for index, value in enumerate(tuple(signature.parameters)):
                value = signature.parameters[value]
                if value.name == keyname:
                    with suppress(IndexError):
                        value = args[index]
                        if delete_if_not_in_signature and wrapper_signature:
                            if keyname not in wrapper_signature.parameters:
                                del args[index]
                        return value
    else:
        if delete_if_not_in_signature and wrapper_signature:
            if keyname not in wrapper_signature.parameters:
                del kwargs[keyname]
        return value
    return None


def raw_param_body_from(function: inspect.Signature):
    sig_funccall = []
    for param_name in function.parameters:
        param = function.parameters[param_name]
        if param.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            sig_funccall.append(f"{param.name}")
        elif param.kind is inspect.Parameter.KEYWORD_ONLY:
            sig_funccall.append(f"{param.name}={param.name}")
        elif param.kind is inspect.Parameter.VAR_KEYWORD:
            sig_funccall.append(f"**{param.name}")
        elif param.kind is inspect.Parameter.VAR_POSITIONAL:
            sig_funccall.append(f"*{param.name}")
    return ", ".join(sig_funccall)


T = TypeVar("T")


def first(iterable: Iterable[T]) -> T:
    for item in iterable:
        return item


def indentation_length(s: str) -> int:
    length = 0
    if "\n" in s:
        for line in s.splitlines(True)[1:]:
            for char in line:
                if char == " ":
                    length += 1
                    continue
                break
            return length
    for char in s:
        if char == " ":
            length += 1
            continue
        break
    return length


INTERNAL_WRAPPER = """
def %(name)s%(args)s:
    _priv_format = %(format_kwarg)s
    if _priv_format not in (None, 'json', 'python', 'lines'):
        raise ValueError("Argument %(format_kwarg)s must be either None or one of 'json', 'python', 'lines'")
    result = _._original_%(name)s(%(sig_funccall)s)
    if silent:
        return result

    if _priv_format is None:
        _priv_format = 'lines'
    if _priv_format == "json":
        kwargs = {}
        if sys.stdout.isatty():
            kwargs = {"indent": 4, "sort_keys": True}
        try:
            print(json.dumps(result, **kwargs))
        except ValueError:
            print('Unable to render as json!', file=sys.stderr)
            _priv_format = "json"
        else:
            return result
    if _priv_format == "python":
        print(pprint.pformat(result))
        return result
    if _priv_format == 'lines':
        if isinstance(result, Mapping):
            for key in result:
                value = result[key]
                print(f"{key}:\t{value}")
            return result
        elif isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
            for item in result:
                print(item)
            return result
        if result is not None:
            print(result)
        return result
    return result

        """

PUBLIC_WRAPPER_FOR_INVOKE = """
def %(name)s%(args)s:
    _priv_format = %(format_kwarg)s
    if _priv_format not in (None, 'json', 'python', 'lines'):
        raise ValueError("Argument %(format_kwarg)s must be either None or one of 'json', 'python', 'lines'")
    result = this._._original_%(name)s(%(sig_funccall)s)
    if silent:
        return result

    if _priv_format is None:
        _priv_format = 'lines'
    if _priv_format == "json":
        kwargs = {}
        if sys.stdout.isatty():
            kwargs = {"indent": 4, "sort_keys": True}
        try:
            print(json.dumps(result, **kwargs))
        except ValueError:
            print('Unable to render as json!', file=sys.stderr)
            _priv_format = "json"
        else:
            return result
    if _priv_format == "python":
        print(pprint.pformat(result))
        return result
    if _priv_format == 'lines':
        if isinstance(result, Mapping):
            for key in result:
                value = result[key]
                print(f"{key}:\t{value}")
            return result
        elif isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
            for item in result:
                print(item)
            return result
        if result is not None:
            print(result)
        return result
    return result

"""


def task(callable_=None, /, **kwargs):
    def wrapper(func):
        # print("Called from", inspect.stack()[1].frame.f_globals["__name__"])
        # Make a read only copy
        stack = inspect.stack()[1:]
        task_frame = stack[0].frame
        while stack and task_frame.f_globals["__name__"] == __name__:
            del stack[0]
            task_frame = stack[0].frame
        assert task_frame.f_globals["__name__"] != __name__
        assert task_frame.f_globals["__name__"] == "tasks"
        support_module_name = Path(task_frame.f_globals["__file__"]).stem
        filename = f"_support_cache/{support_module_name}_{func.__name__}.py"
        with suppress(FileNotFoundError):
            os.remove(filename)
        if DEBUG_CODEGEN:
            os.makedirs("_support_cache", exist_ok=True)
        elif os.path.exists("_support_cache"):
            shutil.rmtree("_support_cache")
        this = sys.modules[task_frame.f_globals["__name__"]]
        if "this" not in task_frame.f_globals:
            task_frame.f_globals = this
        globalns = {
            "_origin_globals_ref": task_frame.f_globals,
            "__name__": task_frame.f_globals["__name__"],
            # '__file__': task_frame.f_globals["__file__"],
            "__builtins__": builtins,
            "__file__": filename,
            "ModuleType": types.ModuleType,
        }
        # populate global ns with a chain map:
        truly_local_modifications = {}
        localns = ChainMap(truly_local_modifications, task_frame.f_locals, task_frame.f_globals)
        module = importlib.util.module_from_spec(
            importlib.util.spec_from_file_location(f"tasksupport.support.{func.__name__}", filename)
        )
        localns.maps.append(globalns)
        code = """
this: Optional[ModuleType] = None

def __getattr__(name: str):
    '''
    Look up in original global ns. Effective ChainMap of namespaces.
    '''
    print('a')
    return _origin_globals_ref[name]

"""
        if DEBUG_CODEGEN:
            with open(filename, "a+") as fh:
                fh.write(code)
                fh.seek(0)
                code = fh.read()
        exec(compile(code, filename, "exec"), globalns, localns)
        globalns.update(truly_local_modifications)
        truly_local_modifications.clear()
        blank = ""
        sig = sanitize_return(func, module.__dict__)
        inner_function_call = sig
        is_contextable = False

        if sig.parameters:
            for param in sig.parameters:
                if is_context_param(sig.parameters[param]):
                    is_contextable = True
                break
        if not is_contextable:
            for index, param in enumerate(sig.parameters):
                param = sig.parameters[param]
                if not index:
                    continue
                if is_context_param(param) in ("type", "name_and_type"):
                    # okay, the context is definitely out of order
                    raise NotImplementedError(
                        "TODO: Implement generating an inner_function_call with rearranged values"
                    )
        prefix_params = []
        if not is_contextable:
            prefix_params = [
                inspect.Parameter("context", inspect.Parameter.POSITIONAL_ONLY, annotation=Context)
            ]

        additional_params = []
        if "silent" not in inner_function_call.parameters:
            silent = inspect.Parameter(
                "silent", inspect.Parameter.KEYWORD_ONLY, annotation=bool, default=False
            )
            additional_params.append(silent)
        format_key = "format"
        if format_key in inner_function_call.parameters:
            format_key = "format_"
        format_ = inspect.Parameter(
            format_key,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Optional[Literal["json", "python", "lines"]],
            default=None,
        )
        if format_key not in inner_function_call.parameters:
            additional_params.append(format_)
            kwargs.setdefault("help", {})
            kwargs["help"][
                format_key
            ] = f'may be one of "json", "python", "lines" (defaults to lines).'

        # Load into the local namespace any missing annotations necessary to run with when
        # we recreate the argument signature:
        new_signature = reify_annotations_in(
            localns,
            sig.replace(
                parameters=(
                    *prefix_params,
                    *sig.parameters.values(),
                    *additional_params,
                )
            ),
        )
        if "silent" in new_signature.parameters:
            kwargs.setdefault("help", {})
            kwargs["help"]["silent"] = "Set to reduce console output (defaults to false)"

        # Merge into the proxy module any missing deps
        module.__dict__.update(truly_local_modifications)
        # Merge in the new globals
        module.__dict__.update(globalns)

        def wrap_func(func):
            ns = ChainMap({}, localns, vars(module))
            signature = inspect.signature(func)
            code = INTERNAL_WRAPPER % dict(
                name=func.__name__,
                args=str(new_signature),
                sig_funccall=raw_param_body_from(signature),
                format_kwarg=format_key,
            )
            if DEBUG_CODEGEN:
                with open(filename, "a+") as fh:
                    fh.write(code)
                    fh.seek(0)
                    code = fh.read()

            exec(compile(code, filename, "exec"), task_frame.f_globals, ns)
            new_func = ns.maps[0][func.__name__]
            setattr(this._, f"_original_{func.__name__}", func)
            return new_func

        code = PUBLIC_WRAPPER_FOR_INVOKE % dict(
            name=func.__name__,
            args=str(new_signature),
            sig_funccall=raw_param_body_from(inner_function_call),
            format_kwarg=format_key,
        )
        # print(code)
        if DEBUG_CODEGEN:
            with open(filename, "a+") as fh:
                fh.write(code)
                fh.seek(0)
                code = fh.read()
        exec(compile(code, filename, "exec"), task_frame.f_globals, localns)
        setattr(this._, func.__name__, wrap_func(func))
        public_func = localns[func.__name__]
        indent = " " * indentation_length(func.__doc__ or blank)
        if ":returns:" not in (func.__doc__ or blank):
            func.__doc__ = f"{func.__doc__ or blank}\n{indent}:returns: {safe_annotation_string_from(new_signature.return_annotation)}"
        public_func.__doc__ = func.__doc__
        if kwargs:
            return _task(**kwargs)(public_func)
        return _task(public_func)

    if callable_ is not None:
        with suppress(AttributeError):
            name = callable_.__name__
            wrapper.__name__ = f"wrapper_for_{name}"
        return wrapper(callable_)
    wrapper.__name__ = f"wrapper_for_unnamed_caller"
    return wrapper
