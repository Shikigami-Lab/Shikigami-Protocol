"""Keep search dependency diagnostics from disclosing proxy credentials.

Call ``install_search_log_filter()`` after constructing DDGS (which lazily
imports its implementation) and before calling ``text()``. This affects only
already-created ddgs/primp loggers, including descendants. Call again for each
search so any newly loaded dependency loggers are covered. No handlers, logger
levels, transports or exception propagation are changed.
"""
import logging
import re
import threading


_INSTALL_LOCK = threading.Lock()
_URL_USERINFO = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s]*@", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"\b(?:password|passwd|pwd|secret|token|authorization|api[_-]?key|"
    r"access[_-]?key|credential|proxy[_-]?auth)\b", re.IGNORECASE,
)
_CATEGORY = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)\b")


def _dependency(name: str) -> bool:
    return name in ('ddgs', 'primp') or name.startswith(('ddgs.', 'primp.'))


class _SearchLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not _dependency(record.name):
            return True

        args = record.args.values() if isinstance(record.args, dict) else record.args
        values = [record.msg, *(args or ())]
        exceptions = [value for value in values if isinstance(value, BaseException)]
        if record.exc_info and record.exc_info[1] is not None:
            exceptions.append(record.exc_info[1])
        category = type(exceptions[0]).__name__ if exceptions else None
        try:
            message = record.getMessage()
        except Exception:
            # A broken diagnostic must not break a search or emit its raw args.
            message = ''
            category = category or 'LogFormattingError'

        textual_category = _CATEGORY.search(message)
        sensitive = (exceptions or category or record.exc_info or record.exc_text
                     or record.stack_info or textual_category
                     or _URL_USERINFO.search(message) or _CREDENTIAL.search(message)
                     or 'Traceback (most recent call last)' in message)
        if sensitive:
            category = category or (textual_category.group() if textual_category else None)
            # Never preserve exception messages, even if they lack a recognizable
            # credential key. A transport may emit a bare password in its repr.
            message = 'Search dependency diagnostic suppressed'
            if category and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', category):
                message += f' ({category})'
        record.msg = message
        record.args = ()
        record.message = message
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


_FILTER = _SearchLogFilter()


def install_search_log_filter() -> None:
    """Idempotently protect loaded DDGS/primp loggers before their handlers run.

    Ancestor logger filters do not run during propagation, so install on each
    existing descendant, not just the package logger or the root logger. This
    also protects root handlers added later (including test log capture).
    """
    with _INSTALL_LOCK:
        for name in ('ddgs', 'primp'):
            logging.getLogger(name).addFilter(_FILTER)
        for name, logger in logging.Logger.manager.loggerDict.copy().items():
            if _dependency(name) and isinstance(logger, logging.Logger):
                logger.addFilter(_FILTER)
