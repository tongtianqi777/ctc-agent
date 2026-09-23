"""The CTC Agent window: sign-in status plus a live log of what the agent does.

The agent runs on a worker thread and reports back through CtcAgent's listener;
every UI update is marshalled to the main thread with AppHelper.callAfter.
Closing the window quits the app, which stops the agent.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

import objc
from AppKit import (
    NSAlert, NSAlertFirstButtonReturn, NSApp, NSApplication, NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered, NSBezelBorder, NSButton, NSColor, NSFont, NSLineBreakByTruncatingTail, NSMakeRect, NSMenu,
    NSMenuItem, NSScrollView, NSSecureTextField, NSTableColumn, NSTableView,
    NSTableViewLastColumnOnlyAutoresizingStyle, NSTextField, NSViewHeightSizable,
    NSViewMinXMargin, NSViewMinYMargin, NSViewWidthSizable, NSWindow,
    NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable, NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSActivityUserInitiatedAllowingIdleSystemSleep, NSObject, NSProcessInfo
from google.auth.exceptions import RefreshError
from PyObjCTools import AppHelper

from ctc_agent import CtcAgent, NeedsApiKey, NeedsAuth, gmail_service, load_credentials, \
    load_since_epoch, make_classifier, save_api_key

log = logging.getLogger("ctc_agent.gui")

MAX_ROWS = 500
# Version 0.1.0 installed itself as a login-time background service; the app now only
# runs while its window is open, so that service is removed on launch.
LEGACY_SERVICE = "com.ctc-agent"
LEGACY_PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_SERVICE}.plist"
COLUMNS = (("time", "Time", 70), ("event", "Event", 120), ("details", "Details", 420))


def _label(frame, size=13, bold=False):
    field = NSTextField.labelWithString_("")
    field.setFrame_(frame)
    field.setFont_(NSFont.boldSystemFontOfSize_(size) if bold else NSFont.systemFontOfSize_(size))
    field.setLineBreakMode_(NSLineBreakByTruncatingTail)
    return field


def remove_legacy_service() -> bool:
    if not LEGACY_PLIST.exists():
        return False
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LEGACY_SERVICE}"],
                   capture_output=True)
    LEGACY_PLIST.unlink(missing_ok=True)
    log.info("Removed legacy background service %s", LEGACY_PLIST)
    return True


class AppDelegate(NSObject):
    def initWithArgs_(self, args):
        self = objc.super(AppDelegate, self).init()
        if self is None:
            return None
        self.args = args
        self.rows = []
        self.acked_count = 0
        self.agent: Optional[CtcAgent] = None
        self.agent_thread: Optional[threading.Thread] = None
        # Bumped on every sign-in/sign-out so events from a superseded worker are ignored.
        self.generation = 0
        self.state = "signed-out"
        self.email = ""
        return self

    # ----- App lifecycle -----

    def applicationDidFinishLaunching_(self, _):
        self._build_menu()
        self._build_window()
        # Without this, App Nap throttles the polling thread whenever the window is hidden.
        self.activity = NSProcessInfo.processInfo().beginActivityWithOptions_reason_(
            NSActivityUserInitiatedAllowingIdleSystemSleep, "Watching Gmail for emails about CTC")
        if remove_legacy_service():
            self._add_row("Updated", "Removed the background service from the previous version")
        self._set_state("connecting")
        self._start(interactive=False)

    def applicationShouldTerminateAfterLastWindowClosed_(self, _):
        return True

    def applicationWillTerminate_(self, _):
        self._stop_agent()
        if self.agent_thread:
            # Let an in-flight reply finish labeling so it isn't sent again next launch.
            self.agent_thread.join(timeout=10)
        log.info("App closed; agent stopped")

    # ----- UI construction -----

    @objc.python_method
    def _build_menu(self):
        menubar = NSMenu.alloc().init()
        app_item = NSMenuItem.alloc().init()
        menubar.addItem_(app_item)
        app_menu = NSMenu.alloc().init()
        key_item = app_menu.addItemWithTitle_action_keyEquivalent_(
            "Set Claude API Key…", "setApiKey:", "")
        key_item.setTarget_(self)
        app_menu.addItem_(NSMenuItem.separatorItem())
        app_menu.addItemWithTitle_action_keyEquivalent_("Quit CTC Agent", "terminate:", "q")
        app_item.setSubmenu_(app_menu)
        NSApp.setMainMenu_(menubar)

    @objc.python_method
    def _build_window(self):
        width, height = 680, 440
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskMiniaturizable | NSWindowStyleMaskResizable)
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, width, height), style, NSBackingStoreBuffered, False)
        self.window.setTitle_("CTC Agent")
        self.window.setMinSize_((520, 300))
        self.window.setReleasedWhenClosed_(False)
        content = self.window.contentView()
        top = NSViewMinYMargin  # keep the header pinned to the top when resizing

        self.dot = _label(NSMakeRect(20, height - 44, 20, 24), size=16)
        self.dot.setAutoresizingMask_(top)
        self.status = _label(NSMakeRect(42, height - 44, width - 200, 24), size=15, bold=True)
        self.status.setAutoresizingMask_(top | NSViewWidthSizable)
        self.detail = _label(NSMakeRect(42, height - 66, width - 200, 18), size=12)
        self.detail.setTextColor_(NSColor.secondaryLabelColor())
        self.detail.setAutoresizingMask_(top | NSViewWidthSizable)
        self.button = NSButton.buttonWithTitle_target_action_("Sign In", self, "buttonClicked:")
        self.button.setFrame_(NSMakeRect(width - 140, height - 56, 120, 32))
        self.button.setAutoresizingMask_(top | NSViewMinXMargin)
        for view in (self.dot, self.status, self.detail, self.button):
            content.addSubview_(view)

        self.table = NSTableView.alloc().initWithFrame_(NSMakeRect(0, 0, width - 40, height - 100))
        for ident, title, col_width in COLUMNS:
            column = NSTableColumn.alloc().initWithIdentifier_(ident)
            column.setTitle_(title)
            column.setWidth_(col_width)
            self.table.addTableColumn_(column)
        self.table.setColumnAutoresizingStyle_(NSTableViewLastColumnOnlyAutoresizingStyle)
        self.table.setUsesAlternatingRowBackgroundColors_(True)
        self.table.setDataSource_(self)
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 20, width - 40, height - 100))
        scroll.setDocumentView_(self.table)
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(NSBezelBorder)
        scroll.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        content.addSubview_(scroll)

        self.window.center()
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    # ----- Table data source -----

    def numberOfRowsInTableView_(self, _):
        return len(self.rows)

    def tableView_objectValueForTableColumn_row_(self, _, column, row):
        return self.rows[row][column.identifier()]

    @objc.python_method
    def _add_row(self, event, details=""):
        self.rows.insert(0, {"time": time.strftime("%H:%M:%S"), "event": event, "details": details})
        del self.rows[MAX_ROWS:]
        self.table.reloadData()

    # ----- State -----

    @objc.python_method
    def _set_state(self, state, detail="", email=""):
        self.state = state
        status, color, button, enabled = {
            "signed-out": ("Not signed in", NSColor.systemGrayColor(), "Sign In", True),
            "signing-in": ("Waiting for you to sign in in your browser…",
                           NSColor.systemOrangeColor(), "Cancel", True),
            "connecting": ("Connecting to Gmail…", NSColor.systemOrangeColor(), "Sign Out", False),
            "watching": (f"Watching {email}", NSColor.systemGreenColor(), "Sign Out", True),
            "retrying": ("Connection problem", NSColor.systemOrangeColor(), "Sign Out", True),
            "needs-key": ("Claude API key needed", NSColor.systemOrangeColor(),
                          "Set API Key…", True),
        }[state]
        self.dot.setStringValue_("●")
        self.dot.setTextColor_(color)
        self.status.setStringValue_(status)
        self.detail.setStringValue_(detail)
        self.button.setTitle_(button)
        self.button.setEnabled_(enabled)

    def setApiKey_(self, _):
        alert = NSAlert.alloc().init()
        alert.setMessageText_("Claude API Key")
        alert.setInformativeText_("CTC Agent uses Claude to decide which emails are asking "
                                  "about CTC. Paste an API key from console.anthropic.com.")
        alert.addButtonWithTitle_("Save")
        alert.addButtonWithTitle_("Cancel")
        field = NSSecureTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 300, 24))
        alert.setAccessoryView_(field)
        alert.window().setInitialFirstResponder_(field)
        if alert.runModal() != NSAlertFirstButtonReturn:
            return
        key = field.stringValue().strip()
        if not key:
            return
        save_api_key(key)
        self._add_row("API key", "Saved the Claude API key")
        if self.state not in ("signed-out", "signing-in"):
            self._stop_agent()
            self._set_state("connecting")
            self._start(interactive=False)

    def buttonClicked_(self, _):
        if self.state == "needs-key":
            self.setApiKey_(None)
        elif self.state == "signed-out":
            self._set_state("signing-in", "Sign in with the Gmail account CTC Agent should watch.")
            self._add_row("Sign-in", "Opened Google sign-in in your browser")
            self._start(interactive=True)
        elif self.state == "signing-in":
            self.generation += 1  # the abandoned browser flow times out on its own
            self._set_state("signed-out", "Sign-in cancelled.")
        else:
            self._stop_agent()
            self.args.token.unlink(missing_ok=True)
            self._add_row("Signed out")
            self._set_state("signed-out", "Sign in with the Gmail account to watch.")

    # ----- Agent worker -----

    @objc.python_method
    def _start(self, interactive):
        self.generation += 1
        gen = self.generation
        threading.Thread(target=self._work, args=(gen, interactive), daemon=True).start()

    @objc.python_method
    def _stop_agent(self):
        self.generation += 1
        if self.agent:
            self.agent.stop()
            self.agent = None

    @objc.python_method
    def _post(self, gen, fn, *args):
        """Run fn(*args) on the main thread unless this worker has been superseded."""
        def call():
            if gen == self.generation:
                fn(*args)
        AppHelper.callAfter(call)

    @objc.python_method
    def _work(self, gen, interactive):
        a = self.args
        try:
            classifier = make_classifier()
            creds = load_credentials(a.token, a.credentials, interactive=interactive)
            service = gmail_service(creds)
            email = service.users().getProfile(userId="me").execute()["emailAddress"]
            previous = self.agent_thread
            if previous and previous is not threading.current_thread():
                previous.join()  # a stopped agent may still be finishing a poll
            if gen != self.generation:
                return
            agent = CtcAgent(service, load_since_epoch(a.state), classifier, a.interval,
                             listener=lambda event, **d: self._post(gen, self._on_event, event, d))
            self.agent, self.agent_thread = agent, threading.current_thread()
            self._post(gen, self._on_started, email)
            agent.run()
        except NeedsApiKey as e:
            self._post(gen, self._on_needs_api_key, str(e))
        except (NeedsAuth, RefreshError) as e:
            self._post(gen, self._on_needs_auth, str(e), interactive)
        except Exception as e:
            log.exception("Agent failed")
            self._post(gen, self._on_failed, str(e))

    # ----- Worker callbacks (main thread) -----

    @objc.python_method
    def _on_started(self, email):
        self.email = email
        self._add_row("Started", f"Watching {email} for emails asking about CTC")
        self._set_state("watching", "Checking for new emails…", email=email)

    @objc.python_method
    def _on_event(self, event, d):
        if event == "acked":
            self.acked_count += 1
            name, addr = parseaddr(d["sender"])
            self._add_row("Replied", f"{d['subject']}  —  to {name or addr}")
        elif event == "ignored":
            name, addr = parseaddr(d["sender"])
            self._add_row("No reply", f"{d['subject']}  —  from {name or addr} ({d['reason']})")
        elif event == "checked":
            self._set_state("watching", f"Last checked {time.strftime('%H:%M:%S')}  ·  "
                            f"{self.acked_count} replied since opening", email=self.email)
        elif event == "error":
            retry = int(d["retry_in"])
            self._add_row("Error", d["error"][:300])
            self._set_state("retrying", f"Retrying in {retry}s: {d['error'][:150]}")

    @objc.python_method
    def _on_needs_auth(self, message, interactive):
        self.agent = None
        if self.state == "watching" or self.state == "retrying":
            self._add_row("Signed out", "Gmail sign-in expired or was revoked. Please sign in again.")
        elif interactive:
            self._add_row("Sign-in failed", message)
        self._set_state("signed-out", "Sign in with the Gmail account to watch.")

    @objc.python_method
    def _on_needs_api_key(self, message):
        self.agent = None
        self._add_row("API key", message)
        self._set_state("needs-key", "Add a Claude API key so the agent can read new emails.")

    @objc.python_method
    def _on_failed(self, message):
        self.agent = None
        self._add_row("Error", message[:300])
        self._set_state("signed-out", f"Could not start: {message[:150]}")


def main(args):
    app = NSApplication.sharedApplication()
    delegate = AppDelegate.alloc().initWithArgs_(args)  # app.delegate is weak; keep a reference
    app.setDelegate_(delegate)
    app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
    AppHelper.runEventLoop()
