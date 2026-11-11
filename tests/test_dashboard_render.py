#!/usr/bin/env python3
"""Unit tests for the stack dashboard render (Grafana + Infinity)."""

import json
import os
import shutil
import tempfile
import unittest

from autobuild.utils.dashboard import (
    PROBE_TIMEOUT_SECONDS, RPC_DS, REST_DS, dashboard_template,
    render_dashboard, render_datasources)


class DashboardTestCase(unittest.TestCase):
    """Provides a minimal blockchain-configuration-files checkout."""

    def setUp(self):
        self.branchpath = tempfile.mkdtemp() + '/'
        self.addCleanup(shutil.rmtree, self.branchpath, True)
        with open(os.path.join(self.branchpath, 'manifest-latest.json'), 'w') as handle:
            json.dump([
                {'ticker': 'BLOCK', 'blockchain': 'Blocknet',
                 'xbridge_conf': 'blocknet--v4.2.0.conf'},
            ], handle)
        os.makedirs(os.path.join(self.branchpath, 'xbridge-confs'))
        with open(os.path.join(self.branchpath, 'xbridge-confs',
                               'blocknet--v4.2.0.conf'), 'w') as handle:
            handle.write('Port=41414\n')

    def stack(self):
        return {
            'blocknet_node': 'snode',
            'dashboard_image': 'grafana/grafana:12.1.0',
            'dashboard_bind': '127.0.0.1:3001',
            'dashboard_refresh': '30s',
            'daemons': [
                {'name': 'UNO', 'type': 'chain', 'rpcPort': 65535},
                {'name': 'DGB', 'type': 'chain', 'rpcPort': 14022},
                {'name': 'SNODE', 'type': 'base'},
                {'name': 'XR_PROXY', 'type': 'base'},
            ],
            'full_utxo_plugin_chains': [
                {'name': 'BLOCK'},
                {'name': 'UNO'},
            ],
        }

    def render(self, data=None):
        return dashboard_template(data or self.stack(), self.branchpath)


class TestDashboardTargets(DashboardTestCase):
    def test_block_is_served_by_the_node(self):
        wallets = {wallet['ticker']: wallet['rpc']
                   for wallet in self.render()['dashboard_wallets']}
        self.assertEqual('http://snode:41414', wallets['BLOCK'])

    def test_every_chain_daemon_is_a_wallet(self):
        wallets = {wallet['ticker'] for wallet in self.render()['dashboard_wallets']}
        self.assertEqual({'BLOCK', 'UNO', 'DGB'}, wallets)

    def test_base_entries_are_not_wallets(self):
        wallets = {wallet['ticker'] for wallet in self.render()['dashboard_wallets']}
        self.assertNotIn('SNODE', wallets)
        self.assertNotIn('XR_PROXY', wallets)

    def test_indexers_come_from_the_utxo_chain_list(self):
        # The adapter answers every coin's index tip from one route, so each
        # row carries the coin it picks out of that answer.
        self.assertEqual(
            ['http://plugin-adapter:5000/height',
             'http://plugin-adapter:5000/height'],
            [indexer['url'] for indexer in self.render()['dashboard_indexers']])

    def test_no_xr_proxy_target_is_rendered(self):
        urls = [target.get('url', '')
                for panel in render_dashboard(self.render())['panels']
                for target in panel.get('targets', [])]
        self.assertFalse([url for url in urls if 'xr_proxy' in url])

    def test_a_stack_without_wallets_is_fatal(self):
        data = self.stack()
        data['daemons'] = []
        data['blocknet_node'] = None
        with self.assertRaises(ValueError):
            self.render(data)

    def test_a_missing_image_is_fatal(self):
        data = self.stack()
        del data['dashboard_image']
        with self.assertRaises(ValueError):
            self.render(data)


class TestDashboardRender(DashboardTestCase):
    def setUp(self):
        super().setUp()
        self.out = self.render()

    def test_every_wallet_is_an_allowed_rpc_host(self):
        hosts = render_datasources(self.out)['datasources'][0]['jsonData']['allowedHosts']
        for wallet in self.out['dashboard_wallets']:
            self.assertIn(wallet['rpc'], hosts)

    def test_rpc_hosts_are_unique(self):
        hosts = render_datasources(self.out)['datasources'][0]['jsonData']['allowedHosts']
        self.assertEqual(len(hosts), len(set(hosts)))

    def test_rest_hosts_cover_indexers(self):
        hosts = render_datasources(self.out)['datasources'][1]['jsonData']['allowedHosts']
        for indexer in self.out['dashboard_indexers']:
            self.assertIn(indexer['url'], hosts)

    def test_credentials_are_placeholders_never_values(self):
        blob = json.dumps(render_datasources(self.out))
        self.assertIn('${RPC_USER}', blob)
        self.assertIn('${RPC_PASSWORD}', blob)

    def test_every_wallet_and_indexer_is_a_query_target(self):
        ref_ids = {target['refId']
                   for panel in render_dashboard(self.out)['panels']
                   for target in panel.get('targets', [])}
        for wallet in self.out['dashboard_wallets']:
            self.assertIn('wallet_' + wallet['ticker'], ref_ids)
        for indexer in self.out['dashboard_indexers']:
            self.assertIn('index_' + indexer['ticker'], ref_ids)

    def test_wallet_queries_are_authenticated_json_rpc_posts(self):
        targets = [target
                   for panel in render_dashboard(self.out)['panels']
                   for target in panel.get('targets', [])]
        query = next(target for target in targets if target['refId'] == 'wallet_DGB')
        self.assertEqual('POST', query['url_options']['method'])
        self.assertEqual('application/json', query['url_options']['body_content_type'])
        self.assertEqual('getblockchaininfo',
                         json.loads(query['url_options']['data'])['method'])
        self.assertEqual(RPC_DS, query['datasource']['uid'])

    def test_every_wallet_has_an_inventory_row(self):
        inventory = next(
            target for panel in render_dashboard(self.out)['panels']
            for target in panel.get('targets', [])
            if target['refId'] == 'inventory_wallets')
        rows = json.loads(inventory['data'])
        self.assertEqual(
            {wallet['ticker'] for wallet in self.out['dashboard_wallets']},
            {row['ticker'] for row in rows})

    def test_every_indexer_has_an_inventory_row(self):
        inventory = next(
            target for panel in render_dashboard(self.out)['panels']
            for target in panel.get('targets', [])
            if target['refId'] == 'inventory_indexers')
        rows = json.loads(inventory['data'])
        self.assertEqual(
            {indexer['ticker'] for indexer in self.out['dashboard_indexers']},
            {row['ticker'] for row in rows})

    def test_indexer_queries_read_the_adapter(self):
        # The board never opens an ElectrumX socket directly; the adapter is
        # the one HTTP surface that owns every coin's index session.
        targets = [target
                   for panel in render_dashboard(self.out)['panels']
                   for target in panel.get('targets', [])
                   if target['refId'].startswith('index_')]
        self.assertTrue(targets)
        for target in targets:
            self.assertEqual(REST_DS, target['datasource']['uid'])
            self.assertEqual('http://plugin-adapter:5000/height', target['url'])

    def test_indexer_root_selects_its_own_coin(self):
        # One route answers every coin, so a row's projection must name the
        # coin it belongs to or a coin would show another coin's height.
        targets = [target
                   for panel in render_dashboard(self.out)['panels']
                   for target in panel.get('targets', [])
                   if target['refId'] == 'index_UNO']
        self.assertEqual(1, len(targets))
        self.assertIn('$.result.UNO', targets[0]['root_selector'])

    def test_every_target_carries_url_options(self):
        # Infinity's panel code dereferences ``url_options`` on every target,
        # including the inline inventory rows; a missing key kills the panel.
        for panel in render_dashboard(self.out)['panels']:
            for target in panel.get('targets', []):
                self.assertIn('method', target['url_options'], target['refId'])

    def test_datasources_tolerate_a_busy_node(self):
        # A wallet busy importing can need seconds to answer; the old 5s limit
        # reported it as down even though the daemon was up.
        for source in render_datasources(self.out)['datasources']:
            self.assertTrue(source['jsonData']['ignoreStatusCodeCheck'])
            self.assertEqual(
                source['jsonData']['timeoutInSeconds'], PROBE_TIMEOUT_SECONDS)

    def test_missing_probe_renders_as_down(self):
        panel = next(p for p in render_dashboard(self.out)['panels']
                     if p['title'] == 'Core wallets')
        override = next(o for o in panel['fieldConfig']['overrides']
                        if o['matcher']['options'] == 'state')
        props = {p['id']: p.get('value') for p in override['properties']}
        self.assertEqual('down', props['noValue'])

    def test_panels_never_overlap(self):
        seen = set()
        for panel in render_dashboard(self.out)['panels']:
            if panel['type'] == 'row':
                continue
            grid = panel['gridPos']
            for x in range(grid['x'], grid['x'] + grid['w']):
                for y in range(grid['y'], grid['y'] + grid['h']):
                    self.assertNotIn((x, y), seen,
                                     'panel {} overlaps'.format(panel['title']))
                    seen.add((x, y))

    def test_panel_ids_are_unique(self):
        ids = [panel['id'] for panel in render_dashboard(self.out)['panels']]
        self.assertEqual(len(ids), len(set(ids)))

    def test_refresh_is_rendered(self):
        data = dict(self.out)
        data['dashboard_refresh'] = '1m'
        self.assertEqual('1m', render_dashboard(data)['refresh'])


if __name__ == '__main__':
    unittest.main()
