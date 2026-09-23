import base64
import email
import logging
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock

from google.auth.exceptions import RefreshError

logging.disable(logging.CRITICAL)

from ctc_agent import (ACK_BODY, CtcAgent, NeedsAuth, build_reply, is_ctc_subject,
                       load_credentials)


def fake_service(messages, labels=()):
    """A MagicMock Gmail service whose search returns `messages` (id -> headers dict)."""
    service = MagicMock()
    msgs = service.users.return_value.messages.return_value
    msgs.list.return_value.execute.return_value = {"messages": [{"id": i} for i in messages]}
    msgs.get.side_effect = lambda userId, id, **_: MagicMock(execute=lambda: {
        "id": id, "threadId": f"t-{id}",
        "payload": {"headers": [{"name": k, "value": v} for k, v in messages[id].items()]},
    })
    lbls = service.users.return_value.labels.return_value
    lbls.list.return_value.execute.return_value = {"labels": list(labels)}
    lbls.create.return_value.execute.return_value = {"id": "Label_new"}
    return service, msgs


def sent_emails(msgs):
    out = []
    for call in msgs.send.call_args_list:
        body = call.kwargs["body"]
        out.append((email.message_from_bytes(base64.urlsafe_b64decode(body["raw"])), body["threadId"]))
    return out


class SubjectTest(unittest.TestCase):
    def test_prefix(self):
        self.assertTrue(is_ctc_subject("[CTC] quarterly report"))
        self.assertTrue(is_ctc_subject("  [CTC]x"))
        self.assertFalse(is_ctc_subject("Re: [CTC] quarterly report"))
        self.assertFalse(is_ctc_subject("CTC update"))
        self.assertFalse(is_ctc_subject("[ctc] lowercase"))
        self.assertFalse(is_ctc_subject(""))


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
        self.assertEqual(msg.get_payload().strip(), ACK_BODY)

    def test_prefers_reply_to(self):
        body = build_reply({"subject": "[CTC] hi", "from": "a@x.com", "reply-to": "list@x.com"}, "t")
        msg = email.message_from_bytes(base64.urlsafe_b64decode(body["raw"]))
        self.assertEqual(msg["To"], "list@x.com")
        self.assertIsNone(msg["In-Reply-To"])


class AgentTest(unittest.TestCase):
    def test_acks_only_ctc_messages_and_labels_them(self):
        service, msgs = fake_service({
            "a": {"Subject": "[CTC] one", "From": "a@x.com", "Message-ID": "<a@x>"},
            "b": {"Subject": "About CTC", "From": "b@x.com"},
            "c": {"Subject": "Re: [CTC] one", "From": "c@x.com"},
        })
        agent = CtcAgent(service, since_epoch=100)
        self.assertEqual(agent.poll_once(), 1)

        query = msgs.list.call_args.kwargs["q"]
        self.assertIn("-label:ctc-acked", query)
        self.assertIn("after:100", query)
        [(sent, thread)] = sent_emails(msgs)
        self.assertEqual(thread, "t-a")
        self.assertEqual(sent["To"], "a@x.com")
        msgs.modify.assert_called_once_with(userId="me", id="a", body={"addLabelIds": ["Label_new"]})

    def test_reuses_existing_label_and_never_double_acks(self):
        service, msgs = fake_service(
            {"a": {"Subject": "[CTC] one", "From": "a@x.com"}},
            labels=[{"id": "Label_7", "name": "ctc-acked"}],
        )
        agent = CtcAgent(service, since_epoch=0)
        agent.poll_once()
        agent.poll_once()  # search still returns "a" (e.g. label not yet indexed)
        self.assertEqual(msgs.send.call_count, 1)
        service.users.return_value.labels.return_value.create.assert_not_called()
        msgs.modify.assert_called_once_with(userId="me", id="a", body={"addLabelIds": ["Label_7"]})

    def test_run_backs_off_on_errors_and_stops(self):
        service, msgs = fake_service({})
        msgs.list.return_value.execute.side_effect = RuntimeError("network down")
        agent = CtcAgent(service, since_epoch=0, poll_interval=5)
        delays = []

        def fake_sleep(seconds):
            delays.append(seconds)
            if len(delays) == 3:
                agent.stop()

        agent._sleep = fake_sleep
        agent.run()
        self.assertEqual(delays, [10, 20, 40])

    def test_reports_actions_to_listener(self):
        service, msgs = fake_service({"a": {"Subject": "[CTC] one", "From": "Ann <a@x.com>"}})
        events = []
        agent = CtcAgent(service, since_epoch=0, poll_interval=5,
                         listener=lambda event, **d: events.append((event, d)))

        def fake_sleep(seconds):
            if len(events) == 2:
                msgs.list.return_value.execute.side_effect = RuntimeError("offline")
            else:
                agent.stop()

        agent._sleep = fake_sleep
        agent.run()
        self.assertEqual(events, [
            ("acked", {"subject": "[CTC] one", "sender": "Ann <a@x.com>"}),
            ("checked", {"acked": 1}),
            ("error", {"error": "offline", "retry_in": 10}),
        ])

    def test_stop_skips_remaining_messages(self):
        service, msgs = fake_service({
            "a": {"Subject": "[CTC] one", "From": "a@x.com"},
            "b": {"Subject": "[CTC] two", "From": "b@x.com"},
        })
        agent = CtcAgent(service, since_epoch=0, listener=lambda event, **d: agent.stop())
        self.assertEqual(agent.poll_once(), 1)

    def test_revoked_sign_in_stops_instead_of_retrying(self):
        service, msgs = fake_service({})
        msgs.list.return_value.execute.side_effect = RefreshError("invalid_grant")
        agent = CtcAgent(service, since_epoch=0)
        agent._sleep = lambda s: self.fail("should not retry")
        with self.assertRaises(NeedsAuth):
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
