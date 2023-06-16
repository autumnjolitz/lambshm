import inspect
import json
import sys
import functools
import types
import dataclasses
import pprint
import typing
import importlib
import builtins
from typing import Any, Literal, Iterable

from invoke import task as _task
from invoke.context import Context
from contextlib import suppress
from typing import Tuple, Optional, Dict, TypeVar
from collections.abc import Mapping as AbstractMapping, Iterable as AbstractIterable

try:
    from typing import get_overloads
except ImportError:
    from typing_extensions import get_overloads


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


def this() -> types.ModuleType:
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
        in_namespace = vars(this())
    if annotation is inspect.Signature.empty:
        annotation = Any
    if isinstance(annotation, str):
        ns = {}
        exec(f"annotation = {annotation!s}", vars(this()), ns)
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
                target = getattr(this(), path[0])
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


def task(func):
    # print("Called from", inspect.stack()[1].frame.f_globals["__name__"])
    globalns = inspect.stack()[1].frame.f_globals
    localns = inspect.stack()[1].frame.f_locals or {}
    blank = ""
    this = sys.modules[globalns["__name__"]]
    # print('this', this, 'for', this.__name__)
    globalns.update(
        {
            "NoneType": type(None),
            "this": this,
            "typing": typing,
            "Optional": Optional,
            "pprint": pprint,
            "json": json,
            "Union": typing.Union,
            "Any": Any,
            "sys": sys,
            "AbstractMapping": AbstractMapping,
            "AbstractIterable": AbstractIterable,
        }
    )
    ns = {**localns}
    sig = sanitize_return(func, ns)
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

    def wrap_func(func):
        ns = {**localns}
        signature = inspect.signature(func)
        code = """
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
        if isinstance(result, AbstractMapping):
            for key in result:
                value = result[key]
                print(f"{key}:\t{value}")
            return result
        elif isinstance(result, AbstractIterable) and not isinstance(result, (str, bytes)):
            for item in result:
                print(item)
            return result
        if result is not None:
            print(result)
        return result
    return result

        """ % dict(
            name=func.__name__,
            args=str(new_signature),
            sig_funccall=raw_param_body_from(signature),
            format_kwarg=format_key,
        )
        exec(code, globalns, ns)
        new_func = ns[func.__name__]
        setattr(this._, f"_original_{func.__name__}", func)
        return new_func

    code = """
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
        if isinstance(result, AbstractMapping):
            for key in result:
                value = result[key]
                print(f"{key}:\t{value}")
            return result
        elif isinstance(result, AbstractIterable) and not isinstance(result, (str, bytes)):
            for item in result:
                print(item)
            return result
        if result is not None:
            print(result)
        return result
    return result

""" % dict(
        name=func.__name__,
        args=str(new_signature),
        sig_funccall=raw_param_body_from(inner_function_call),
        format_kwarg=format_key,
    )
    # print(code)
    exec(code, globalns, ns)
    setattr(this._, func.__name__, wrap_func(func))
    wrapped_func = ns[func.__name__]
    wrapped_func.__doc__ = f"{func.__doc__ or blank}\n:returns: {safe_annotation_string_from(new_signature.return_annotation)}"
    return _task(wrapped_func)
