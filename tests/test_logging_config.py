from __future__ import annotations

import datetime
import logging
import os
import tempfile
import unittest
from unittest.mock import patch

from pullwise_server import app, logging_config


def timestamp(year: int, month: int, day: int, hour: int, minute: int) -> float:
    return datetime.datetime(year, month, day, hour, minute).timestamp()


class LoggingConfigTest(unittest.TestCase):
    def test_daily_file_handler_writes_records_to_dated_log_files(self) -> None:
        with tempfile.TemporaryDirectory() as log_dir:
            handler = logging_config.DailyDatedFileHandler(
                log_dir,
                rotation_time=datetime.time(0, 0),
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            record = logging.LogRecord(
                "pullwise_server.test",
                logging.INFO,
                __file__,
                1,
                "hello daily log",
                (),
                None,
            )
            record.created = timestamp(2026, 5, 23, 10, 30)

            try:
                handler.emit(record)
            finally:
                handler.close()

            with open(os.path.join(log_dir, "pullwise-2026-05-23.log"), "r", encoding="utf-8") as log_file:
                self.assertIn("hello daily log", log_file.read())

    def test_daily_file_handler_uses_configured_rotation_time_as_day_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as log_dir:
            handler = logging_config.DailyDatedFileHandler(
                log_dir,
                rotation_time=datetime.time(6, 0),
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            before_boundary = logging.LogRecord("pullwise_server.test", logging.INFO, __file__, 1, "before", (), None)
            before_boundary.created = timestamp(2026, 5, 23, 5, 59)
            after_boundary = logging.LogRecord("pullwise_server.test", logging.INFO, __file__, 1, "after", (), None)
            after_boundary.created = timestamp(2026, 5, 23, 6, 0)

            try:
                handler.emit(before_boundary)
                handler.emit(after_boundary)
            finally:
                handler.close()

            with open(os.path.join(log_dir, "pullwise-2026-05-22.log"), "r", encoding="utf-8") as log_file:
                self.assertIn("before", log_file.read())
            with open(os.path.join(log_dir, "pullwise-2026-05-23.log"), "r", encoding="utf-8") as log_file:
                self.assertIn("after", log_file.read())

    def test_configure_logging_installs_daily_file_handler_from_environment(self) -> None:
        root_logger = logging.getLogger()
        original_handlers = list(root_logger.handlers)
        original_level = root_logger.level

        with tempfile.TemporaryDirectory() as log_dir:
            try:
                with patch.dict(
                    os.environ,
                    {
                        "PULLWISE_LOG_DIR": log_dir,
                        "PULLWISE_LOG_LEVEL": "DEBUG",
                        "PULLWISE_LOG_ROTATION_TIME": "06:30",
                    },
                    clear=True,
                ):
                    logging_config.configure_logging(project_root=os.getcwd())

                daily_handlers = [
                    handler
                    for handler in root_logger.handlers
                    if isinstance(handler, logging_config.DailyDatedFileHandler)
                ]
                self.assertEqual(1, len(daily_handlers))
                self.assertEqual(log_dir, daily_handlers[0].directory)
                self.assertEqual(datetime.time(6, 30), daily_handlers[0].rotation_time)
                self.assertEqual(logging.DEBUG, root_logger.level)
            finally:
                for handler in list(root_logger.handlers):
                    if handler not in original_handlers:
                        handler.close()
                root_logger.handlers = original_handlers
                root_logger.setLevel(original_level)

    def test_main_configures_logging_before_serving_requests(self) -> None:
        class ServerStub:
            def __init__(self) -> None:
                self.closed = False

            def serve_forever(self) -> None:
                raise KeyboardInterrupt

            def server_close(self) -> None:
                self.closed = True

        server = ServerStub()

        with (
            patch("sys.argv", ["pullwise-server"]),
            patch.object(app, "load_env_file"),
            patch.object(app.logging_config, "configure_logging") as configure_logging,
            patch.object(app, "PullwiseThreadingHTTPServer", return_value=server),
            patch("builtins.print"),
        ):
            app.main()

        configure_logging.assert_called_once_with(project_root=app.project_root())
        self.assertTrue(server.closed)

    def test_http_request_logs_use_access_logger(self) -> None:
        handler = app.PullwiseHandler.__new__(app.PullwiseHandler)
        handler.client_address = ("127.0.0.1", 4000)

        with (
            patch.object(app.access_logger, "info") as access_info,
            patch("builtins.print") as print_output,
        ):
            app.PullwiseHandler.log_message(handler, "%s %s", "GET /health", "200")

        access_info.assert_called_once_with("%s - %s", "127.0.0.1", "GET /health 200")
        print_output.assert_not_called()


if __name__ == "__main__":
    unittest.main()
