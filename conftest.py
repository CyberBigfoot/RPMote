"""Test-wide safety net: nothing here may write into the real user's profile.

Every SSH session records itself, which is exactly what we want in the app and
exactly what we do not want from a test suite that opens hundreds of them.
Redirecting the recordings directory once, for the whole run, is more reliable
than remembering to disable recording in each test that happens to feed the
terminal a byte.
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# The suite runs on Qt's offscreen platform, which has no OpenGL.  Pin the
# dashboard to its software base class before anything imports it, whatever
# the developer's own saved preference is.
os.environ.setdefault("RPMOTE_HW_ACCEL", "0")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# Test modules share one application. Create it before collection, with a
# program name and shared GL contexts: otherwise an earlier terminal test's
# QApplication([]) leaves WebEngine unable to start when browser tests run.
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
_TEST_APP = QApplication.instance() or QApplication([str(Path(__file__).resolve())])

from app import session_log, windows_credentials


@pytest.fixture(scope="session", autouse=True)
def _sessions_in_a_temp_directory():
    previous = session_log.SESSIONS_DIR
    temporary = Path(tempfile.mkdtemp(prefix="rpmote-test-sessions-"))
    session_log.use_sessions_directory(temporary)
    try:
        yield temporary
    finally:
        session_log.use_sessions_directory(previous)
        shutil.rmtree(temporary, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _credentials_stay_in_memory():
    """Opening an RDP connection stores its login in Windows Credential
    Manager for mstsc.  A test that opens one must not leave a made-up
    account behind on the developer's machine."""
    previous = windows_credentials.use_credential_store(
        windows_credentials.MemoryCredentialStore())
    try:
        yield
    finally:
        windows_credentials.use_credential_store(previous)
