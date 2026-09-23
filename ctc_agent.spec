# PyInstaller spec for "CTC Agent.app". Build with ./build.sh rather than directly.
import os
import re

import googleapiclient

VERSION = re.search(r'__version__ = "(.+)"', open("ctc_agent.py").read()).group(1)

# Only the Gmail discovery document is needed, not the ~500 bundled with the client library.
gmail_discovery = os.path.join(os.path.dirname(googleapiclient.__file__),
                               "discovery_cache", "documents", "gmail.v1.json")
datas = [(gmail_discovery, "googleapiclient/discovery_cache/documents")]
if os.path.exists("credentials.json"):
    datas.append(("credentials.json", "."))

a = Analysis(["ctc_agent.py"], datas=datas, excludes=["tkinter"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="ctc-agent", console=False)
coll = COLLECT(exe, a.binaries, a.datas, name="ctc-agent")
app = BUNDLE(
    coll,
    name="CTC Agent.app",
    bundle_identifier="com.ctc-agent",
    version=VERSION,
    info_plist={
        "CFBundleShortVersionString": VERSION,
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
