"""Offline dependency-log protection, including the real DDGS dispatcher."""
import io
import logging
import sys
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from src.tools.websearch.log_safety import install_search_log_filter


class SearchLogSafetyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.output = io.StringIO()
        self.records = []
        records = self.records

        class Capture(logging.StreamHandler):
            def emit(self, record):
                records.append(record)
                super().emit(record)

        handler = Capture(self.output)
        handler.setFormatter(logging.Formatter('%(name)s %(levelname)s %(message)s'))
        root = logging.getLogger()
        self.stack.enter_context(patch.object(root, 'handlers', [handler]))
        self.stack.enter_context(patch.object(root, 'level', logging.DEBUG))
        self.loggers = [logging.getLogger(name) for name in (
            'ddgs', 'ddgs.ddgs', 'ddgs.http_client', 'ddgs.engines.synthetic',
            'primp', 'primp.synthetic.transport', 'unrelated.search',
        )]
        for logger in self.loggers:
            self.stack.enter_context(patch.object(logger, 'filters', list(logger.filters)))
            self.stack.enter_context(patch.object(logger, 'handlers', []))
            self.stack.enter_context(patch.object(logger, 'level', logging.NOTSET))
            self.stack.enter_context(patch.object(logger, 'propagate', True))
            self.stack.enter_context(patch.object(logger, 'disabled', False))

    def assert_no_secrets(self):
        rendered = self.output.getvalue()
        captured = repr([r.__dict__ for r in self.records])
        for secret in ('synthetic-user', 'synthetic-password', 'encoded%40password',
                       'synthetic-token', 'synthetic-traceback'):
            self.assertNotIn(secret, rendered)
            self.assertNotIn(secret, captured)
        self.assertNotIn('Traceback (most recent call last)', rendered)

    def test_descendants_url_credentials_and_plain_messages_reach_root_safely(self):
        install_search_log_filter()
        for name in ('ddgs.engines.synthetic', 'primp.synthetic.transport'):
            logger = logging.getLogger(name)
            logger.info('Connecting %s', 'socks5://synthetic-user:encoded%40password@proxy.invalid:1080')
            logger.info('request failed: password=synthetic-password')
            logger.info('headers: %s', {'Authorization': 'Bearer synthetic-token'})
            logger.info('Received %d results', 5)
        self.assert_no_secrets()
        self.assertEqual(self.output.getvalue().count('Received 5 results'), 2)

    def test_exception_args_message_and_cached_traceback_are_removed(self):
        install_search_log_filter()
        logger = logging.getLogger('ddgs.http_client')
        error = RuntimeError('proxy http://synthetic-user:synthetic-password@proxy.invalid')
        logger.info('Error in engine %s: %r', 'synthetic', error)
        logger.warning(error)
        logger.info('failure %(error)r', {'error': error})
        try:
            raise error
        except RuntimeError:
            record = logger.makeRecord(logger.name, logging.ERROR, __file__, 1,
                                       'transport failed', (), sys.exc_info())
            record.exc_text = 'synthetic-traceback synthetic-password'
            record.stack_info = 'synthetic-traceback synthetic-password'
            logger.handle(record)
        self.assert_no_secrets()
        self.assertIn('RuntimeError', self.output.getvalue())
        self.assertTrue(all(r.exc_info is None and not r.exc_text and not r.stack_info
                            for r in self.records))

    def test_install_is_idempotent_and_does_not_filter_other_libraries(self):
        install_search_log_filter()
        counts = [len(logger.filters) for logger in self.loggers]
        install_search_log_filter()
        self.assertEqual(counts, [len(logger.filters) for logger in self.loggers])
        logging.getLogger('unrelated.search').warning('password=unrelated-marker')
        self.assertIn('password=unrelated-marker', self.output.getvalue())

    def test_root_capture_added_after_install_is_protected(self):
        install_search_log_filter()
        with self.assertLogs(level=logging.INFO) as capture:
            logging.getLogger('primp.synthetic.transport').info(
                'failure: %r', RuntimeError('synthetic-password'))
        self.assertNotIn('synthetic-password', repr(capture.output))
        self.assertNotIn('synthetic-password', repr(capture.records[0].__dict__))
        self.assertIn('RuntimeError', capture.output[0])

    def test_reinstall_covers_newly_loaded_descendant(self):
        install_search_log_filter()
        logger = logging.getLogger('ddgs.later_loaded.synthetic')
        with patch.object(logger, 'filters', []):
            install_search_log_filter()
            logger.warning('authorization=synthetic-token')
        self.assert_no_secrets()
        self.assertIn('suppressed', self.output.getvalue())

    def test_preformatted_exception_and_bad_formatting_do_not_leak(self):
        install_search_log_filter()
        logger = logging.getLogger('primp.synthetic.transport')
        logger.info("RuntimeError('synthetic-password')")
        logger.info('invalid %d', 'synthetic-password')
        self.assert_no_secrets()
        self.assertIn('RuntimeError', self.output.getvalue())
        self.assertIn('LogFormattingError', self.output.getvalue())

    def test_real_ddgs_dispatcher_logs_safely_and_still_raises(self):
        from ddgs import DDGS
        from ddgs.engines import ENGINES
        from ddgs.exceptions import DDGSException

        error = RuntimeError('proxy http://synthetic-user:synthetic-password@proxy.invalid')
        calls = []

        class SyntheticEngine:
            name = 'synthetic'
            provider = 'synthetic'
            priority = 1

            def __init__(self, **kwargs):
                pass  # No HTTP client or transport is constructed.

            def search(self, *args, **kwargs):
                calls.append('search')
                raise error

        with patch.dict(ENGINES, {'text': {'synthetic': SyntheticEngine}}):
            with DDGS(timeout=1) as client:
                install_search_log_filter()  # After lazy imports, before text().
                with self.assertRaises(DDGSException) as raised:
                    client.text('synthetic query', backend='synthetic', max_results=5)
        self.assertEqual(calls, ['search'])
        self.assertIn('synthetic-password', str(raised.exception))  # Errors are not swallowed/rewritten.
        self.assert_no_secrets()
        self.assertIn('RuntimeError', self.output.getvalue())

    def test_real_ddgs_empty_engine_remains_an_error(self):
        from ddgs import DDGS
        from ddgs.engines import ENGINES
        from ddgs.exceptions import DDGSException

        class EmptyEngine:
            name = 'synthetic'
            provider = 'synthetic'
            priority = 1

            def __init__(self, **kwargs):
                pass

            def search(self, *args, **kwargs):
                return []

        with patch.dict(ENGINES, {'text': {'synthetic': EmptyEngine}}):
            with DDGS(timeout=1) as client:
                install_search_log_filter()
                with self.assertRaises(DDGSException):
                    client.text('synthetic query', backend='synthetic', max_results=5)


if __name__ == '__main__':
    unittest.main()
