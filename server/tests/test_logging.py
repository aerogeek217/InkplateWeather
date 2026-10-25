"""configure_logging(): timestamped lines for uvicorn's loggers and ours, without clobbering other handlers."""
from __future__ import annotations

import io
import logging
import re

import pytest

from app import main

STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} ")


@pytest.fixture
def clean_loggers():
    names = ["", "inkplate", "app", "uvicorn.error", "uvicorn.access"]
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level) for n in names}
    yield
    for n, (handlers, level) in saved.items():
        lg = logging.getLogger(n)
        lg.handlers[:] = handlers
        lg.setLevel(level)


def test_adds_timestamped_root_handler_and_info_level(clean_loggers):
    root = logging.getLogger()
    root.handlers[:] = []
    logging.getLogger("inkplate").setLevel(logging.NOTSET)
    logging.getLogger("app").setLevel(logging.NOTSET)
    main.configure_logging()
    (h,) = root.handlers
    buf = io.StringIO()
    h.setStream(buf)
    logging.getLogger("inkplate").info("hello")
    logging.getLogger("app.cache").info("from a module")
    logging.getLogger("app.cache").debug("not shown")
    lines = buf.getvalue().splitlines()
    assert len(lines) == 2
    assert all(STAMP.match(ln) for ln in lines)
    assert lines[0].endswith("INFO    inkplate: hello")
    assert lines[1].endswith("app.cache: from a module")


def test_uvicorn_handlers_get_timestamps(clean_loggers):
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logging.getLogger("uvicorn.access").handlers[:] = [h]
    main.configure_logging()
    logging.getLogger("uvicorn.access").warning('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/health", "1.1", 200)
    out = buf.getvalue()
    assert STAMP.match(out)
    assert out.rstrip().endswith('uvicorn.access: 1.2.3.4:5 - "GET /health HTTP/1.1" 200')


def test_idempotent_and_leaves_existing_root_handlers(clean_loggers):
    root = logging.getLogger()
    mine = logging.NullHandler()
    root.handlers[:] = [mine]
    main.configure_logging()
    main.configure_logging()
    assert root.handlers == [mine]
    root.handlers[:] = []
    main.configure_logging()
    main.configure_logging()
    assert len(root.handlers) == 1
