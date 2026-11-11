"""Render data for the stack dashboard (Grafana + the Infinity data source).

The dashboard reads the same sources the wallet stack serves: every core
wallet over its JSON-RPC endpoint, the coin index over the adapter's HTTP
surface, and the service node over JSON-RPC. Grafana fetches each value at
view time, so the board cannot display a cached or invented number.

Two rules shape the design:

* **Nothing may be silent.** The builder renders one inventory row per
  deployed coin into the board itself. A reachable coin's probe fills that
  row; a stopped container leaves the probe empty and the row still renders
  with ``down`` in the state column and dashes in every numeric cell.
* **One query per source.** No shared proxy, no XRouter call: a probe always
  targets the component that owns the data, so a coin can never show another
  coin's height and no request leaves the local stack.

Nothing here writes a credential: the provisioned data sources reference
``${RPC_USER}``/``${RPC_PASSWORD}`` and Grafana resolves them from the
container environment, the same way the rendered wallet entrypoints do.
"""

import json

from autobuild.utils import autoconfig

INFINITY_TYPE = 'yesoreyeram-infinity-datasource'

# Two instances: the wallets answer only with HTTP basic auth, while the
# adapter's index surface is unauthenticated.
RPC_DS = 'stack-rpc'
REST_DS = 'stack-rest'

TICKER = 'ticker'
NODE = 'node'
RPC_URL = 'rpc'
REST_URL = 'url'

# The adapter owns every coin's ElectrumX session and answers the whole
# registry's index tip from one unauthenticated route.
ADAPTER_SERVICE = 'plugin-adapter'
ADAPTER_PORT = 5000
ADAPTER_HEIGHT_PATH = '/height'


def _adapter_height_url():
	return 'http://{}:{}{}'.format(ADAPTER_SERVICE, ADAPTER_PORT, ADAPTER_HEIGHT_PATH)


def daemon_rpc_port(branchpath, ticker):
	"""A coin's RPC port, read from the deployed coin configuration.

	The board reaches each wallet on the port its own conf declares, so a
	coin whose port moved is probed where it actually listens rather than on
	a hard-coded one.
	"""
	manifest = json.loads(autoconfig.load_template(autoconfig.manifest_content(branchpath)))
	entry = next((item for item in manifest if item['ticker'].upper() == ticker), None)
	if entry is None:
		raise ValueError(f'coin configuration has no entry for {ticker}')
	conf = autoconfig.load_template(
		branchpath.rstrip('/') + '/xbridge-confs/' + entry['xbridge_conf'])
	for line in conf.splitlines():
		key, separator, value = line.partition('=')
		if separator and key.strip().upper() == 'PORT':
			return int(value.strip())
	raise ValueError(f'{ticker}: {entry["xbridge_conf"]} has no Port')

# A wallet busy importing holds the chain lock for seconds at a time, so a
# probe can be slow without the daemon being down. Both data sources share
# this cap.
PROBE_TIMEOUT_SECONDS = 30

# Grafana's grid unit is 30px. A table row is ~40px at the default cell
# height, plus ~50px for the panel header and table header.
GRID_UNIT_PX = 30
TABLE_ROW_PX = 40
TABLE_CHROME_PX = 80
MIN_PANEL_HEIGHT = 3


def _wallet_targets(data, branchpath):
	"""One entry per core wallet this stack serves.

	BLOCK has no daemon container (the service node serves it in-process),
	so it comes from the node; every other wallet is reached by its compose
	service name on the backend network.
	"""
	wallets = []
	seen = set()
	node = data.get('blocknet_node')
	if node:
		wallets.append({
			TICKER: 'BLOCK',
			RPC_URL: 'http://{}:{}'.format(node, daemon_rpc_port(branchpath, 'BLOCK')),
		})
		seen.add('BLOCK')
	for daemon in data.get('daemons', []):
		if daemon.get('type') not in ('chain', 'hybrid'):
			continue
		name = str(daemon.get('name', '')).upper()
		if not name or name in ('BLOCK', 'TBLOCK') or name in seen:
			continue
		rpc = daemon.get('rpcPort')
		if not rpc:
			continue
		seen.add(name)
		wallets.append({TICKER: name, RPC_URL: 'http://{}:{}'.format(name, rpc)})
	return wallets


def _indexer_targets(data):
	"""One entry per deployed coin's index, read through the adapter.

	The adapter owns every coin's ElectrumX session and answers the whole
	registry's index tip from one route, so each row carries the coin it
	picks out of that answer.
	"""
	return [
		{TICKER: chain['name'], REST_URL: _adapter_height_url()}
		for chain in data.get('full_utxo_plugin_chains', [])
	]


def _unique(items):
	"""De-duplicated, order-preserving."""
	return list(dict.fromkeys(items))


def dashboard_template(data, branchpath):
	"""Compose the values the compose template and the dashboard need."""
	image = data.get('dashboard_image')
	if not image:
		raise ValueError('dashboard has no dashboard_image')

	wallets = _wallet_targets(data, branchpath)
	if not wallets:
		raise ValueError('dashboard has no wallet to monitor')

	indexers = _indexer_targets(data)
	node = data.get('blocknet_node')
	snode = None
	if node:
		snode = 'http://{}:{}'.format(node, daemon_rpc_port(branchpath, 'BLOCK'))

	return {
		'deploy_dashboard': True,
		'dashboard_image': image,
		'dashboard_bind': data.get('dashboard_bind', '127.0.0.1:3001'),
		'dashboard_refresh': data.get('dashboard_refresh', '30s'),
		'dashboard_wallets': wallets,
		'dashboard_indexers': indexers,
		'dashboard_snode': snode or '',
		# Hosts the data sources may reach. The plugin refuses anything not
		# listed, so a stale target fails loudly instead of silently querying
		# an endpoint that is no longer part of the stack.
		'dashboard_rpc_hosts': _unique(
			[wallet[RPC_URL] for wallet in wallets]
			+ ([snode] if snode else [])),
		'dashboard_rest_hosts': _unique(
			[indexer[REST_URL] for indexer in indexers]),
	}


def render_datasources(data):
	"""Provisioning file for the two Infinity instances.

	``${RPC_USER}``/``${RPC_PASSWORD}`` are resolved by Grafana from the
	container environment at startup, so the rendered file carries
	placeholders and never a credential.

	``ignoreStatusCodeCheck`` is what lets a wallet that answers HTTP 500
	("Loading block index") still produce a row: the JSON-RPC error body is
	parsed and the query's JSONata fallback turns it into nulls instead of a
	panel error.
	"""
	return {
		'apiVersion': 1,
		'datasources': [
			{
				'name': 'Core wallets',
				'uid': RPC_DS,
				'type': INFINITY_TYPE,
				'access': 'proxy',
				'isDefault': False,
				'editable': False,
				'jsonData': {
					'auth_method': 'basicAuth',
					'timeoutInSeconds': PROBE_TIMEOUT_SECONDS,
					'ignoreStatusCodeCheck': True,
					'allowedHosts': data['dashboard_rpc_hosts'],
				},
				'basicAuth': True,
				'basicAuthUser': '${RPC_USER}',
				'secureJsonData': {'basicAuthPassword': '${RPC_PASSWORD}'},
			},
			{
				'name': 'Stack REST',
				'uid': REST_DS,
				'type': INFINITY_TYPE,
				'access': 'proxy',
				'isDefault': True,
				'editable': False,
				'jsonData': {
					'timeoutInSeconds': PROBE_TIMEOUT_SECONDS,
					'ignoreStatusCodeCheck': True,
					'allowedHosts': data['dashboard_rest_hosts'],
				},
			},
		],
	}


def _inventory_query(ref_id, uid, rows):
	"""One row per deployed object, rendered by the builder.

	This is the spine of every table: it exists even when every container is
	stopped, so no coin can disappear from the board.
	"""
	return {
		'refId': ref_id,
		'datasource': {'type': INFINITY_TYPE, 'uid': uid},
		'type': 'json',
		'source': 'inline',
		'format': 'table',
		'parser': 'backend',
		'data': json.dumps(rows, separators=(',', ':')),
		'root_selector': '$',
		# The plugin's panel code dereferences url_options even for an inline
		# source; without this the panel fails to render with a JS error.
		'url_options': {'method': 'GET'},
	}


def _rpc_query(ref_id, url, method, params, root):
	"""An Infinity JSON query against a wallet's JSON-RPC endpoint."""
	return {
		'refId': ref_id,
		'datasource': {'type': INFINITY_TYPE, 'uid': RPC_DS},
		'type': 'json',
		'source': 'url',
		'format': 'table',
		'parser': 'backend',
		'url': url,
		'root_selector': root,
		'url_options': {
			'method': 'POST',
			'body_type': 'raw',
			'body_content_type': 'application/json',
			'data': json.dumps({
				'jsonrpc': '1.0',
				'id': 1,
				'method': method,
				'params': params,
			}, separators=(',', ':')),
		},
	}


def _rest_query(ref_id, url, root):
	"""An Infinity JSON query against an unauthenticated REST endpoint."""
	return {
		'refId': ref_id,
		'datasource': {'type': INFINITY_TYPE, 'uid': REST_DS},
		'type': 'json',
		'source': 'url',
		'format': 'table',
		'parser': 'backend',
		'url': url,
		'root_selector': root,
		# The plugin's panel code dereferences url_options on every target;
		# a GET still needs the key present.
		'url_options': {'method': 'GET'},
	}


def _wallet_root(ticker):
	"""Project getblockchaininfo onto the shared wallet row.

	A wallet still loading its block index (HTTP 500, JSON-RPC -28) yields
	nulls; a stopped container yields no probe at all and the inventory row
	renders as ``down``.
	"""
	return (
		'$type($) = "object" ? {{"ticker": "{t}", '
		'"state": $exists($.result) ? "up" : "starting", '
		'"height": $.result.blocks, "headers": $.result.headers, '
		'"disk": $.result.size_on_disk, '
		'"progress": $.result.verificationprogress, '
		'"ibd": $.result.initialblockdownload}} : '
		'{{"ticker": "{t}", "state": "down"}}'
	).format(t=ticker)


def _indexer_root(ticker):
	"""Project the adapter's index tip onto the shared indexer row.

	The route answers every coin's ElectrumX tip. A coin whose indexer is
	not serving has a null height, so it renders as ``down`` rather than
	disappearing; an unreachable adapter leaves the inventory row to carry
	the coin.
	"""
	return (
		'$type($) = "object" and $exists($.result) ? {{"ticker": "{t}", '
		'"state": ($exists($.result.{t}) and $.result.{t} != null) ? "up" : "down", '
		'"indexed": $.result.{t}}} : '
		'{{"ticker": "{t}", "state": "down"}}'
	).format(t=ticker)


def _snode_status_root():
	# Projected, not projected-after-fetch: the RPC also returns the service
	# node private key, which must never reach a browser frame.
	return (
		'$.result ? {"node": "snode", "alias": $.result[0].alias, '
		'"tier": $.result[0].tier, "status": $.result[0].status, '
		'"last_seen": $.result[0].timelastseenstr} : '
		'{"node": "snode"}'
	)


def _snode_network_root():
	return (
		'$.result ? {"node": "snode", "connections": $.result.connections, '
		'"daemon": $.result.subversion, "version": $.result.version, '
		'"xbridge": $.result.xbridgeprotocolversion, '
		'"xrouter": $.result.xrouterprotocolversion} : '
		'{"node": "snode"}'
	)


def _organize(index, rename=None, exclude=None):
	"""Order and label the columns a merged table exposes."""
	return {
		'id': 'organize',
		'options': {
			'excludeByName': exclude or {},
			'indexByName': index,
			'renameByName': rename or {},
		},
	}


def _merge(*transformations):
	return [{'id': 'merge', 'options': {}}] + list(transformations)


def _field_override(field, properties):
	return {
		'matcher': {'id': 'byName', 'options': field},
		'properties': properties,
	}


def _bool_mapping(true_text, false_text):
	return {
		'id': 'mappings',
		'value': [{'type': 'value', 'options': {
			'true': {'text': true_text[0], 'color': true_text[1], 'index': 0},
			'false': {'text': false_text[0], 'color': false_text[1], 'index': 1},
		}}],
	}


def _count_override(field):
	"""Exact integer, dash when the coin did not answer."""
	return _field_override(field, [
		{'id': 'unit', 'value': 'none'},
		{'id': 'decimals', 'value': 0},
		{'id': 'noValue', 'value': '—'},
	])


def _bytes_override(field):
	"""Byte count in the largest binary unit that fits."""
	return _field_override(field, [
		{'id': 'unit', 'value': 'decbytes'},
		{'id': 'decimals', 'value': 2},
		{'id': 'noValue', 'value': '—'},
	])


def _state_override(extra=None):
	"""The state column: ``down`` is what a missing probe renders as."""
	options = {
		'up': {'text': 'up', 'color': 'green', 'index': 0},
		'down': {'text': 'down', 'color': 'red', 'index': 1},
	}
	options.update(extra or {})
	return _field_override('state', [
		{'id': 'mappings', 'value': [{'type': 'value', 'options': options}]},
		{'id': 'noValue', 'value': 'down'},
		{'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': 'text'}},
	])


def _wallet_overrides():
	return [
		_state_override({'starting': {'text': 'starting', 'color': 'orange', 'index': 2}}),
		_count_override('height'),
		_count_override('headers'),
		_field_override('progress', [
			{'id': 'unit', 'value': 'percentunit'},
			{'id': 'decimals', 'value': 3},
			{'id': 'noValue', 'value': '—'},
			{'id': 'thresholds', 'value': {'mode': 'absolute', 'steps': [
				{'color': 'red', 'value': None},
				{'color': 'orange', 'value': 0.999},
				{'color': 'green', 'value': 0.9999},
			]}},
		]),
		_field_override('ibd', [
			_bool_mapping(('IBD', 'red'), ('at tip', 'green')),
			{'id': 'noValue', 'value': '—'},
			{'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': 'text'}},
		]),
		_bytes_override('disk'),
	]


def _indexer_overrides():
	return [
		_state_override(),
		_count_override('indexed'),
	]


def _node_overrides():
	return [
		_field_override('status', [
			{
				'id': 'mappings',
				'value': [{'type': 'value', 'options': {
					'running': {'text': 'running', 'color': 'green', 'index': 0},
					'offline': {'text': 'offline', 'color': 'red', 'index': 1},
				}}],
			},
			{'id': 'noValue', 'value': 'down'},
			{'id': 'color', 'value': {'mode': 'fixed', 'fixedColor': 'text'}},
		]),
		_field_override('last_seen', [
			{'id': 'noValue', 'value': '—'},
		]),
		_count_override('connections'),
		_count_override('version'),
		_count_override('xbridge'),
		_count_override('xrouter'),
	]


def _table_panel(panel_id, title, description, uid, targets, grid,
				 overrides=None, transformations=None, sort_by=TICKER):
	datasource = ({'type': 'datasource', 'uid': '-- Mixed --'}
				  if any(target['datasource']['uid'] != uid for target in targets)
				  else {'type': INFINITY_TYPE, 'uid': uid})
	return {
		'id': panel_id,
		'type': 'table',
		'title': title,
		'description': description,
		'datasource': datasource,
		'gridPos': grid,
		'targets': targets,
		'options': {
			'showHeader': True,
			'cellHeight': 'md',
			'footer': {'show': False},
			'sortBy': ([{'displayName': sort_by, 'desc': False}]
					   if sort_by else []),
		},
		'fieldConfig': {
			'defaults': {
				'custom': {
					'align': 'auto',
					'cellOptions': {'type': 'auto'},
					'filterable': False,
					'inspect': False,
				},
				'color': {'mode': 'thresholds'},
				'thresholds': {
					'mode': 'absolute',
					'steps': [{'color': 'green', 'value': None}],
				},
				'mappings': [],
			},
			'overrides': overrides or [],
		},
		'transformations': transformations or [],
	}


def _table_height(rows):
	"""Panel height that fits ``rows`` without scrolling."""
	pixels = rows * TABLE_ROW_PX + TABLE_CHROME_PX
	return max(MIN_PANEL_HEIGHT, -(-pixels // GRID_UNIT_PX))


def render_dashboard(data):
	"""One table per layer, with an inventory row per deployed object."""
	panels = []
	panel_id = 1
	y = 0

	def next_panel():
		nonlocal panel_id
		panel_id += 1
		return panel_id - 1

	def add_table(title, description, uid, targets, rows, overrides,
				  transformations, width=24, sort_by=TICKER):
		nonlocal y
		height = _table_height(rows)
		panels.append(_table_panel(
			next_panel(), title, description, uid, targets,
			{'x': 0, 'y': y, 'w': width, 'h': height},
			overrides, transformations, sort_by))
		y += height

	if data['dashboard_wallets']:
		targets = [_inventory_query(
			'inventory_wallets', RPC_DS,
			[{TICKER: wallet[TICKER]} for wallet in data['dashboard_wallets']])]
		for wallet in data['dashboard_wallets']:
			targets.append(_rpc_query(
				'wallet_' + wallet[TICKER], wallet[RPC_URL],
				'getblockchaininfo', [], _wallet_root(wallet[TICKER])))
		add_table(
			'Core wallets',
			'getblockchaininfo from every core wallet. A coin whose '
			'container is stopped keeps its row and shows down.',
			RPC_DS, targets, len(data['dashboard_wallets']), _wallet_overrides(),
			_merge(_organize(
				{TICKER: 0, 'state': 1, 'height': 2, 'headers': 3,
				 'progress': 4, 'ibd': 5, 'disk': 6},
				{'height': 'height', 'headers': 'headers', 'disk': 'disk',
				 'progress': 'progress', 'ibd': 'initial sync'})))

	if data['dashboard_indexers']:
		targets = [_inventory_query(
			'inventory_indexers', REST_DS,
			[{TICKER: indexer[TICKER]} for indexer in data['dashboard_indexers']])]
		for indexer in data['dashboard_indexers']:
			targets.append(_rest_query(
				'index_' + indexer[TICKER],
				indexer[REST_URL],
				_indexer_root(indexer[TICKER])))
		add_table(
			'ElectrumX indexers',
			'Index height from every coin\'s ElectrumX session, read through '
			'the adapter. A coin whose indexer is not serving keeps its row '
			'and shows down.',
			REST_DS, targets, len(data['dashboard_indexers']), _indexer_overrides(),
			_merge(_organize(
				{TICKER: 0, 'state': 1, 'indexed': 2},
				{'indexed': 'indexed height'})))

	if data.get('dashboard_snode'):
		targets = [
			_inventory_query('inventory_snode', RPC_DS, [{NODE: 'snode'}]),
			_rpc_query('snode_status', data['dashboard_snode'],
					   'servicenode', ['status'], _snode_status_root()),
			_rpc_query('snode_peers', data['dashboard_snode'],
					   'getnetworkinfo', [], _snode_network_root()),
		]
		add_table(
			'Service node',
			'servicenode status and getnetworkinfo from the service node.',
			RPC_DS, targets, 1, _node_overrides(),
			_merge(_organize(
				{'alias': 0, 'tier': 1, 'status': 2,
				 'last_seen': 3, 'connections': 4, 'daemon': 5,
				 'version': 6, 'xbridge': 7, 'xrouter': 8},
				{'last_seen': 'last seen', 'xbridge': 'xbridge proto',
				 'xrouter': 'xrouter proto'},
				{NODE: True})),
			sort_by=None)

	return {
		'uid': 'stack',
		'title': 'Stack',
		'description': 'Core wallets, ElectrumX indexers and the service node '
					   'of this deployment.',
		'schemaVersion': 39,
		'version': 1,
		'editable': True,
		'refresh': data.get('dashboard_refresh', '30s'),
		'time': {'from': 'now-5m', 'to': 'now'},
		'timepicker': {'refresh_intervals': ['30s', '1m', '5m', '15m']},
		'timezone': 'browser',
		'panels': panels,
		'templating': {'list': []},
		'annotations': {'list': []},
	}
