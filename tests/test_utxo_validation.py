#!/usr/bin/env python3
"""Unit tests for utxo-plugin chain validation and render helpers.

Covers the AST extraction of the coin_map registry (without executing the
file), the render-vs-image comparison, and the coin facts read from the
blockchain-configuration source of truth.

Stdlib only; host-runnable: `python3 -m unittest discover -s tests`
from the repo root.
"""
import unittest
from unittest.mock import patch

from autobuild.utils.utxo import (
    coin_facts,
    coin_map_from_source,
    coin_map_keys_from_source,
    missing_chain_support,
    utxo_template,
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


class TestCoinFacts(unittest.TestCase):
    def _facts(self, conf_text, ticker="BLOCK"):
        import autobuild.utils.utxo as utxo_mod
        manifest = '[{"ticker": "%s", "xbridge_conf": "blocknet--v1.conf"}]' % ticker

        def loader(url):
            return manifest if url.endswith("manifest-latest.json") else conf_text

        with patch.object(utxo_mod, "load_template", side_effect=loader):
            return coin_facts("/bcf", [ticker])

    def test_reads_prefixes_from_the_conf(self):
        facts = self._facts(
            "AddressPrefix=26\nScriptPrefix=28\n")
        self.assertEqual(
            {"BLOCK": {"p2pkh": "1a", "p2sh": ["1c"]}},
            facts)

    def test_a_six_decimal_coin_needs_no_divisor(self):
        # The bridge never prices an amount: the daemon reads satoshis. Only the
        # address prefix is read, so a coin with a different divisor is served
        # the same way.
        facts = self._facts(
            "AddressPrefix=30\nScriptPrefix=33\n", "XVG")
        self.assertEqual({"p2pkh": "1e", "p2sh": ["21"]}, facts["XVG"])

    def test_cashaddr_prefix_is_passed_through(self):
        facts = self._facts(
            "AddressPrefix=0\nScriptPrefix=5\n"
            "CashAddrPrefix=bitcoincash\n", "BCH")
        self.assertEqual("bitcoincash", facts["BCH"]["cashaddr_prefix"])

    def test_missing_fact_is_fatal_not_defaulted(self):
        with self.assertRaises(ValueError):
            self._facts("ScriptPrefix=28\n")

    def test_unknown_ticker_is_fatal(self):
        import autobuild.utils.utxo as utxo_mod
        manifest = '[{"ticker": "BLOCK", "xbridge_conf": "blocknet--v1.conf"}]'
        with patch.object(utxo_mod, "load_template", return_value=manifest):
            with self.assertRaises(ValueError) as ctx:
                coin_facts("/bcf", ["NOPE"])
        self.assertIn("NOPE", str(ctx.exception))


class TestUtxoTemplateMapping(unittest.TestCase):
    def test_each_chain_gets_its_coin_class_and_adapter_facts(self):
        plugin = {"chains": [{"name": "BLOCK"}, {"name": "XVG"}]}
        data = {
            "blocknet_node": "snode",
            "daemons": [
                {"name": "XVG", "ip": "172.31.10.73", "rpcPort": "20102"},
            ],
            "snode_ip": "172.31.5.204",
        }
        facts = {
            "BLOCK": {"p2pkh": "1a", "p2sh": ["1c"]},
            "XVG": {"p2pkh": "1e", "p2sh": ["21"]},
        }
        with patch("autobuild.utils.utxo.coin_facts", return_value=facts), \
                patch("autobuild.utils.utxo.autoconfig.coin_rpc_port",
                      return_value=41414):
            used, out = utxo_template(
                {"ip": {}}, "172.31.0.0/16", plugin, data,
                {"BLOCK": "Blocknet", "XVG": "Verge"}, "/bcf")
        classes = {c["name"]: c["coin_class"] for c in out["full_utxo_plugin_chains"]}
        self.assertEqual({"BLOCK": "Blocknet", "XVG": "Verge"}, classes)
        import json
        config = json.loads(out["adapter_coin_config"])
        self.assertEqual({"p2pkh": "1e", "p2sh": ["21"]}, config["XVG"])
        # The bridge never calls the coin daemon, so no credential is rendered
        # into the adapter's configuration.
        self.assertNotIn("daemon_url", config["BLOCK"])

    def test_base_chain_is_reached_on_the_node_not_a_daemon(self):
        # BLOCK has no container: the node serves it, so the indexer must be
        # pointed at the node's address and the node's RPC port.
        plugin = {"chains": [{"name": "BLOCK"}]}
        data = {"blocknet_node": "snode", "daemons": [], "snode_ip": "172.31.5.204"}
        facts = {"BLOCK": {"divisor": 100000000, "p2pkh": "1a", "p2sh": ["1c"]}}
        with patch("autobuild.utils.utxo.coin_facts", return_value=facts), \
                patch("autobuild.utils.utxo.autoconfig.coin_rpc_port",
                      return_value=41414):
            _, out = utxo_template(
                {"ip": {}}, "172.31.0.0/16", plugin, data,
                {"BLOCK": "Blocknet"}, "/bcf")
        chain = out["full_utxo_plugin_chains"][0]
        self.assertEqual("172.31.5.204", chain["daemon_addr"])
        self.assertEqual(41414, chain["rpc_port"])
        self.assertEqual("snode", chain["depends_on"])

    def test_testsnode_serves_tblock(self):
        # A testnet node serves TBLOCK, so the base chain follows the node.
        plugin = {"chains": [{"name": "TBLOCK"}]}
        data = {"blocknet_node": "testsnode", "daemons": [],
                "testsnode_ip": "172.31.5.9"}
        facts = {"TBLOCK": {"divisor": 100000000, "p2pkh": "8b", "p2sh": ["13"]}}
        with patch("autobuild.utils.utxo.coin_facts", return_value=facts), \
                patch("autobuild.utils.utxo.autoconfig.coin_rpc_port",
                      return_value=41419):
            _, out = utxo_template(
                {"ip": {}}, "172.31.0.0/16", plugin, data,
                {"TBLOCK": "BlocknetTestnet"}, "/bcf")
        chain = out["full_utxo_plugin_chains"][0]
        self.assertEqual("172.31.5.9", chain["daemon_addr"])
        self.assertEqual(41419, chain["rpc_port"])
        self.assertEqual("testsnode", chain["depends_on"])

    def test_a_chain_with_no_server_is_fatal(self):
        # Selecting a coin the stack neither runs nor serves must fail the
        # render, never emit an indexer pointed at nothing.
        plugin = {"chains": [{"name": "RVN"}]}
        data = {"blocknet_node": "snode", "daemons": [], "snode_ip": "172.31.5.204"}
        with patch("autobuild.utils.utxo.coin_facts", return_value={}), \
                patch("autobuild.utils.utxo.autoconfig.coin_rpc_port",
                      return_value=41414):
            with self.assertRaises(ValueError) as ctx:
                utxo_template(
                    {"ip": {}}, "172.31.0.0/16", plugin, data,
                    {"RVN": "Ravencoin"}, "/bcf")
        self.assertIn("RVN", str(ctx.exception))

    def test_a_stack_without_a_node_cannot_serve_the_base_chain(self):
        plugin = {"chains": [{"name": "BLOCK"}]}
        data = {"daemons": []}
        with patch("autobuild.utils.utxo.coin_facts", return_value={}):
            with self.assertRaises(ValueError):
                utxo_template(
                    {"ip": {}}, "172.31.0.0/16", plugin, data,
                    {"BLOCK": "Blocknet"}, "/bcf")


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
