from rich import print
from autobuild.utils.autoconfig import random_ip
import ast
import subprocess


def coin_map_keys_from_source(source):
	"""Extract coin_map ticker keys from main.py source without executing it.

	AST-based (no imports, no side effects). Returns tickers in definition
	order; [] when no coin_map assignment exists. Raises SyntaxError on
	unparseable source.
	"""
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, ast.Assign) and len(node.targets) == 1:
			target = node.targets[0]
			if isinstance(target, ast.Name) and target.id == "coin_map":
				if isinstance(node.value, ast.Dict):
					return [k.value for k in node.value.keys
							if isinstance(k, ast.Constant) and isinstance(k.value, str)]
	return []


def missing_chain_support(chains, image_coin_list):
	"""Chains selected for render that the image cannot serve."""
	return [c for c in chains if c not in image_coin_list]


def image_coin_map_keys(image):
	"""List coin_map tickers baked into a utxo-plugin image.

	Runs a side-effect-free AST probe inside a throwaway container; the
	image's code is parsed, never imported. Raises RuntimeError when docker
	is unavailable or the image cannot be probed.
	"""
	probe = ("import ast, sys; tree = ast.parse(open('/app/plugins/main.py').read());"
			 " out = []; [out.extend([k.value for k in n.value.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)])"
			 " for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1"
			 " and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'coin_map'"
			 " and isinstance(n.value, ast.Dict)]; print(' '.join(out))")
	try:
		proc = subprocess.run(
			["docker", "run", "--rm", "--entrypoint", "python3", image, "-c", probe],
			capture_output=True, text=True, timeout=120)
	except (OSError, subprocess.SubprocessError) as e:
		raise RuntimeError(f"cannot probe utxo-plugin image {image}: {e}")
	if proc.returncode != 0:
		raise RuntimeError(
			f"cannot probe utxo-plugin image {image}: {proc.stderr.strip()}")
	return proc.stdout.split()


def validate_utxo_chains_against_image(chains, image):
	"""Fail fast when the render selects chains the image cannot serve.

	Returns the image's coin list on success. Raises ValueError naming the
	unsupported chains (action: rebuild the image or drop the chains), or
	RuntimeError when the image cannot be probed at all.
	"""
	keys = image_coin_map_keys(image)
	missing = missing_chain_support(chains, keys)
	if missing:
		raise ValueError(
			f"utxo-plugin image {image} lacks coin support for "
			f"{', '.join(missing)} (image serves: {', '.join(keys)}); "
			"rebuild the image with the missing coin classes or drop the chains.")
	return keys


def validate_utxo_chains_against_source(chains, source_keys):
	"""Fail fast when the render selects chains the configured utxo-plugin
	source cannot serve.

	The source (--utxo-plugin-source: local path for devs, remote URL
	for production) is the authority, not the registry image tag.
	Returns the source coin list on success. Raises RuntimeError
	naming the unsupported chains (deliberately not ValueError:
	this branch is the authority check, so a miss is fatal here;
	contrast the image-probe branch, where RuntimeError means
	probe failure and only warns, while ValueError aborts).
	"""
	missing = missing_chain_support(chains, source_keys or [])
	if missing:
		raise RuntimeError(
			f"utxo-plugin source lacks coin support for "
			f"{', '.join(missing)} (source serves: "
			f"{', '.join(source_keys or [])}); fix the selection or "
			f"extend the source coin_map.")
	return list(source_keys or [])

def utxo_template(used_ip, subnet, utxo_plugin, data):

	# create dictionary to access utxo coin RPC port and container IP address
	utxo_coin_dict = {'BLOCK':{'ip':data['snode_ip'],'rpc_port':'41414'}}
	utxo_coins = [x['name'] for x in utxo_plugin['chains']]
	for daemon in data['daemons']:
		if daemon['name'] in utxo_coins:
			utxo_coin_dict[daemon['name']] = {'ip':daemon['ip'], 'rpc_port':daemon['rpcPort']}

	# prepare data to fill in utxo plugin container data in autobuild/templates/utxo.j2
#	print("utxo_coin_dict: ", utxo_coin_dict)
#	print("utxo_plugin: ", utxo_plugin)
#	print("data: ", data)
	final_data = {}
	final_data['full_utxo_plugin_chains'] = []
	utxo_container_ip = {}
	for coin_name in utxo_coins:
		utxo_plugin_chain = {'name':coin_name}
		ip_name = f'{coin_name.lower()}_utxo_plugin_ip'
		custom_ip = random_ip(subnet)
		if ip_name in used_ip['ip'].keys():
			utxo_plugin_chain['ip'] = used_ip['ip'][ip_name]
		else:
			while True:
				custom_ip = random_ip(subnet)
				if custom_ip not in used_ip['ip'].values():
					utxo_plugin_chain['ip'] = custom_ip
					used_ip['ip'][ip_name] = custom_ip
					break
		utxo_container_ip[coin_name] = utxo_plugin_chain['ip']
		utxo_plugin_chain['rpc_port'] = utxo_coin_dict[coin_name]['rpc_port']
		utxo_plugin_chain['daemon_addr'] = utxo_coin_dict[coin_name]['ip']
		utxo_plugin_chain['depends_on'] = coin_name if coin_name != 'BLOCK' else 'snode'
		final_data['full_utxo_plugin_chains'].append(utxo_plugin_chain)

	# prepare data to fill in plugin adapter container data in autobuild/templates/utxo.j2
	final_data['adapter_utxo_plugin_list'] = ','.join([f'{coin_name}:{utxo_container_ip[coin_name]}' for coin_name in utxo_coins])

	return [used_ip, final_data]
