#!/usr/bin/env python3
"""Unit tests for the render's file writers.

The compose file is what the deploy step consumes, so a render that fails
halfway must leave the previous file exactly as it was. These tests pin the
atomic write that guarantees it.
"""

import os
import shutil
import tempfile
import unittest

from autobuild.utils.autoconfig import write_text_file
from autobuild.app import write_rendered_stack


class AtomicWriteTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, 'docker-compose.yml')

    def test_writes_the_content(self):
        write_text_file(self.path, 'services:\n')
        with open(self.path) as handle:
            self.assertEqual('services:\n', handle.read())

    def test_replaces_an_existing_file(self):
        write_text_file(self.path, 'old\n')
        write_text_file(self.path, 'new\n')
        with open(self.path) as handle:
            self.assertEqual('new\n', handle.read())

    def test_leaves_no_temporary_file_behind(self):
        write_text_file(self.path, 'services:\n')
        self.assertEqual(['docker-compose.yml'], sorted(os.listdir(self.dir)))

    def test_a_failed_write_keeps_the_previous_file(self):
        write_text_file(self.path, 'good\n')
        # a value the writer cannot serialise: the failure must happen before
        # the target is touched
        with self.assertRaises(TypeError):
            write_text_file(self.path, object())
        with open(self.path) as handle:
            self.assertEqual('good\n', handle.read())


class DeferredStackWriteTestCase(unittest.TestCase):
    """The compose file is written only after the whole render succeeds."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, 'docker-compose.yml')

    def test_writes_every_rendered_file(self):
        write_rendered_stack([{'rendered_files': [(self.path, 'services:\n')]}])
        with open(self.path) as handle:
            self.assertEqual('services:\n', handle.read())

    def test_a_stage_that_never_ran_leaves_the_file_alone(self):
        write_text_file(self.path, 'previous\n')
        # a render that failed before producing any file carries no entry
        write_rendered_stack([{}])
        with open(self.path) as handle:
            self.assertEqual('previous\n', handle.read())


if __name__ == '__main__':
    unittest.main()
