#!/usr/bin/env python3
"""Run with python3; no Docker daemon or real mailcow.conf is used.

The Docker mock uses SQLite fixtures to exercise the reset SQL and rollback
on disconnect. This does not replace integration testing against MariaDB.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import pty
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "mailcow-reset-admin.sh"


def password_hash(password):
    salt = b"test-only-salt"
    digest = hashlib.sha256(password.encode() + salt).digest()
    return "{SSHA256}" + base64.b64encode(digest + salt).decode()


def mock_docker(args):
    root = Path(os.environ["RESET_TEST_DIR"])
    mode = os.environ["RESET_TEST_MODE"]
    with (root / "calls.jsonl").open("a") as log:
        log.write(json.dumps(args) + "\n")
    if args[:2] == ["ps", "-qf"]:
        service = args[2].split("=", 1)[1]
        if mode == "missing-" + service:
            return 0
        print(service)
        # Even partial output from a failed docker ps must not be accepted.
        return 1 if mode == "discovery-failure-" + service else 0
    if args[:1] != ["exec"] or args[1].startswith("-"):
        print("Unexpected Docker invocation or interactive/TTY flag", file=sys.stderr)
        return 1
    if args[1:3] == ["dovecot-mailcow", "doveadm"]:
        if mode == "hash-empty":
            return 0
        print(password_hash(args[-1]))
        # A failing hash command can still produce output, followed by a
        # successful tr in the old pipeline. Its exit status must survive.
        return 1 if mode == "hash-failure" else 0
    if args[1:3] != ["mysql-mailcow", "mysql"]:
        return 1
    if args[3:7] != ["-ufixture user", "-pfixture pass *", "fixture db", "-e"]:
        print("Database arguments were not preserved", file=sys.stderr)
        return 1
    if mode == "database-unavailable":
        return 1
    connection = sqlite3.connect(root / "fixture.db", isolation_level=None)
    try:
        for index, statement in enumerate(filter(str.strip, args[7].split(";"))):
            if mode == "database-failure-" + str(index):
                connection.execute("SELECT * FROM nonexistent_table")
            statement = statement.strip()
            if statement == "START TRANSACTION":
                statement = "BEGIN"
            connection.execute(statement)
    except sqlite3.Error as error:
        print(error, file=sys.stderr)
        return 1
    finally:
        connection.close()
    return 0


class ResetAdminTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="mailcow-reset-admin-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.install = self.root / "mailcow"
        self.install.mkdir()
        (self.install / "helper-scripts").mkdir()
        self.script = self.install / "helper-scripts" / SCRIPT.name
        shutil.copyfile(SCRIPT, self.script)
        (self.install / "mailcow.conf").write_text(
            "DBUSER='fixture user'\nDBPASS='fixture pass *'\nDBNAME='fixture db'\n"
        )
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.bash = shutil.which("bash")
        self.write_mock(
            "docker",
            "exec " + shlex.join([sys.executable, str(Path(__file__).resolve()), "--mock-docker"])
            + ' "$@"\n',
        )

    def write_mock(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(0o755)

    def snapshot(self):
        with sqlite3.connect(self.root / "fixture.db") as connection:
            return {
                table: connection.execute("SELECT * FROM " + table + " ORDER BY username").fetchall()
                for table in ("admin", "domain_admins", "tfa")
            }

    def run_script(self, args=("-y",), mode="success", answer=None, helper_cwd=False, tty=False):
        database = self.root / "fixture.db"
        database.unlink(missing_ok=True)
        with sqlite3.connect(database) as connection:
            connection.executescript("""
                CREATE TABLE admin (username TEXT PRIMARY KEY, password TEXT,
                                    superadmin INTEGER, active INTEGER);
                CREATE TABLE domain_admins (username TEXT, domain TEXT);
                CREATE TABLE tfa (username TEXT, secret TEXT);
                INSERT INTO admin VALUES ('admin', 'old-password', 0, 0),
                                         ('other', 'other-password', 1, 1);
                INSERT INTO domain_admins VALUES ('admin', 'example.test'),
                                                 ('other', 'other.test');
                INSERT INTO tfa VALUES ('admin', 'old-secret'), ('other', 'other-secret');
            """)
        self.before = self.snapshot()
        (self.root / "calls.jsonl").write_text("")
        environment = os.environ.copy()
        # Do not inherit shell startup files or shell options from the caller.
        for name in ("BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"):
            environment.pop(name, None)
        environment.update(
            PATH=str(self.bin) + os.pathsep + environment["PATH"],
            RESET_TEST_DIR=str(self.root), RESET_TEST_MODE=mode,
        )
        input_options = {"stdin": subprocess.DEVNULL} if answer is None else {"input": answer}
        if tty:
            master, slave = pty.openpty()
            os.write(master, answer.encode())
            input_options = {"stdin": slave}
        try:
            result = subprocess.run(
                [self.bash, str(self.script), *args],
                cwd=self.script.parent if helper_cwd else self.install,
                env=environment, text=True, capture_output=True, timeout=10,
                **input_options,
            )
        finally:
            if tty:
                os.close(master)
                os.close(slave)
        self.calls = [json.loads(line) for line in (self.root / "calls.jsonl").read_text().splitlines()]
        return result

    def assert_failure(self, result, message):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(message, result.stdout + result.stderr)
        self.assertNotIn("Reset credentials:", result.stdout)
        self.assertNotIn("Password:", result.stdout)
        self.assertEqual(self.snapshot(), self.before)

    def assert_success(self, result, length=16):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        password = re.search(r"^Password: ([A-Za-z0-9_-]+)$", result.stdout, re.MULTILINE)
        self.assertIsNotNone(password, result.stdout)
        self.assertEqual(len(password[1]), length)
        self.assertEqual(self.snapshot(), {
            "admin": [("admin", password_hash(password[1]), 1, 1),
                      ("other", "other-password", 1, 1)],
            "domain_admins": [("other", "other.test")],
            "tfa": [("other", "other-secret")],
        })
        executions = [call for call in self.calls if call[0] == "exec"]
        self.assertEqual(len(executions), 2)
        self.assertEqual(executions[0][-1], password[1])

    def test_no_tty_yes_flags(self):
        for flag in ("-y", "--yes"):
            with self.subTest(flag=flag):
                self.assert_success(self.run_script(args=(flag,)))

    def test_custom_length_and_helper_directory(self):
        for flag in ("-y", "--yes"):
            with self.subTest(flag=flag):
                self.assert_success(self.run_script(args=(flag, "24"), helper_cwd=True), length=24)

    def test_confirmation_accepts_yes(self):
        for answer in ("y\n", "YES\n"):
            with self.subTest(answer=answer):
                self.assert_success(self.run_script(args=(), answer=answer))

    def test_confirmation_cancellation_and_eof(self):
        for answer in ("n\n", "\n", None):
            with self.subTest(answer=answer):
                result = self.run_script(args=(), answer=answer)
                self.assertEqual(result.returncode, 0)
                self.assertIn("Operation canceled.", result.stdout)
                self.assertNotIn("Reset credentials:", result.stdout)
                self.assertFalse(any(call[0] == "exec" for call in self.calls))
                self.assertEqual(self.snapshot(), self.before)

    def test_interactive_terminal_confirmation(self):
        result = self.run_script(args=(), answer="y\n", tty=True)
        self.assertIn("[y/N]", result.stderr)
        self.assert_success(result)
        result = self.run_script(args=(), answer="n\n", tty=True)
        self.assertEqual(result.returncode, 0)
        self.assertIn("[y/N]", result.stderr)
        self.assertIn("Operation canceled.", result.stdout)
        self.assertNotIn("Reset credentials:", result.stdout)
        self.assertEqual(self.snapshot(), self.before)

    def test_hash_failure_or_empty_output(self):
        for mode in ("hash-failure", "hash-empty"):
            with self.subTest(mode=mode):
                self.assert_failure(self.run_script(mode=mode), "Failed to generate password hash")
                self.assertFalse(any(call[:3] == ["exec", "mysql-mailcow", "mysql"] for call in self.calls))

    def test_unavailable_containers_and_failed_discovery(self):
        for service in ("mysql-mailcow", "dovecot-mailcow"):
            for prefix in ("missing-", "discovery-failure-"):
                with self.subTest(service=service, prefix=prefix):
                    self.assert_failure(self.run_script(mode=prefix + service), "not up and running")
                    self.assertFalse(any(call[0] == "exec" for call in self.calls))

    def test_database_failure_and_rollback_at_every_statement(self):
        # START, both DELETEs, INSERT, TFA DELETE, and COMMIT.
        for mode in ["database-unavailable"] + ["database-failure-" + str(i) for i in range(6)]:
            with self.subTest(mode=mode):
                self.assert_failure(self.run_script(mode=mode), "Failed to reset administrator account")

    def test_invalid_password_length(self):
        for length in ("0", "-1", "invalid"):
            with self.subTest(length=length):
                self.assert_failure(self.run_script(args=("-y", length)), "Password length")
                self.assertFalse(any(call[0] == "exec" for call in self.calls))

    def test_failed_or_short_random_generation(self):
        for body in ("exit 1\n", "printf short\nexit 1\n", "printf '0123456789abcdef'\nexit 1\n"):
            with self.subTest(body=body):
                self.write_mock("tr", body)
                self.assert_failure(self.run_script(), "Failed to generate a random password")
                self.assertFalse(any(call[0] == "exec" for call in self.calls))

    def test_failed_head_with_full_output(self):
        self.write_mock("head", "printf '0123456789abcdef'\nexit 1\n")
        self.assert_failure(self.run_script(), "Failed to generate a random password")
        self.assertFalse(any(call[0] == "exec" for call in self.calls))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--mock-docker"]:
        sys.exit(mock_docker(sys.argv[2:]))
    unittest.main(verbosity=2)
