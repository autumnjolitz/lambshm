#!/usr/bin/env python
"""Test handler to showcase operational `shm_open(2)` access.

Uses multiprocessing.Queue and multiprocessing.Semaphore to prove functionality.
"""

import functools
import operator
import os.path
import random
import logging
from contextlib import nullcontext, closing
from multiprocessing import get_context, Queue, Semaphore
from typing import Optional, Any, Dict, List

module_name: str = f"{os.path.splitext(__file__)[0]}" if __name__ == "__main__" else __name__
logger: logging.Logger = logging.getLogger(module_name)

# process globals:

_fork_queue: Optional[Queue] = None
_fork_semaphore: Optional[Semaphore] = None


def handler(event: Dict[str, str], context: Any) -> Dict[str, List[str]]:
    global _fork_semaphore, _fork_queue
    logger = logging.getLogger(f"{module_name}.handler")

    event.setdefault("limit", 23)
    limit = int(event["limit"])

    args = range(limit)
    context = get_context("fork")
    _fork_semaphore = context.Semaphore(4)
    _fork_queue = context.Queue()

    with context.Pool() as p, closing(_fork_queue) as queue:
        result = p.map(slow_call, args)
        logger.info(f"Single map returned {args!r} -> {result!r}")
        result = p.map(functools.partial(slow_call, lock=True, queue=True), args)
        with _fork_semaphore:
            result_q = []
            for _ in range(limit):
                result_q.append(queue.get())
            logger.info(f"Queue returned {result_q!r}")
        if frozenset(result_q) != frozenset(result):
            raise ValueError("This isn't right! results should be identical!")
    return {"by_return": list(result), "by_queue": list(result_q)}


def slow_call(value: int, queue: Optional[Queue] = None, lock: Optional[Semaphore] = None) -> int:
    logger = logging.getLogger(f"{module_name}.slow_call")
    if lock is True:
        lock = _fork_semaphore
    if queue is True:
        queue = _fork_queue

    logger.debug(f"Queue is {queue!r}, lock is {lock!r}")

    with lock or nullcontext():
        op = random.choice([operator.pow, operator.add])
        # time.sleep(random.randrange(0, 1))
        val = op(value, abs(value - 2) or 1)
        if queue is not None:
            queue.put(val)
        return val


if __name__ == "__main__":
    import argparse

    def _setup_logging(debug: bool = False):
        logger.setLevel(logging.DEBUG)
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        if debug:
            handler.setLevel(logging.DEBUG)
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        logger.addHandler(handler)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-d", "--debug", action="store_true", default=False)
    parser.add_argument("limit", default=23, nargs="?")
    args = parser.parse_args()
    _setup_logging(args.debug)
    handler({"limit": args.limit}, None)
