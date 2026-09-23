import base64
import email
import email.policy
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from google.auth.exceptions import RefreshError

logging.disable(logging.CRITICAL)

import anthropic

from ctc_agent import (CtcAgent, NeedsApiKey, NeedsAuth, automated_reason, build_reply,
                       load_credentials, message_text)


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode()


def fake_service(messages, labels=(), answered_threads=()):
    """A MagicMock Gmail service whose search returns `messages` (id -> headers dict).

    A message's body is its "Body" header value; threads in `answered_threads` contain a SENT message.
    """
    service = MagicMock()
    msgs = service.users.return_value.messages.return_value
    msgs.list.return_value.execute.return_value = {"messages": [{"id": i} for i in messages]}

    def get(userId, id, **_):
        headers = dict(messages[id])
        body = headers.pop("Body", "")
        return MagicMock(execute=lambda: {
            "id": id, "threadId": f"t-{id}",
            "payload": {"mimeType": "text/plain", "body": {"data": b64(body)},
                        "headers": [{"name": k, "value": v} for k, v in headers.items()]},
        })

    msgs.get.side_effect = get
    service.users.return_value.threads.return_value.get.side_effect = \
        lambda userId, id, **_: MagicMock(execute=lambda: {"messages": [
            {"labelIds": ["SENT"] if id in answered_threads else ["INBOX"]}]})
    lbls = service.users.return_value.labels.return_value
    lbls.list.return_value.execute.return_value = {"labels": list(labels)}
    lbls.create.side_effect = lambda userId, body: MagicMock(
        execute=lambda: {"id": f"Label_{body['name']}"})
    return service, msgs


class FakeClassifier:
    """Says an email asks about CTC when its body contains "about CTC"."""

    def __init__(self):
        self.calls = []

    def wants_ctc_info(self, sender, subject, body):
        self.calls.append(subject)
        return "about CTC" in body


def sent_emails(msgs):
    out = []
    for call in msgs.send.call_args_list:
        body = call.kwargs["body"]
        out.append((email.message_from_bytes(base64.urlsafe_b64decode(body["raw"])), body["threadId"]))
    return out


class AutomatedReasonTest(unittest.TestCase):
    def test_detects_machine_and_bulk_mail(self):
        self.assertIsNone(automated_reason({"from": "a@x.com"}))
        self.assertIsNone(automated_reason({"auto-submitted": "no"}))
        self.assertEqual(automated_reason({"auto-submitted": "auto-replied"}), "automated message")
        self.assertEqual(automated_reason({"precedence": "Bulk"}), "bulk mail")
        self.assertEqual(automated_reason({"list-unsubscribe": "<mailto:u@x>"}), "mailing list")


class MessageTextTest(unittest.TestCase):
    def test_prefers_plain_text(self):
        payload = {"mimeType": "multipart/alternative", "parts": [
            {"mimeType": "text/html", "body": {"data": b64("<p>html</p>")}},
            {"mimeType": "text/plain", "body": {"data": b64("plain")}},
        ]}
        self.assertEqual(message_text(payload), "plain")

    def test_strips_html(self):
        html = "<style>p{}</style><p>Tell me&nbsp;about <b>CTC</b></p>"
        payload = {"mimeType": "text/html", "body": {"data": b64(html)}}
        self.assertEqual(message_text(payload), "Tell me about CTC")

    def test_decodes_charset(self):
        payload = {"mimeType": "text/plain", "headers": [
            {"name": "Content-Type", "value": 'text/plain; charset="big5"'}],
            "body": {"data": base64.urlsafe_b64encode("香柏木".encode("big5")).decode()}}
        self.assertEqual(message_text(payload), "香柏木")


class BuildReplyTest(unittest.TestCase):
    def test_threads_reply(self):
        body = build_reply({"subject": "[CTC] hi", "from": "Ann <ann@x.com>",
                            "message-id": "<m2@x>", "references": "<m1@x>"}, "t1")
        msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
        self.assertEqual(body["threadId"], "t1")
        self.assertEqual(msg["To"], "Ann <ann@x.com>")
        self.assertEqual(msg["Subject"], "Re: [CTC] hi")
        self.assertEqual(msg["In-Reply-To"], "<m2@x>")
        self.assertEqual(msg["References"], "<m1@x> <m2@x>")
        self.assertEqual(msg["Auto-Submitted"], "auto-replied")
        text = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]),
                                        policy=email.policy.default).get_content()
        self.assertTrue(text.startswith("Hi Ann,"))
        self.assertIn("https://www.cedartc.org", text)

    def test_prefers_reply_to(self):
        body = build_reply({"subject": "[CTC] hi", "from": "a@x.com", "reply-to": "list@x.com"}, "t")
        msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
        self.assertEqual(msg["To"], "list@x.com")
        self.assertIsNone(msg["In-Reply-To"])


class AgentTest(unittest.TestCase):
    def test_replies_only_to_ctc_inquiries_and_labels_everything(self):
        service, msgs = fake_service({
            "a": {"Subject": "Question", "From": "a@x.com", "Message-ID": "<a@x>",
                  "Body": "Can you tell me about CTC?"},
            "b": {"Subject": "Lunch", "From": "b@x.com", "Body": "Lunch on Friday?"},
        })
        classifier = FakeClassifier()
        agent = CtcAgent(service, 100, classifier)
        self.assertEqual(agent.poll_once(), 1)

        query = msgs.list.call_args.kwargs["q"]
        for part in ("in:inbox", "-from:me", "-label:ctc-acked", "-label:ctc-checked", "after:100"):
            self.assertIn(part, query)
        [(sent, thread)] = sent_emails(msgs)
        self.assertEqual(thread, "t-a")
        self.assertEqual(sent["To"], "a@x.com")
        self.assertEqual(msgs.modify.call_args_list, [
            mock.call(userId="me", id="a",
                      body={"addLabelIds": ["Label_ctc-checked", "Label_ctc-acked"]}),
            mock.call(userId="me", id="b", body={"addLabelIds": ["Label_ctc-checked"]}),
        ])
        create = service.users.return_value.labels.return_value.create
        created = {c.kwargs["body"]["name"]: c.kwargs["body"] for c in create.call_args_list}
        self.assertEqual(created["ctc-checked"]["messageListVisibility"], "hide")
        self.assertEqual(created["ctc-acked"]["messageListVisibility"], "show")

    def test_skips_automated_mail_and_answered_threads_without_asking_claude(self):
        service, msgs = fake_service({
            "a": {"Subject": "News", "From": "n@x.com", "List-Id": "<news.x.com>",
                  "Body": "All about CTC"},
            "b": {"Subject": "Re: hi", "From": "b@x.com", "Body": "more about CTC"},
            "c": {"Subject": "Out of office", "From": "c@x.com", "Auto-Submitted": "auto-replied",
                  "Body": "about CTC"},
            "d": {"Subject": "No sender", "Body": "about CTC"},
        }, answered_threads={"t-b"})
        classifier = FakeClassifier()
        self.assertEqual(CtcAgent(service, 0, classifier).poll_once(), 0)
        self.assertEqual(classifier.calls, [])
        msgs.send.assert_not_called()
        self.assertEqual(msgs.modify.call_count, 4)

    def test_reuses_existing_labels_and_never_double_replies(self):
        service, msgs = fake_service(
            {"a": {"Subject": "Hi", "From": "a@x.com", "Body": "about CTC"}},
            labels=[{"id": "Label_7", "name": "ctc-acked"}, {"id": "Label_8", "name": "ctc-checked"}],
        )
        agent = CtcAgent(service, 0, FakeClassifier())
        agent.poll_once()
        agent.poll_once()  # search still returns "a" (e.g. label not yet indexed)
        self.assertEqual(msgs.send.call_count, 1)
        service.users.return_value.labels.return_value.create.assert_not_called()
        msgs.modify.assert_called_once_with(
            userId="me", id="a", body={"addLabelIds": ["Label_8", "Label_7"]})

    def test_classifier_failure_leaves_message_for_next_poll(self):
        service, msgs = fake_service({"a": {"Subject": "Hi", "From": "a@x.com"}})
        classifier = MagicMock()
        classifier.wants_ctc_info.side_effect = RuntimeError("overloaded")
        with self.assertRaises(RuntimeError):
            CtcAgent(service, 0, classifier).poll_once()
        msgs.modify.assert_not_called()

    def test_run_backs_off_on_errors_and_stops(self):
        service, msgs = fake_service({})
        msgs.list.return_value.execute.side_effect = RuntimeError("network down")
        agent = CtcAgent(service, 0, FakeClassifier(), poll_interval=5)
        delays = []

        def fake_sleep(seconds):
            delays.append(seconds)
            if len(delays) == 3:
                agent.stop()

        agent._sleep = fake_sleep
        agent.run()
        self.assertEqual(delays, [10, 20, 40])

    def test_reports_actions_to_listener(self):
        service, msgs = fake_service({
            "a": {"Subject": "Hi", "From": "Ann <a@x.com>", "Body": "about CTC"},
            "b": {"Subject": "Lunch", "From": "b@x.com"},
        })
        events = []
        agent = CtcAgent(service, 0, FakeClassifier(), poll_interval=5,
                         listener=lambda event, **d: events.append((event, d)))

        def fake_sleep(seconds):
            if len(events) == 3:
                msgs.list.return_value.execute.side_effect = RuntimeError("offline")
            else:
                agent.stop()

        agent._sleep = fake_sleep
        agent.run()
        self.assertEqual(events, [
            ("acked", {"subject": "Hi", "sender": "Ann <a@x.com>"}),
            ("ignored", {"subject": "Lunch", "sender": "b@x.com", "reason": "not asking about CTC"}),
            ("checked", {"acked": 1}),
            ("error", {"error": "offline", "retry_in": 10}),
        ])

    def test_stop_skips_remaining_messages(self):
        service, msgs = fake_service({
            "a": {"Subject": "one", "From": "a@x.com", "Body": "about CTC"},
            "b": {"Subject": "two", "From": "b@x.com", "Body": "about CTC"},
        })
        agent = CtcAgent(service, 0, FakeClassifier(), listener=lambda event, **d: agent.stop())
        self.assertEqual(agent.poll_once(), 1)

    def test_revoked_sign_in_stops_instead_of_retrying(self):
        service, msgs = fake_service({})
        msgs.list.return_value.execute.side_effect = RefreshError("invalid_grant")
        agent = CtcAgent(service, 0, FakeClassifier())
        agent._sleep = lambda s: self.fail("should not retry")
        with self.assertRaises(NeedsAuth):
            agent.run()

    def test_rejected_api_key_stops_instead_of_retrying(self):
        service, msgs = fake_service({"a": {"Subject": "Hi", "From": "a@x.com"}})
        classifier = MagicMock()
        response = MagicMock(status_code=401, headers={})
        classifier.wants_ctc_info.side_effect = anthropic.AuthenticationError(
            "invalid x-api-key", response=response, body=None)
        agent = CtcAgent(service, 0, classifier)
        agent._sleep = lambda s: self.fail("should not retry")
        with self.assertRaises(NeedsApiKey):
            agent.run()


class CredentialsTest(unittest.TestCase):
    def test_background_mode_never_opens_browser(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch("google_auth_oauthlib.flow.InstalledAppFlow") as flow:
            with self.assertRaises(NeedsAuth):
                load_credentials(Path(d) / "token.json", interactive=False)
            flow.assert_not_called()

class LegacyServiceTest(unittest.TestCase):
    def test_removes_old_background_service(self):
        import gui

        with tempfile.TemporaryDirectory() as d:
            plist = Path(d) / "com.ctc-agent.plist"
            with mock.patch.object(gui, "LEGACY_PLIST", plist), \
                    mock.patch.object(gui.subprocess, "run") as run:
                self.assertFalse(gui.remove_legacy_service())
                run.assert_not_called()
                plist.write_text("<plist/>")
                self.assertTrue(gui.remove_legacy_service())
                self.assertFalse(plist.exists())
                self.assertEqual(run.call_args.args[0][:2], ["launchctl", "bootout"])


if __name__ == "__main__":
    unittest.main()
