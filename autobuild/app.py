#!/usr/bin/env python3
import os
import stat
import json
import re
import ipaddress
import subprocess
from jinja2 import Environment, FileSystemLoader, Template, BaseLoader
from autobuild.utils.autoconfig import *
import autobuild.utils.autoconfig as autoconfig
from autobuild.utils.xquery import xq_template
from autobuild.utils.utxo import (
	utxo_template,
	image_coin_map,
	validate_utxo_chains_against_image,
	validate_utxo_chains_against_source,
)
from icecream import ic, install
from rich import print

ic.configureOutput(includeContext=True)   # show context
install()                                 # make available to imported modules

J2_ENV = Environment(loader=FileSystemLoader(''),
					 trim_blocks=True)

J2_ENV2 = Environment(loader=BaseLoader(),
					 trim_blocks=True)


OUTPUT_PATH = './'
IPS_CACHE = '.cache_ip'
UTXO_PLUGIN_METHODS = ['getutxos','getrawtransaction','getrawmempool','getblockcount','sendrawtransaction','gettransaction','getblock','getblockhash','heights','fees','getbalance','gethistory','ping']

# lbcd/lbcwallet (btcd-family) expose the node's internal RPC 10000 ports above
# the wallet facade's legacy RPC port. This is an lbcd/lbcwallet-specific
# offset, NOT the vanilla btcd convention (which uses +2).
BTCD_NODE_RPC_PORT_OFFSET = 10000

def _version_key(tag):
	"""Natural sort key for version tags, so 'v0.21.5.13' > 'v0.21.5.4'."""
	return tuple(int(n) for n in re.findall(r'\d+', tag))

def _build_wallet_config(BRANCHPATH, daemon):
	"""Build sanitized wallet config lines from the chain-specific conf.

	Raises RuntimeError if the conf is missing or unreadable: every daemon
	that reaches the render stage carries 'wallet_conf' from manifest data
	(unresolvable daemons are dropped earlier), so an unloadable conf is a
	broken deploy, never a case for a degraded generic fallback (which
	previously emitted empty rpcuser/rpcpassword below the entrypoint's
	own credentials).
	"""
	if 'wallet_conf' not in daemon:
		raise RuntimeError(
			"daemon '{}' has no wallet_conf; refusing to render entrypoint".format(
				daemon.get('name', '?')))
	conf_path = autoconfig.wallet_conf_file(BRANCHPATH, daemon['wallet_conf'])
	try:
		chain_conf_text = autoconfig.load_template(conf_path)
	except Exception as e:
		raise RuntimeError(
			"wallet conf for '{}' not loadable from {}: {}".format(
				daemon.get('name', '?'), conf_path, e))
	return autoconfig.parse_wallet_conf_lines(chain_conf_text)


def _is_sys(name):
	"""The SYS daemon, case-insensitively (daemon names are upper-cased
	elsewhere, but YAML input is free-form)."""
	return isinstance(name, str) and name.upper() == 'SYS'


def _coin_params(image, timeout=60):
	"""Runtime params declared at image build (coin-params.sh content).

	Returns a dict {launch_flags, addnodes, file_logging, tail_filter};
	empty dict when the image is absent locally (normal render-then-pull
	flow, all Hub images), docker is missing, or anything fails — which
	renders today's exact scripts (graceful fallback). Values are parsed,
	never sourced: strict VAR="value" lines only.
	"""
	empty = {'launch_flags': '', 'addnodes': [],
	         'file_logging': False, 'tail_filter': ''}
	if not image:
		return empty
	try:
		present = subprocess.run(
			["docker", "image", "inspect", image],
			capture_output=True, timeout=timeout)
		if present.returncode != 0:
			return empty
		proc = subprocess.run(
			["docker", "run", "--rm", "--entrypoint", "cat", image,
			 "/opt/blockchain/coin-params.sh"],
			capture_output=True, text=True, timeout=timeout)
	except (OSError, subprocess.TimeoutExpired):
		return empty
	if proc.returncode != 0:
		return empty
	vals = {}
	for line in (proc.stdout or "").splitlines():
		m = re.match(r'^([A-Z_]+)="([^"]*)"$', line.strip())
		if m:
			vals[m.group(1)] = m.group(2)
	parsed = {'launch_flags': vals.get('LAUNCH_FLAGS', ''),
	          'addnodes': vals.get('ADDNODES', '').split(),
	          'file_logging': vals.get('FILE_LOGGING', '') == '1',
	          'tail_filter': vals.get('TAIL_FILTER', '')}
	for token in parsed['launch_flags'].split() + parsed['addnodes']:
		if re.search(r'[^A-Za-z0-9_.:=-]', token):
			print("coin-params from '%s' has an unsafe token %r; ignoring all params" % (image, token))
			return empty
	if "'" in parsed['tail_filter']:
		print("coin-params from '%s' has a quoted tail filter; ignoring all params" % image)
		return empty
	return parsed


def processcustom(customlist, SUBNET, BRANCHPATH, utxo_supported_chains=None):
	if IPS_CACHE not in os.listdir(os.getcwd()):
		write_text_file(IPS_CACHE,json.dumps({'ip':{}}, indent=4, sort_keys=False))
	used_ip = json.loads(load_text_file(os.getcwd()+'/'+IPS_CACHE))

	print('Processing custom input...')
	to_del_index = []
	daemons_list = []
	configFiles = {}
	binFiles = {}
	rendered_files = []
	daemonFamily = {}
	daemonStem = {}
	rpc_threads = 0
	# A repeated daemon would render the same compose service twice, which
	# Compose rejects ("mapping key already defined") after the render has
	# already reported success. Refuse the input before anything is read or
	# written, and normalise the name to its upper-case ticker here: every
	# downstream lookup (manifest, xbridge section, compose service,
	# entrypoint name) keys coins that way.
	for c in customlist:
		seen_daemons = {}
		for i, daemon in enumerate(c.get('daemons', [])):
			daemon['name'] = str(daemon.get('name', '')).upper()
			key = daemon['name']
			if key in seen_daemons:
				raise RuntimeError(
					"daemon '{}' is listed twice in the input (entries {} and {}); "
					"refusing to render a stack with a duplicate service".format(
						daemon['name'], seen_daemons[key], i))
			seen_daemons[key] = i
	manifest_config = autoconfig.load_template(autoconfig.manifest_content(BRANCHPATH))
	manifest = json.loads(Template(manifest_config).render())
	for blockchain in manifest:
		configFiles[blockchain['ticker']] = blockchain['conf_name']
		if 'daemon_stem' in blockchain: # eg: SCC, XVG have non-standard daemon names
			binFiles[blockchain['ticker']] = blockchain['daemon_stem'] + 'd'
		else:
			binFiles[blockchain['ticker']] = blockchain['conf_name'].split('.conf')[0] + 'd'
		daemonFamily[blockchain['ticker']] = blockchain.get('daemon_family')
		daemonStem[blockchain['ticker']] = blockchain.get('daemon_stem',
		                                                  blockchain['conf_name'].split('.conf')[0])

	# conf_name is shared by a few mainnet/testnet + fork pairs (BTC/BCH,
	# BLOCK/TBLOCK, SYS/TSYS) whose daemon binaries genuinely share the
	# family name. Entrypoint script paths must still be unique per
	# deployed coin, so holders after the first-in-manifest one get a
	# ticker suffix in configName only. Binaries, image conf names and
	# xbridge sections are intentionally left alone.
	_first_conf_holder = {}
	for blockchain in manifest:
		_first_conf_holder.setdefault(blockchain['conf_name'], blockchain['ticker'])

	customlist[0]['deploy_nevm'] = False
	for c in customlist:
		miss_index = []
		miss_names = []
		skip_base_index = []
		for i in range(len(c['daemons'])):
			name = c['daemons'][i]['name']
			type = c['daemons'][i]['type']
			#daemon configs
			if type in ['chain', 'hybrid']:
				# BLOCK/TBLOCK are never standalone daemons: the Blocknet
				# wallet is served in-process by the node itself
				# (SNODE/TNODE serve BLOCK, TESTSNODE/TESTTNODE serve
				# TBLOCK via scripts/start-<node>.sh). Silently carrying
				# them here produced a spurious BLOCK compose service +
				# scripts/entrypoints/start-blocknet.sh that nothing
				# should consume. Drop them (covers stale --yaml inputs
				# that still list BLOCK as a chain).
				if isinstance(name, str) and name.upper() in ('BLOCK', 'TBLOCK'):
					print('Skip daemon (served by node): {}'.format(name))
					skip_base_index.append(i)
					continue
				try:
					print(f'fetch template for {name} from raw.git')
					xbridge_text = autoconfig.load_template(autoconfig.chain_lookup(BRANCHPATH, name))
					xtemplate = Template(xbridge_text)
					xresult = xtemplate.render()
					xbridge_json = json.loads(xresult)
					c['daemons'][i]['p2pPort'] = xbridge_json[name]['p2pPort']
					c['daemons'][i]['rpcPort'] = xbridge_json[name]['rpcPort']
					c['daemons'][i]['binFile'] = binFiles[name]
					c['daemons'][i]['daemon_family'] = daemonFamily.get(name)
					c['daemons'][i]['daemon_stem'] = daemonStem.get(name)
					conf_stem = configFiles[name].split('.conf')[0]
					if name != _first_conf_holder[configFiles[name]]:
						conf_stem = '{}-{}'.format(conf_stem, name.lower())
					c['daemons'][i]['configName'] = conf_stem
					tag = c['daemons'][i]['image'].split(':')[1]
					if '-staging' in tag:
						tag = tag.split('-staging')[0]
					if tag == 'latest':
						version_list = list(xbridge_json[name]['versions'])
						version_list.sort(key=_version_key)
						tag = version_list[-1]

					version_data = xbridge_json[name]['versions'][tag]
					c['daemons'][i]['deprecatedrpc'] = version_data.get('deprecatedrpc', False)
					c['daemons'][i]['legacy'] = version_data['legacy']
					if 'rpcserialversion' in version_data:
						c['daemons'][i]['rpcserialversion'] = version_data['rpcserialversion']
					if 'testnet' in version_data:
						c['daemons'][i]['testnet'] = version_data['testnet']
					else:
						c['daemons'][i]['testnet'] = False
					if 'wallet_conf' in version_data:
						c['daemons'][i]['wallet_conf'] = version_data['wallet_conf']
					if name in used_ip['ip'].keys():
						c['daemons'][i]['ip'] = used_ip['ip'][name]
					else:
						while True:
							custom_ip = autoconfig.random_ip(SUBNET)
							if custom_ip not in used_ip['ip'].values():
								c['daemons'][i]['ip'] = custom_ip
								used_ip['ip'][name] = custom_ip
								break
					if _is_sys(name):
						nevm_ip = c['daemons'][i]['ip']
					daemons_list.append(name.upper())
					rpc_threads += 1
				except Exception as e:
					print("Config for currency {} not found. The error is {}".format(name, e))
					miss_index.append(i)
					miss_names.append(name)
			else:
				#others configs
				to_del_index.append(i)

				if name.upper() in ['XR_PROXY', 'SNODE', 'TNODE', 'TESTSNODE', 'TESTTNODE']:
					if name.upper() not in ['XR_PROXY']:
						customlist[0]['blocknet_image'] = c['daemons'][i]['image']
						customlist[0]['blocknet_node'] = name.lower()
					else:
						if 'image' in list(c['daemons'][i]):
							customlist[0][f'{name.lower()}_image'] = c['daemons'][i]['image']
					if f'{name.lower()}_ip' in used_ip['ip'].keys():
						customlist[0][f'{name.lower()}_ip'] = used_ip['ip'][f'{name.lower()}_ip']
					else:
						while True:
							custom_ip = autoconfig.random_ip(SUBNET)
							if custom_ip not in used_ip['ip'].values():
								customlist[0][f'{name.lower()}_ip'] = custom_ip
								used_ip['ip'][f'{name.lower()}_ip'] = custom_ip
								break
				#deploy payment configs
				if name.upper() == 'PAYMENT':
					customlist[0][f'{name.lower()}_image'] = c['daemons'][i]['image']
					if 'payment_xquery' in list(c['daemons'][i]):
						customlist[0][f'{name.lower()}_xquery'] = c['daemons'][i]['payment_xquery']
					else:
						customlist[0][f'{name.lower()}_xquery'] = 35
					if 'payment_tier1' in list(c['daemons'][i]):
						customlist[0][f'{name.lower()}_tier1'] = c['daemons'][i]['payment_tier1']
					else:
						customlist[0][f'{name.lower()}_tier1'] = 35
					if 'payment_tier2' in list(c['daemons'][i]):
						customlist[0][f'{name.lower()}_tier2'] = c['daemons'][i]['payment_tier2']
					else:
						customlist[0][f'{name.lower()}_tier2'] = 200
					customlist[0]['deploy_payment'] = True
					for k in ['PG','PAYMENT']:
						if f'{k.lower()}_ip' in used_ip['ip'].keys():
							customlist[0][f'{k.lower()}_ip'] = used_ip['ip'][f'{k.lower()}_ip']
						else:    
							while True:
								custom_ip = autoconfig.random_ip(SUBNET)
								if custom_ip not in used_ip['ip'].values():
									customlist[0][f'{k.lower()}_ip'] = custom_ip
									used_ip['ip'][f'{k.lower()}_ip'] = custom_ip
									break
					customlist[0]['plugins'].append('projects')
				if name.upper() == 'ETH':                
					customlist[0]['deploy_eth'] = True
					if 'host' in list(c['daemons'][i]):
						customlist[0]['gethexternal'] = c['daemons'][i]['host']
						print("Using external geth")
					else:
						customlist[0][f'{name.lower()}_image'] = c['daemons'][i]['image']
						print("Using internal geth")
					for k in ['GETH']:
						if f'{k.lower()}_ip' in used_ip['ip'].keys():
							customlist[0][f'{k.lower()}_ip'] = used_ip['ip'][f'{k.lower()}_ip']
						else:
							while True:
								custom_ip = autoconfig.random_ip(SUBNET)
								if custom_ip not in used_ip['ip'].values():
									if k == 'GETH' and customlist[0]['gethexternal']:
										customlist[0][f'{k.lower()}_ip'] = customlist[0]['gethexternal']
										break
									customlist[0][f'{k.lower()}_ip'] = custom_ip
									used_ip['ip'][f'{k.lower()}_ip'] = custom_ip
									break
				if name.upper() == 'AVAX':
					if 'image' in list(c['daemons'][i]):
						customlist[0][f'{name.lower()}_image'] = c['daemons'][i]['image']
						customlist[0]['deploy_avax'] = True
						print("Using internal avax")
						if f'{name.lower()}_ip' in used_ip['ip'].keys():
							customlist[0][f'{name.lower()}_ip'] = used_ip['ip'][f'{name.lower()}_ip']
						else:
							while True:
								custom_ip = autoconfig.random_ip(SUBNET)
								if custom_ip not in used_ip['ip'].values():
									customlist[0][f'{name.lower()}_ip'] = custom_ip
									used_ip['ip'][f'{name.lower()}_ip'] = custom_ip
									break
					else:
						customlist[0]['deploy_avax'] = False
						customlist[0]['avaxexternal'] = True
						customlist[0][f'{name.lower()}_ip'] = c['daemons'][i]['host']
						print("Using external avax")
				if name.upper() == 'NEVM':
					if 'image' in list(c['daemons'][i]):
						customlist[0][f'{name.lower()}_image'] = c['daemons'][i]['image']
						customlist[0]['deploy_nevm'] = True
						print("Using internal nevm")
						customlist[0]['nevm_ip'] = nevm_ip
					else:
						customlist[0]['deploy_nevm'] = False
						customlist[0]['nevmexternal'] = True
						customlist[0][f'{name.lower()}_ip'] = c['daemons'][i]['host']
						print("Using external nevm")

				if name.upper() == 'HYDRA':
					print('HYDRA exists')
					customlist[0]['plugins'].append('evm_passthrough')
					if 'free' in c['daemons'][i].keys() and c['daemons'][i]['free'] == True:
						customlist[0]['plugins'].append('free_evm_passthrough')
					for j in c['daemons'][i]['chains']:
						print(f'HYDRA - {j["name"].upper()}')
						customlist[0]['hydra'].append(j['name'].upper())
						customlist[0]['plugins'].append(f'evm_passthrough_{j["name"].lower()}')
					
				if name.upper() == 'XQUERY':
					print('XQUERY exists')
					customlist[0]['deploy_xquery'] = True
					query = dict(c['daemons'][i])
					del query['name']
					customlist[0]['plugins'].append('xquery')

					# These following two lines add names like "xquery_pangolin", "xquery_pegasys" to plugins list
					# so the specific dexes (e.g. pangolin, pegasys) supported by xquery on this SNode are broadcast to
					# the Blocknet P2P network.
					for xq_dex in query['dexs']:
						customlist[0]['plugins'].append(f'xquery_{xq_dex["name"].lower()}')

					autoconfig.write_yaml_file('xquery.yaml',query)
					used_ip, qtemplate = xq_template(used_ip, SUBNET, query, customlist[0])
					for key, item in qtemplate.items():
						c[key] = item

				if name.upper() == 'UTXO_PLUGIN':
					print('UTXO_PLUGIN exists')
					customlist[0]['deploy_utxo'] = True
					customlist[0]['utxo_plugin_volume'] = c['daemons'][i]['volume']
					customlist[0]['utxo_plugin_image'] = c['daemons'][i]['utxo_plugin_image']
					customlist[0]['plugin_adapter_image'] = c['daemons'][i]['plugin_adapter_image']
					if name in used_ip['ip'].keys():
						customlist[0]['plugin_adapter_ip'] = used_ip['ip'][name]
					else:
						while True:
							custom_ip = autoconfig.random_ip(SUBNET)
							if custom_ip not in used_ip['ip'].values():
								customlist[0]['plugin_adapter_ip'] = custom_ip
								used_ip['ip']['plugin_adapter_ip'] = custom_ip
								break

#					utxo_plugin = dict(c['daemons'][i])
					customlist[0]['plugins'] += UTXO_PLUGIN_METHODS
					customlist[0]['utxo_plugin_methods'] = UTXO_PLUGIN_METHODS
					utxo_coin_map = utxo_supported_chains
					if utxo_coin_map is None:
						try:
							utxo_coin_map = image_coin_map(customlist[0]['utxo_plugin_image'])
						except RuntimeError as e:
							print(f"[bold yellow]WARNING: {e} (coin class map unavailable)[/bold yellow]")
							utxo_coin_map = {}
					used_ip, utxotemplate = utxo_template(
						used_ip, SUBNET, c['daemons'][i], customlist[0],
						utxo_coin_map, BRANCHPATH)
					for key, item in utxotemplate.items():
						c[key] = item
					# Fail fast when the render selects chains the utxo-plugin
					# cannot serve (crash-loop hours later otherwise).
					# Authority is the configured coin source
					# (--utxo-plugin-source: local path for devs, remote
					# URL for production), not the registry image tag.
					# A source miss raises outside the probe handler below
					# on purpose: it is fatal, never a skippable warning.
					# Without a source list (None), fall back to probing
					# the image: a probe failure (RuntimeError) warns and
					# continues, but a proven lack of support (ValueError)
					# propagates and aborts the render.
					utxo_chain_names = [x['name'] for x in utxotemplate['full_utxo_plugin_chains']]
					if utxo_supported_chains is not None:
						source_keys = validate_utxo_chains_against_source(
							utxo_chain_names, utxo_supported_chains)
						print(f"utxo-plugin source serves: {', '.join(source_keys)}")
					else:
						try:
							image_keys = validate_utxo_chains_against_image(
								utxo_chain_names,
								customlist[0]['utxo_plugin_image'])
							print(f"utxo-plugin image {customlist[0]['utxo_plugin_image']} serves: {', '.join(image_keys)}")
						except RuntimeError as e:
							print(f"[bold yellow]WARNING: {e} (coverage check skipped)[/bold yellow]")


				#volumes paths configs
				for j in list(c['daemons'][i]):
					if j not in ['name','image']:
						mount_dir = f'{name.lower()}_{j}'
						customlist[0][mount_dir] = os.environ.get(mount_dir.upper(),c['daemons'][i][j])

		# drop daemons whose manifest lookup failed (reverse order keeps indices valid)
		for i in sorted(miss_index, reverse=True):
			del c['daemons'][i]
		if miss_names:
			raise RuntimeError(
				"no manifest data for daemon(s) {}; refusing to render a partial stack".format(
					", ".join(miss_names)))

		write_text_file(IPS_CACHE,json.dumps(used_ip, indent=4, sort_keys=False))
		#check for missed configs
		#loading template vars
		template_vars = autoconfig.template_vars('autobuild/templates/{}'.format(c['j2template']))
		# print("template_vars:",template_vars)
		# print("c['daemons']:",c['daemons'])
		# print(daemons_list)
		# for index, var in enumerate(c['daemons']):
		#     #check if fake daemon or not (SNODE ETH XR_PROXY)
		#     if var['name'] in daemons_list:
		#         #compare daemons with template
		#         tocomp_a = list(var)
		#         tocomp_b = list(template_vars['daemons'])
		#         tocomp_a.sort()
		#         tocomp_b.sort()
		#         print(tocomp_a)
		#         print(tocomp_b)
		#         if set(tocomp_b).issubset(tocomp_a) == False:
		#             #if daemons missing config add to to_del_index
		#             print(f'invalid config in YAML for {var["name"]}:\nmissing {list(set(tocomp_a).symmetric_difference(set(tocomp_b)))}')
		#             to_del_index.append(index)

		#     elif var['name'].upper() in ['XR_PROXY', 'SNODE', 'TNODE', 'TESTSNODE', 'ETH', 'XQUERY', 'AVAX', 'PAYMENT']:
		#         continue

		#delete fake daemons SNODE ETH XR_PROXY plus base-chain daemons
		#(BLOCK/TBLOCK) served by the node itself. Single combined pass in
		#reverse order keeps indices valid.
		for i in sorted(set(to_del_index) | set(skip_base_index), reverse=True):
			del c['daemons'][i]

		if rpc_threads > 4:
			c['rpcthreads'] = rpc_threads * 2
		else:
			c['rpcthreads'] = 8

		custom_template_fname = 'autobuild/templates/{}'.format(c['j2template'])
		with open(custom_template_fname,'r') as file:
			template_string = file.read()
		custom_template = J2_ENV.from_string(template_string)
		# custom_template = J2_ENV2.get_template(custom_template_fname)
		rendered_data = custom_template.render(c)
		rendered_filename = '{}{}.yml'.format(OUTPUT_PATH, c['name'])
		# The compose file is what the deploy step consumes, so it is written
		# last: processconfigs() can still raise (a missing wallet conf, a
		# Blockbook chain the source cannot serve), and a failed render must
		# leave the previous file exactly as it was. write_text_file() renames
		# the new content over the target in one step, so the file on disk is
		# never a partial render.
		rendered_files.append((rendered_filename, rendered_data))

		plugins = ''
		if len(customlist[0]['plugins']) >= 1:
			for i in customlist[0]['plugins']:
				plugins = plugins + i +','
			plugins = plugins[:-1]

		customlist[0]['xrouter_plugins'] = plugins
		customlist[0]['hydra'] = ','.join([x.upper() for x in customlist[0]['hydra']])
		# Held until the whole render has succeeded: processconfigs() and
		# processdashboard() run after this function and can still raise, and
		# the compose file must not be replaced by a partial render.
		customlist[0]['rendered_files'] = rendered_files

		return([c])


def write_rendered_stack(datalist):
	"""Write the compose file(s) a successful render produced.

	Called only after every render stage has returned, so a failure anywhere
	in the pipeline leaves the previous compose file untouched.
	"""
	for data in datalist:
		for rendered_filename, rendered_data in data.get('rendered_files', []):
			write_text_file(rendered_filename, rendered_data)


def processconfigs(datalist, BRANCHPATH):
	if(datalist[0]["blocknet_node"].upper() in ['SNODE', 'TNODE']):
		base_block = 'BLOCK'
	else:
		base_block = 'TBLOCK'

	XBRIDGE_CONF = "[Main]\nFullLog=true\nLogPath=\nExchangeTax=300\nExchangeWallets="
	xr_wallet_tokens = [base_block]

	custom_template_ec = J2_ENV.get_template('autobuild/templates/entrypoint_config.j2')

	for data in datalist:
		for daemon in data['daemons']:
			name = daemon['name']
			# BLOCK/TBLOCK are never standalone daemons: the Blocknet
			# wallet is served in-process by the node itself
			# (SNODE/TNODE serve BLOCK, TESTSNODE/TESTTNODE serve TBLOCK
			# via scripts/start-<node>.sh). Rendering an entrypoint for
			# them produced scripts/entrypoints/start-blocknet.sh, which
			# nothing should consume.
			if isinstance(name, str) and name.upper() in ('BLOCK', 'TBLOCK'):
				print('Skip daemon (served by node): {}'.format(name))
				continue
			if name.upper() not in ['TNODE', 'SNODE', 'TESTSNODE', 'TESTTNODE', 'ETH', 'XR_PROXY']:
				xr_wallet_tokens.append(name.upper())
				template_wc = _build_wallet_config(BRANCHPATH, daemon)
				if _is_sys(name) and datalist[0]['deploy_nevm'] is True:
					write_nevm = True
					nevm_ip = datalist[0]['nevm_ip']
				else:	 
					write_nevm = False
					nevm_ip = '0.0.0.0'
				# btcd-family daemons (lbcd/lbcwallet) expose three distinct ports.
				# The proxy talks to the wallet facade's legacy RPC; the node's
				# internal RPC is localhost-only and sits BTCD_NODE_RPC_PORT_OFFSET
				# (10000) ports above the wallet RPC port.
				wallet_rpc_port = int(daemon.get('rpcPort', 9245))
				node_p2p_port = int(daemon.get('p2pPort', 9246))
				node_rpc_port = wallet_rpc_port + BTCD_NODE_RPC_PORT_OFFSET

				rendered_data_ec = custom_template_ec.render({
					'walletConfig': template_wc,
					'config_name': daemon['configName'],
					'daemon_stem': daemon.get('daemon_stem'),
					'writeNEVM': write_nevm,
					'nevmIP': nevm_ip,
					'daemon_family': daemon.get('daemon_family'),
					'node_p2p_port': node_p2p_port,
					'wallet_rpc_port': wallet_rpc_port,
					'node_rpc_port': node_rpc_port,
					'coin_params': _coin_params(daemon.get('image', '')),
					})
				config_name = './scripts/entrypoints/start-{}.sh'.format(daemon['configName'])
				print('Creating File: {}'.format(config_name))
				autoconfig.save_config(rendered_data_ec, config_name)
				st = os.stat(config_name)
				os.chmod(config_name, st.st_mode | stat.S_IEXEC)

	XBRIDGE_CONF += ",".join(dict.fromkeys(xr_wallet_tokens))
	XBRIDGE_CONF += '\n\n'

	xr_router_tokens = [base_block]
	for data in datalist:
		p2pport = ''
		rpcport = ''
		username = os.environ.get("RPC_USER", "${RPC_USER}")
		password = os.environ.get("RPC_PASSWORD", "${RPC_PASSWORD}")
		for daemon in data['daemons']:
			name = daemon['name']
			ip = daemon['ip']
			if name.upper() not in ['TNODE', 'SNODE', 'TESTSNODE', 'TESTTNODE', 'ETH', 'XR_PROXY']:
				xr_router_tokens.append(name.upper())
				if name.upper() == base_block:
					# The base chain is always served by the node itself
					# (snode serves BLOCK, tnode serves TBLOCK); its
					# section is emitted once by the base append below.
					# Emitting a wallet section too produces a duplicate
					# section name, which the XBridge loader rejects —
					# blanking the whole wallet config (no connectors,
					# empty dxgetlocaltokens).
					print('Skip Xbridge (served by node): {}'.format(name))
				else:
					XBRIDGE_CONF += "{}\n\n".format(autoconfig.generate_confs(BRANCHPATH, name, p2pport, rpcport, username, password, ip))
					print('Add Xbridge: {}'.format(name))
		# Add BLOCK settings
		XBRIDGE_CONF += "{}\n\n".format(autoconfig.generate_confs(BRANCHPATH, base_block, p2pport, rpcport, username, password, '127.0.0.1'))

	autoconfig.save_config(XBRIDGE_CONF, os.path.join('./scripts/config', 'xbridge.conf'))
	XR_TOKENS = ",".join(dict.fromkeys(xr_router_tokens))
	custom_template_xr = J2_ENV.get_template('autobuild/templates/xrouter.j2')
	XROUTER_CONF = custom_template_xr.render({'XR_TOKENS': XR_TOKENS, 'xrouter_plugins': datalist[0]['xrouter_plugins']})

	custom_template_snode = J2_ENV.get_template(f'autobuild/templates/{datalist[0]["blocknet_node"]}.j2')
	datalist[0]['XROUTER_CONF'] = XROUTER_CONF
	datalist[0]['XBRIDGE_CONF'] = XBRIDGE_CONF
	rendered_data_snode = custom_template_snode.render(datalist[0])
	autoconfig.save_config(rendered_data_snode, f'./scripts/start-{datalist[0]["blocknet_node"]}.sh')

	custom_template_uw = J2_ENV.get_template('autobuild/templates/xrproxy.j2')
	rendered_data_uw = custom_template_uw.render(datalist[0])
	autoconfig.save_config(rendered_data_uw, './scripts/start-xrproxy.sh')
	autoconfig.make_executable_files('./scripts')
	autoconfig.make_executable_files('./scripts/entrypoints')

	

