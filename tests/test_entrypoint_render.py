#!/usr/bin/env python3
"""Unit tests for the rendered daemon entrypoints.

The entrypoint reads the runtime needs a coin declares at image build
(``/opt/blockchain/coin-params.sh``) at container start, so the render must
never depend on a local image and must never spawn a container. These tests
pin the rendered script's shape: the params are read from the image, the
launch flags reach the daemon command line, and the NEVM off-switch keeps
them out of Syscoin's.
"""

import unittest

from autobuild.app import J2_ENV


TEMPLATE = 'autobuild/templates/entrypoint_config.j2'


def render(**overrides):
    values = {
        'walletConfig': '',
        'config_name': 'raven',
        'daemon_stem': 'ravend',
        'writeNEVM': False,
        'nevmIP': '0.0.0.0',
        'daemon_family': None,
        'node_p2p_port': 8767,
        'wallet_rpc_port': 8766,
        'node_rpc_port': 18766,
        'daemon_rpc_threads': 32,
        'daemon_rpc_workqueue': 256,
        'rpc_thread_coins': ('unobtanium', 'digibyte'),
        'rpc_workqueue_coins': ('digibyte',),
    }
    values.update(overrides)
    return J2_ENV.get_template(TEMPLATE).render(values)


class EntrypointRenderTestCase(unittest.TestCase):
    def test_params_are_read_from_the_image_at_start(self):
        script = render()
        self.assertIn('_coin_params=/opt/blockchain/coin-params.sh', script)
        self.assertIn('[ -f "$_coin_params" ]', script)

    def test_params_are_parsed_never_sourced(self):
        # Sourcing the file would execute image content in the entrypoint;
        # the September render-hardening commit chose to parse it instead.
        script = render()
        self.assertNotIn('. "$_coin_params"', script)
        self.assertNotIn('. /opt/blockchain/coin-params.sh', script)
        self.assertIn('sed -n', script)

    def test_addnodes_are_appended_to_the_coin_conf(self):
        script = render()
        self.assertIn(
            'echo "addnode=$_node" >> /opt/blockchain/config/raven.conf', script)

    def test_file_logging_mirrors_the_daemon_log(self):
        script = render()
        self.assertIn('tail -F /opt/blockchain/data/debug.log &', script)

    def test_launch_flags_reach_the_daemon_command_line(self):
        script = render()
        self.assertIn(
            'exec $1 -conf=/opt/blockchain/config/raven.conf $_launch_flags',
            script)

    def test_nevm_suppresses_the_launch_flags(self):
        # An image off-switch on the command line would kill the SysGeth the
        # gethcommandline block just enabled.
        script = render(config_name='syscoin', writeNEVM=True)
        self.assertNotIn('$_launch_flags', script.split('# ensure docker')[1])

    def test_syscoin_never_takes_the_launch_flags(self):
        script = render(config_name='syscoin')
        self.assertNotIn('$_launch_flags', script)

    def test_thread_overrides_are_written_per_coin(self):
        self.assertIn('rpcthreads=32', render(config_name='digibyte'))
        self.assertNotIn('rpcthreads', render(config_name='raven'))

    def test_render_never_shells_out(self):
        # The render is pure templating: no docker, no subprocess. A coin's
        # params travel with its image instead of being read from a local one.
        with open('autobuild/app.py') as handle:
            source = handle.read()
        self.assertNotIn('subprocess', source)
        self.assertNotIn('docker run', source)


if __name__ == '__main__':
    unittest.main()
