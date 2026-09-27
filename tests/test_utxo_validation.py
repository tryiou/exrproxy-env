#!/usr/bin/env python3
"""Unit tests for utxo-plugin chain validation helpers.

Covers `autobuild.utils.utxo.coin_map_keys_from_source` (AST extraction of
the coin_map registry without executing the file) and
`autobuild.utils.utxo.missing_chain_support` (render-vs-image comparison).

Stdlib only; host-runnable: `python3 -m unittest discover -s tests`
from the repo root.
"""
import unittest
from unittest.mock import patch

from autobuild.utils.utxo import (
    coin_map_keys_from_source,
    missing_chain_support,
    validate_utxo_chains_against_image,
    validate_utxo_chains_against_source,
)
from autobuild.utils import read_utxo_plugin_source

MAIN_PY_FIXTURE = '''
from electrumx.lib.coins import Verge as UpstreamVerge

coin_map = {
    "BLOCK": Blocknet,
    "TBLOCK": BlocknetTestnet,
    "BTC": BitcoinSegwit,
    "PIVX": Pivx,
    "XVG": Verge,
}
'''


class TestCoinMapKeysFromSource(unittest.TestCase):
    def test_extracts_all_keys_in_order(self):
        self.assertEqual(
            ["BLOCK", "TBLOCK", "BTC", "PIVX", "XVG"],
            coin_map_keys_from_source(MAIN_PY_FIXTURE))

    def test_missing_coin_map_returns_empty(self):
        self.assertEqual([], coin_map_keys_from_source("x = 1\n"))

    def test_non_string_keys_ignored(self):
        source = 'coin_map = {"BLOCK": Blocknet, 7: Seven}\n'
        self.assertEqual(["BLOCK"], coin_map_keys_from_source(source))

    def test_invalid_python_raises(self):
        with self.assertRaises(SyntaxError):
            coin_map_keys_from_source("coin_map = {")


class TestMissingChainSupport(unittest.TestCase):
    def test_all_supported_returns_empty(self):
        self.assertEqual(
            [],
            missing_chain_support(["BLOCK", "PIVX"],
                                  ["BLOCK", "TBLOCK", "BTC", "PIVX"]))

    def test_unsupported_chain_reported(self):
        self.assertEqual(
            ["XVG"],
            missing_chain_support(["BLOCK", "PIVX", "XVG"],
                                  ["BLOCK", "TBLOCK", "BTC", "PIVX"]))

    def test_empty_inputs(self):
        self.assertEqual([], missing_chain_support([], []))
        self.assertEqual([], missing_chain_support([], ["BLOCK"]))


class TestValidateUtxoChainsAgainstImage(unittest.TestCase):
    def test_returns_image_keys_when_covered(self):
        import autobuild.utils.utxo as utxo_mod
        orig = utxo_mod.image_coin_map_keys
        utxo_mod.image_coin_map_keys = lambda image: ["BLOCK", "PIVX"]
        try:
            self.assertEqual(
                ["BLOCK", "PIVX"],
                validate_utxo_chains_against_image(["BLOCK"], "img"))
        finally:
            utxo_mod.image_coin_map_keys = orig

    def test_missing_chain_raises_value_error(self):
        import autobuild.utils.utxo as utxo_mod
        orig = utxo_mod.image_coin_map_keys
        utxo_mod.image_coin_map_keys = lambda image: ["BLOCK", "PIVX"]
        try:
            with self.assertRaises(ValueError) as ctx:
                validate_utxo_chains_against_image(
                    ["BLOCK", "XVG"], "img")
            self.assertIn("XVG", str(ctx.exception))
        finally:
            utxo_mod.image_coin_map_keys = orig


class TestReadUtxoPluginSource(unittest.TestCase):
    def test_local_path_read(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(MAIN_PY_FIXTURE)
            path = f.name
        try:
            text = read_utxo_plugin_source(path)
            self.assertEqual(
                ["BLOCK", "TBLOCK", "BTC", "PIVX", "XVG"],
                coin_map_keys_from_source(text))
        finally:
            os.unlink(path)

    def test_file_url_read(self):
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as f:
            f.write(MAIN_PY_FIXTURE)
            path = f.name
        try:
            text = read_utxo_plugin_source("file://" + path)
            self.assertIn('"XVG"', text)
        finally:
            os.unlink(path)

    def test_missing_file_url_raises_without_network(self):
        with self.assertRaises(Exception):
            read_utxo_plugin_source("file:///nonexistent-xyz/main.py")


class TestValidateUtxoChainsAgainstSource(unittest.TestCase):
    def test_covered_returns_source_keys(self):
        self.assertEqual(
            ["BLOCK", "PIVX", "XVG"],
            validate_utxo_chains_against_source(
                ["BLOCK", "XVG"], ["BLOCK", "PIVX", "XVG"]))

    def test_missing_chain_raises_runtime_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            validate_utxo_chains_against_source(
                ["BLOCK", "XVG"], ["BLOCK", "PIVX"])
        self.assertIn("XVG", str(ctx.exception))

    def test_empty_selection_passes(self):
        self.assertEqual(
            ["BLOCK"], validate_utxo_chains_against_source([], ["BLOCK"]))

    def test_empty_source_keys_raise_runtime_error(self):
        # An empty authority must be fatal, never a silent pass.
        with self.assertRaises(RuntimeError):
            validate_utxo_chains_against_source(["BLOCK"], [])


class TestSnodeCoinList(unittest.TestCase):
    def _snode_with_source(self, text):
        import tempfile, os
        import autobuild.utils as utils_mod
        with tempfile.NamedTemporaryFile("w", suffix=".py",
                                         delete=False) as f:
            f.write(text)
            path = f.name
        try:
            return utils_mod.Snode(".", "/nonexistent.env",
                                   utxo_plugin_source=path)
        finally:
            os.unlink(path)

    def test_single_line_multi_entry_map_parsed_exactly(self):
        # Regression: the old greedy regex turned
        # {"BLOCK": A, "BTC": B} into ['BLOCK": A, "BTC'].
        snode = self._snode_with_source(
            'coin_map = {"BLOCK": A, "BTC": B}\n')
        self.assertEqual(["BLOCK", "BTC"],
                         snode.supported_utxo_plugin_chains)

    def test_multiline_fixture_parsed(self):
        snode = self._snode_with_source(MAIN_PY_FIXTURE)
        self.assertEqual(["BLOCK", "TBLOCK", "BTC", "PIVX", "XVG"],
                         snode.supported_utxo_plugin_chains)

    def test_source_without_coin_map_raises(self):
        with self.assertRaises(Exception) as ctx:
            self._snode_with_source("x = 1\n")
        self.assertIn("coin_map", str(ctx.exception))

    def test_network_failure_wrapped(self):
        import requests
        import autobuild.utils as utils_mod
        with patch.object(utils_mod.requests, "get",
                          side_effect=requests.ConnectionError("down")):
            with self.assertRaises(Exception) as ctx:
                utils_mod.read_utxo_plugin_source(
                    "https://example.invalid/main.py")
        self.assertIn("EROR", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
