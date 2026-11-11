#!/usr/bin/env python3
"""Unit tests for the input validation the render performs up front.

A malformed input must be refused before any file is written: the compose
file a previous render produced is what the deploy step consumes.
"""

import unittest

from autobuild.app import processcustom


class DuplicateDaemonTestCase(unittest.TestCase):
    def _run(self, names):
        customlist = [{
            'daemons': [{'name': name, 'type': 'chain'} for name in names],
        }]
        return processcustom(customlist, '172.31.0.0/16', '/nonexistent/')

    def test_a_repeated_coin_is_refused(self):
        with self.assertRaises(RuntimeError) as caught:
            self._run(['RVN', 'DGB', 'RVN'])
        self.assertIn('RVN', str(caught.exception))
        self.assertIn('twice', str(caught.exception))

    def test_the_same_coin_in_different_case_is_still_a_duplicate(self):
        with self.assertRaises(RuntimeError):
            self._run(['RVN', 'rvn'])


class DaemonNameCaseTestCase(unittest.TestCase):
    """A daemon name reaches every lookup as its upper-case ticker."""

    def test_a_lower_case_name_is_normalised_before_the_manifest_lookup(self):
        customlist = [{'daemons': [{'name': 'rvn', 'type': 'chain'}]}]
        # the manifest source does not exist, so the render stops there; the
        # point is that the name was normalised before any lookup saw it
        with self.assertRaises(Exception):
            processcustom(customlist, '172.31.0.0/16', '/nonexistent/')
        self.assertEqual('RVN', customlist[0]['daemons'][0]['name'])


if __name__ == '__main__':
    unittest.main()
