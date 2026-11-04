from rich import print
from autobuild.utils.autoconfig import load_template, manifest_content, random_ip
import ast
import json
import subprocess


def coin_map_from_source(source):
	"""Extract the ticker -> ElectrumX coin class map from utxo-plugin source.

	AST-based (no imports, no side effects). Returns an ordered dict; {} when
	no coin_map assignment exists. Raises SyntaxError on unparseable source.
	"""
	tree = ast.parse(source)
	for node in tree.body:
		if isinstance(node, ast.Assign) and len(node.targets) == 1:
			target = node.targets[0]
			if isinstance(target, ast.Name) and target.id == "coin_map":
				if isinstance(node.value, ast.Dict):
					mapping = {}
					for key, value in zip(node.value.keys, node.value.values):
						if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
							continue
						if isinstance(value, ast.Constant) and isinstance(value.value, str):
							mapping[key.value] = value.value
						else:
							mapping[key.value] = ast.unparse(value)
					return mapping
	return {}


def coin_map_keys_from_source(source):
	"""Ticker keys only, kept for callers that need the membership list."""
	return list(coin_map_from_source(source))


def missing_chain_support(chains, image_coin_list):
	"""Chains selected for render that the image cannot serve."""
	return [c for c in chains if c not in image_coin_list]


def image_coin_map(image):
	"""The ticker -> coin class map baked into a utxo-plugin image.

	Runs a side-effect-free AST probe inside a throwaway container; the
	image's code is parsed, never imported. Raises RuntimeError when docker
	is unavailable or the image cannot be probed.
	"""
	probe = (
		"import ast, json; tree = ast.parse(open('/app/coins.py').read()); out = {};"
		" [out.update({k.value: v.value for k, v in zip(n.value.keys, n.value.values)"
		" if isinstance(k, ast.Constant) and isinstance(k.value, str)"
		" and isinstance(v, ast.Constant) and isinstance(v.value, str)})"
		" for n in tree.body if isinstance(n, ast.Assign) and len(n.targets) == 1"
		" and isinstance(n.targets[0], ast.Name) and n.targets[0].id == 'coin_map'"
		" and isinstance(n.value, ast.Dict)]; print(json.dumps(out))")
	try:
		proc = subprocess.run(
			["docker", "run", "--rm", "--entrypoint", "python3", image, "-c", probe],
			capture_output=True, text=True, timeout=120)
	except (OSError, subprocess.SubprocessError) as e:
		raise RuntimeError(f"cannot probe utxo-plugin image {image}: {e}")
	if proc.returncode != 0:
		raise RuntimeError(
			f"cannot probe utxo-plugin image {image}: {proc.stderr.strip()}")
	import json
	return json.loads(proc.stdout)


def image_coin_map_keys(image):
	"""Ticker keys only, kept for callers that need the membership list."""
	return list(image_coin_map(image))


def coin_facts(branchpath, tickers):
	"""Coin facts the adapter needs, read from the coin-configuration source.

	The deployment already renders every daemon from this repository; reading
	the same xbridge conf keeps the indexer, the daemon and the hub from
	disagreeing about a divisor or a version byte. No value has a default here:
	a missing or malformed key is a render error, not a guess.
	"""
	manifest = json.loads(load_template(manifest_content(branchpath)))
	entries = {entry["ticker"].upper(): entry for entry in manifest}
	facts = {}
	for ticker in tickers:
		entry = entries.get(ticker)
		if entry is None:
			raise ValueError(f"coin-configuration manifest has no entry for {ticker}")
		conf = load_template(
			branchpath.rstrip("/") + "/xbridge-confs/" + entry["xbridge_conf"])
		values = {}
		for line in conf.splitlines():
			key, separator, value = line.partition("=")
			if separator:
				values[key.strip().upper()] = value.strip()
		for key in ("COIN", "ADDRESSPREFIX", "SCRIPTPREFIX"):
			if key not in values:
				raise ValueError(
					f"{ticker}: {entry['xbridge_conf']} has no {key}")
		try:
			facts[ticker] = {
				"divisor": int(values["COIN"]),
				"p2pkh": format(int(values["ADDRESSPREFIX"]), "02x"),
				"p2sh": [format(int(values["SCRIPTPREFIX"]), "02x")],
			}
		except ValueError as error:
			raise ValueError(
				f"{ticker}: {entry['xbridge_conf']} has a non-numeric "
				f"COIN/AddressPrefix/ScriptPrefix") from error
		cashaddr = values.get("CASHADDRPREFIX")
		if cashaddr:
			facts[ticker]["cashaddr_prefix"] = cashaddr
		if ticker == "BCH" and "cashaddr_prefix" not in facts[ticker]:
			facts[ticker]["cashaddr_prefix"] = "bitcoincash"
	return facts


def validate_utxo_chains_against_image(chains, image):
	"""Fail fast when the render selects chains the image cannot serve.

	Returns the image's coin map on success. Raises ValueError naming the
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
	missing = missing_chain_support(chains, source_keys or {})
	if missing:
		raise RuntimeError(
			f"utxo-plugin source lacks coin support for "
			f"{', '.join(missing)} (source serves: "
			f"{', '.join(source_keys or {})}); fix the selection or "
			f"extend the source coin_map.")
	return source_keys if source_keys is not None else {}

def utxo_template(used_ip, subnet, utxo_plugin, data, coin_map=None, branchpath=None):

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
	coin_map = coin_map or {}
	for coin_name in utxo_coins:
		if coin_name not in coin_map:
			raise ValueError(f"utxo-plugin coin_map has no class for {coin_name}")
		utxo_plugin_chain = {'name':coin_name, 'coin_class':coin_map[coin_name]}
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
	if branchpath is None:
		raise ValueError("utxo_template needs branchpath to read coin facts")
	facts = coin_facts(branchpath, utxo_coins)
	adapter_config = {}
	for chain in final_data['full_utxo_plugin_chains']:
		coin_name = chain['name']
		entry = dict(facts[coin_name])
		entry['daemon_url'] = (
			f"http://${{RPC_USER}}:${{RPC_PASSWORD}}@"
			f"{chain['daemon_addr']}:{chain['rpc_port']}/")
		adapter_config[coin_name] = entry
	final_data['adapter_coin_config'] = json.dumps(adapter_config, separators=(',', ':'))

	return [used_ip, final_data]
