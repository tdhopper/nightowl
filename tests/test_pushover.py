from __future__ import annotations

import io
import json
import logging
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from nightowl import pushover as pushover_mod


@pytest.fixture(autouse=True)
def tmp_credentials(tmp_path, monkeypatch):
    """Isolate the on-disk Pushover credential paths from ~/.config/pushover/."""
    token_path = tmp_path / "token"
    user_path = tmp_path / "user"
    monkeypatch.setattr(pushover_mod, "PUSHOVER_TOKEN_PATH", token_path)
    monkeypatch.setattr(pushover_mod, "PUSHOVER_USER_PATH", user_path)
    return token_path, user_path


@pytest.fixture
def logger():
    return logging.getLogger("test")


def _ok_response() -> MagicMock:
    resp = MagicMock()
    resp.status = 200
    resp.read.return_value = json.dumps({"status": 1, "request": "abc"}).encode()
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


class TestBuildFailureMessage:
    def test_title_singular_and_plural(self):
        one = [{"task_id": "a", "result": "failure", "error": "boom"}]
        title, _ = pushover_mod.build_failure_message(one)
        assert title == "nightowl: 1 task failed"

        two = [
            {"task_id": "a", "result": "failure", "error": "boom"},
            {"task_id": "b", "result": "failure", "error": "kaboom"},
        ]
        title, _ = pushover_mod.build_failure_message(two)
        assert title == "nightowl: 2 tasks failed"

    def test_title_includes_project_basename(self):
        runs = [{"task_id": "a", "result": "failure", "error": "boom"}]
        title, _ = pushover_mod.build_failure_message(
            runs, project_dir="/Users/tim/repos/handbook",
        )
        assert title == "nightowl: 1 task failed in handbook"

    def test_message_lists_each_failure_first_line_only(self):
        runs = [
            {"task_id": "content-gap", "result": "failure",
             "error": "ENOTFOUND api.reddit.com\nstack trace line 2"},
            {"task_id": "reddit-scout", "result": "failure", "error": "timeout"},
        ]
        _, message = pushover_mod.build_failure_message(runs)
        assert "content-gap: ENOTFOUND api.reddit.com" in message
        assert "reddit-scout: timeout" in message
        # Only the first line of the multi-line error
        assert "stack trace line 2" not in message

    def test_missing_error_gets_placeholder(self):
        runs = [{"task_id": "a", "result": "failure", "error": None}]
        _, message = pushover_mod.build_failure_message(runs)
        assert "a: (no error message)" in message


class TestSendFailureNotification:
    def test_no_failures_skips(self, logger, tmp_credentials):
        token_path, user_path = tmp_credentials
        token_path.write_text("t")
        user_path.write_text("u")
        with patch("nightowl.pushover.urllib.request.urlopen") as mock_open:
            sent = pushover_mod.send_failure_notification(
                [{"task_id": "a", "result": "success"}], logger,
            )
            assert sent is False
            mock_open.assert_not_called()

    def test_empty_runs_skips(self, logger):
        with patch("nightowl.pushover.urllib.request.urlopen") as mock_open:
            sent = pushover_mod.send_failure_notification([], logger)
            assert sent is False
            mock_open.assert_not_called()

    def test_missing_credentials_skips(self, logger, tmp_credentials):
        token_path, user_path = tmp_credentials
        assert not token_path.exists()
        assert not user_path.exists()
        with patch("nightowl.pushover.urllib.request.urlopen") as mock_open:
            sent = pushover_mod.send_failure_notification(
                [{"task_id": "a", "result": "failure", "error": "boom"}], logger,
            )
            assert sent is False
            mock_open.assert_not_called()

    def test_one_credential_missing_skips(self, logger, tmp_credentials):
        token_path, user_path = tmp_credentials
        token_path.write_text("t")
        # user_path missing
        with patch("nightowl.pushover.urllib.request.urlopen") as mock_open:
            sent = pushover_mod.send_failure_notification(
                [{"task_id": "a", "result": "failure", "error": "boom"}], logger,
            )
            assert sent is False
            mock_open.assert_not_called()

    def test_empty_credential_file_skips(self, logger, tmp_credentials):
        token_path, user_path = tmp_credentials
        token_path.write_text("")
        user_path.write_text("u")
        with patch("nightowl.pushover.urllib.request.urlopen") as mock_open:
            sent = pushover_mod.send_failure_notification(
                [{"task_id": "a", "result": "failure", "error": "boom"}], logger,
            )
            assert sent is False
            mock_open.assert_not_called()

    def test_posts_to_pushover_api(self, logger, tmp_credentials):
        token_path, user_path = tmp_credentials
        token_path.write_text("tok_123")
        user_path.write_text("usr_456")
        with patch(
            "nightowl.pushover.urllib.request.urlopen",
            return_value=_ok_response(),
        ) as mock_open:
            sent = pushover_mod.send_failure_notification(
                [
                    {"task_id": "a", "result": "failure", "error": "boom"},
                    {"task_id": "b", "result": "success"},
                ],
                logger,
                project_dir="/Users/x/repos/handbook",
            )
            assert sent is True
            assert mock_open.call_count == 1

            req = mock_open.call_args[0][0]
            assert req.full_url == pushover_mod.PUSHOVER_API_URL
            assert req.method == "POST"

            payload = dict(urllib.parse.parse_qsl(req.data.decode()))
            assert payload["token"] == "tok_123"
            assert payload["user"] == "usr_456"
            assert payload["title"] == "nightowl: 1 task failed in handbook"
            assert "a: boom" in payload["message"]
            # Successful task does not appear in the body
            assert "b:" not in payload["message"]

    def test_non_2xx_response_does_not_raise(self, logger, tmp_credentials):
        token_path, user_path = tmp_credentials
        token_path.write_text("t")
        user_path.write_text("u")
        resp = MagicMock()
        resp.status = 500
        resp.read.return_value = b"server error"
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with patch(
            "nightowl.pushover.urllib.request.urlopen", return_value=resp,
        ):
            sent = pushover_mod.send_failure_notification(
                [{"task_id": "a", "result": "failure", "error": "boom"}], logger,
            )
            assert sent is False

    def test_pushover_rejection_does_not_raise(self, logger, tmp_credentials):
        token_path, user_path = tmp_credentials
        token_path.write_text("t")
        user_path.write_text("u")
        resp = MagicMock()
        resp.status = 200
        resp.read.return_value = json.dumps(
            {"status": 0, "errors": ["application token is invalid"]}
        ).encode()
        resp.__enter__.return_value = resp
        resp.__exit__.return_value = False
        with patch(
            "nightowl.pushover.urllib.request.urlopen", return_value=resp,
        ):
            sent = pushover_mod.send_failure_notification(
                [{"task_id": "a", "result": "failure", "error": "boom"}], logger,
            )
            assert sent is False

    def test_http_error_does_not_raise(self, logger, tmp_credentials):
        token_path, user_path = tmp_credentials
        token_path.write_text("t")
        user_path.write_text("u")
        err = urllib.error.HTTPError(
            url=pushover_mod.PUSHOVER_API_URL,
            code=400,
            msg="Bad Request",
            hdrs=None,
            fp=io.BytesIO(b'{"status":0}'),
        )
        with patch(
            "nightowl.pushover.urllib.request.urlopen", side_effect=err,
        ):
            sent = pushover_mod.send_failure_notification(
                [{"task_id": "a", "result": "failure", "error": "boom"}], logger,
            )
            assert sent is False

    def test_url_error_does_not_raise(self, logger, tmp_credentials):
        token_path, user_path = tmp_credentials
        token_path.write_text("t")
        user_path.write_text("u")
        with patch(
            "nightowl.pushover.urllib.request.urlopen",
            side_effect=urllib.error.URLError("dns failure"),
        ):
            sent = pushover_mod.send_failure_notification(
                [{"task_id": "a", "result": "failure", "error": "boom"}], logger,
            )
            assert sent is False
