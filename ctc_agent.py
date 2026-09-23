"""Long-running Gmail watcher that acknowledges emails whose subject starts with "[CTC]".

For every new inbox message with a "[CTC]" subject prefix, the agent sends a
threaded reply with the body "ctc email ack" and applies a Gmail label so the
message is never acknowledged twice (even across restarts).
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import signal
import sys
import time
import warnings
from email.message import EmailMessage
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

# Deprecation notices about the build's Python/SSL versions only clutter the user's log.
warnings.filterwarnings("ignore", message=".*(past its end of life|non-supported Python version)")
warnings.filterwarnings("ignore", message="urllib3 v2 only supports OpenSSL")

from google.auth.exceptions import RefreshError  # noqa: E402


__version__ = "0.2.0"

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]  # read, label and send
SUBJECT_PREFIX = "[CTC]"
ACK_BODY = "ctc email ack"
ACK_LABEL = "ctc-acked"
MAX_BACKOFF_SECONDS = 600
DATA_DIR = Path(os.environ.get("CTC_AGENT_HOME")
                or Path.home() / "Library" / "Application Support" / "ctc-agent")
LOG_PATH = Path.home() / "Library" / "Logs" / "ctc-agent.log"

log = logging.getLogger("ctc_agent")


def is_ctc_subject(subject: str) -> bool:
    return subject.lstrip().startswith(SUBJECT_PREFIX)


def build_reply(headers: Dict[str, str], thread_id: str) -> dict:
    """Build a Gmail API send body that replies in-thread to a message with the given headers."""
    subject = headers.get("subject", "")
    reply = EmailMessage()
    reply["To"] = headers.get("reply-to") or headers["from"]
    reply["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    message_id = headers.get("message-id")
    if message_id:
        reply["In-Reply-To"] = message_id
        reply["References"] = f"{headers.get('references', '')} {message_id}".strip()
    reply.set_content(ACK_BODY)
    raw = base64.urlsafe_b64encode(reply.as_bytes()).decode()
    return {"raw": raw, "threadId": thread_id}


class CtcAgent:
    def __init__(self, service, since_epoch: int, poll_interval: float = 30,
                 listener: Optional[Callable[..., None]] = None):
        self.service = service
        # Called from the agent's thread as listener(event, **details); see run() and handle().
        self._emit = listener or (lambda event, **details: None)
        self.since_epoch = since_epoch
        self.poll_interval = poll_interval
        self._label_id: Optional[str] = None
        # Message ids already handled by this process: non-CTC matches from the
        # broad Gmail search, and acks whose labeling failed after sending.
        self._seen: set = set()
        self._stopping = False

    @property
    def messages(self):
        return self.service.users().messages()

    def label_id(self) -> str:
        if self._label_id is None:
            labels = self.service.users().labels().list(userId="me").execute().get("labels", [])
            for label in labels:
                if label["name"].lower() == ACK_LABEL:
                    self._label_id = label["id"]
                    break
            else:
                created = self.service.users().labels().create(
                    userId="me",
                    body={"name": ACK_LABEL, "labelListVisibility": "labelShow",
                          "messageListVisibility": "show"},
                ).execute()
                self._label_id = created["id"]
        return self._label_id

    def candidate_ids(self) -> Iterator[str]:
        # Gmail search ignores brackets, so match broadly and filter the exact prefix locally.
        query = f"in:inbox subject:CTC -label:{ACK_LABEL} after:{self.since_epoch}"
        page_token = None
        while True:
            resp = self.messages.list(userId="me", q=query, pageToken=page_token).execute()
            for m in resp.get("messages", []):
                yield m["id"]
            page_token = resp.get("nextPageToken")
            if not page_token:
                return

    def handle(self, msg_id: str) -> bool:
        """Acknowledge one message if it qualifies. Returns True if a reply was sent."""
        if msg_id in self._seen:
            return False
        msg = self.messages.get(
            userId="me", id=msg_id, format="metadata",
            metadataHeaders=["Subject", "From", "Reply-To", "Message-ID", "References"],
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        subject = headers.get("subject", "")
        if not is_ctc_subject(subject) or "from" not in headers:
            self._seen.add(msg_id)
            return False

        self.messages.send(userId="me", body=build_reply(headers, msg["threadId"])).execute()
        self._seen.add(msg_id)
        log.info("Acked %r from %s", subject, headers["from"])
        self._emit("acked", subject=subject, sender=headers["from"])
        self.messages.modify(
            userId="me", id=msg_id, body={"addLabelIds": [self.label_id()]}
        ).execute()
        return True

    def poll_once(self) -> int:
        acked = 0
        for msg_id in self.candidate_ids():
            if self._stopping:
                break
            acked += self.handle(msg_id)
        return acked

    def stop(self, *_):
        log.info("Shutting down")
        self._stopping = True

    def run(self):
        log.info("Watching inbox for %r emails (every %ss)", SUBJECT_PREFIX, self.poll_interval)
        failures = 0
        while not self._stopping:
            try:
                acked = self.poll_once()
                failures = 0
                delay = self.poll_interval
                self._emit("checked", acked=acked)
            except RefreshError as e:
                raise NeedsAuth(f"Gmail sign-in is no longer valid: {e}") from e
            except Exception as e:
                failures += 1
                delay = min(self.poll_interval * 2 ** failures, MAX_BACKOFF_SECONDS)
                log.exception("Poll failed (%d in a row); retrying in %ss", failures, delay)
                self._emit("error", error=str(e), retry_in=delay)
            self._sleep(delay)

    def _sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while not self._stopping and time.monotonic() < end:
            time.sleep(min(1, end - time.monotonic()))


class NeedsAuth(Exception):
    """The user must sign in to Gmail (again) before the agent can run."""


def find_client_secrets(explicit: Optional[Path]) -> Path:
    """The OAuth client: --credentials, else the data dir, else the copy bundled in the app."""
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for path in (explicit, DATA_DIR / "credentials.json", bundle_dir / "credentials.json"):
        if path and path.exists():
            return path
    raise NeedsAuth(
        f"No OAuth client found. Put credentials.json in {DATA_DIR} (see README for setup)."
    )


def load_credentials(token_path: Path, credentials_path: Optional[Path] = None,
                     interactive: bool = False):
    """Load the saved Gmail token, refreshing it if needed.

    Only when `interactive` is set does this open a browser for the consent flow;
    otherwise it raises NeedsAuth, so the background service never pops up a browser.
    """
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            return _save_token(creds, token_path)
        except RefreshError:
            log.warning("Saved Gmail token was rejected; a new sign-in is required")
    if not interactive:
        raise NeedsAuth("Not signed in to Gmail.")
    flow = InstalledAppFlow.from_client_secrets_file(str(find_client_secrets(credentials_path)), SCOPES)
    creds = flow.run_local_server(
        port=0, timeout_seconds=300,
        success_message="CTC Agent is signed in to Gmail. You can close this tab.",
    )
    return _save_token(creds, token_path)


def _save_token(creds, token_path: Path):
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
    return creds


def gmail_service(creds):
    from googleapiclient.discovery import build

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def load_since_epoch(state_path: Path) -> int:
    """Only emails received after the agent's first start are acknowledged."""
    if state_path.exists():
        return json.loads(state_path.read_text())["since_epoch"]
    since = int(time.time())
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"since_epoch": since}))
    return since


def cmd_run(args) -> int:
    try:
        creds = load_credentials(args.token, args.credentials, interactive=sys.stdin.isatty())
        agent = CtcAgent(gmail_service(creds), load_since_epoch(args.state), args.interval)
        signal.signal(signal.SIGTERM, agent.stop)
        signal.signal(signal.SIGINT, agent.stop)
        agent.run()
    except NeedsAuth as e:
        log.error("%s Run `ctc-agent auth` to sign in.", e)
        return 1
    return 0


def cmd_auth(args) -> int:
    creds = load_credentials(args.token, args.credentials, interactive=True)
    profile = gmail_service(creds).users().getProfile(userId="me").execute()
    print(f"Signed in as {profile['emailAddress']}")
    return 0


def cmd_app(args) -> int:
    import gui

    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.getLogger().addHandler(logging.FileHandler(LOG_PATH))
    gui.main(args)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = [a for a in (sys.argv[1:] if argv is None else argv) if not a.startswith("-psn_")]
    parser = argparse.ArgumentParser(prog="ctc-agent", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--credentials", type=Path,
                        help="OAuth client secrets file (defaults to the data dir or the bundled copy)")
    parser.add_argument("--token", type=Path, default=DATA_DIR / "token.json",
                        help="Where the Gmail sign-in token is saved")
    parser.add_argument("--state", type=Path, default=DATA_DIR / "state.json",
                        help="Stores the start time; emails before it are ignored")
    parser.add_argument("--interval", type=float, default=30, help="Poll interval in seconds")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("app", help="Open the app window (the default for the packaged app)")
    sub.add_parser("run", help="Watch the inbox in the terminal, without a window")
    sub.add_parser("auth", help="Sign in to Gmail and save the token")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Double-clicking the packaged app passes no arguments.
    command = args.command or ("app" if getattr(sys, "frozen", False) else "run")
    handler = {"app": cmd_app, "run": cmd_run, "auth": cmd_auth}[command]
    try:
        return handler(args)
    except NeedsAuth as e:
        print(e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
