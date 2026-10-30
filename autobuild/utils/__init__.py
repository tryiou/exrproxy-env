import re
from rich import print
from rich.table import Table
import math
import docker
import sys
import os
import subprocess
import requests
import yaml
import json
import dotenv
import socket
import psutil
import pathlib
from autobuild.utils.utxo import coin_map_keys_from_source
import shutil
import re
from packaging import version
from questionary import Style, Choice, checkbox, confirm, text, password, select

input_template = [{
	'daemons': []
}]

input_template_args = [{
	'j2template': 'dockercompose.j2',
	'name': 'docker-compose',
	'plugins': [],
	'hydra': [],
	'deploy_eth': False,
	'gethexternal': False,
	'eth_testnet': False,
	'syncmode': 'full'
}]


def read_utxo_plugin_source(source):
	"""Read utxo-plugin main.py source from a local path or a URL.

	`file://` URLs and plain filesystem paths are read from disk (dev loops
	against a local checkout — no server needed); anything else is fetched
	over HTTP. Raises Exception (same shape as the fetch failure) when the
	source cannot be read.
	"""
	if source.startswith("file://"):
		path = source[len("file://"):]
		if not os.path.isfile(path):
			raise Exception(f"EROR: Failed to read list of supported UTXO Plugin chains from {source}: file not found.")
		source = path
	if os.path.isfile(source):
		try:
			with open(source) as f:
				return f.read()
		except OSError as e:
			raise Exception(f"EROR: Failed to read list of supported UTXO Plugin chains from {source}: {e}.")
	try:
		get_utxo_plugin_code_response = requests.get(source, timeout=30)
	except requests.RequestException as e:
		raise Exception(f"EROR: Failed to get list of supported UTXO Plugin chains from {source}: {e}.")
	if get_utxo_plugin_code_response.status_code != 200:
		raise Exception(f"EROR: Failed to get list of supported UTXO Plugin chains from {source}.")
	return get_utxo_plugin_code_response.text


def _source_default_volume(source, name):
	"""Default install location for `name` as declared in sources.yaml."""
	for entry in source or []:
		if str(entry.get('name', '')).upper() == str(name).upper():
			return entry.get('volume')
	return None


def heal_known_volumes(known_volumes, source):
	"""Forget recorded install locations whose directory no longer exists.

	`.known_volumes` outlives the directories it points at: moving or
	deleting a chaindir tree leaves a stale absolute path behind, and every
	caller that trusts it (the disk preflight, the rendered bind mounts)
	then works on a path that is not there. The sources.yaml default is the
	only safe fallback, so a lost directory means "forget it" rather than
	"crash" or "silently mount an empty path as root".

	Returns (healed, dropped) where dropped is a list of
	(name, path, fallback, reason) tuples, so the caller can report each
	repair and persist the healed mapping.
	"""
	healed = dict(known_volumes or {})
	volumes = {}
	dropped = []
	for name, path in (healed.get('volumes') or {}).items():
		fallback = _source_default_volume(source, name)
		reason = None
		if not isinstance(path, str) or not os.path.isabs(path):
			reason = 'not an absolute path'
		elif not os.path.isdir(path):
			reason = 'directory does not exist'
		if reason is None:
			volumes[name] = path
			continue
		dropped.append((name, path, fallback, reason))
	healed['volumes'] = volumes
	return healed, dropped


class Inquirer():
	def __init__(self):
		self.style = Style.from_dict({
			'separator': '#cc5454 bold',
			'questionmark': '#cc5454 bold',
			'selected': '#00ff00 bold',
			'pointer': '#673ab7 bold',
			'instruction': '#808080 bold',
			'answer': '#f44336 bold',
			'question': '#00ff00 bold'
		})

	def pick_checkbox(self,ask,choices):
		answer = checkbox(
			ask,
			choices=[Choice(title=c['name'], checked=c.get('checked', False)) for c in choices],
			style=self.style
		).ask()
		return answer

	def ask_question(self,ask, default=True):
		answer = confirm(ask, default=default, style=self.style).ask()
		return answer

	def get_input(self,ask):
		answer = text(ask, style=self.style).ask()
		return answer

	def get_password(self,ask):
		answer = password(ask, style=self.style).ask()
		return answer

	def pick_one(self, ask, choices):
		answer = select(ask, choices=choices, style=self.style).ask()
		return answer

def resolve_compose():
    """Return (cmd, version_str) for the docker compose v2 plugin or docker-compose (v1/v2), or (None, None)."""
    try:
        proc = subprocess.check_output('docker compose version', shell=True).decode('UTF-8')
        m = re.search(r"\d+[.]\d+[.]*\d*", proc)
        if m:
            return ('docker compose', m.group())
    except Exception:
        pass
    if shutil.which('docker-compose'):
        try:
            proc = subprocess.check_output('docker-compose version', shell=True).decode('UTF-8')
            m = re.search(r"\d+[.]\d+[.]*\d*", proc)
            if m:
                return ('docker-compose', m.group())
        except Exception:
            pass
    return (None, None)

class Snode():
	#: Default main.py source scraped for the supported utxo-plugin coin list.
	#: Overridable via builder.py --utxo-plugin-source: an HTTP(S) URL (e.g. a
	#: fork branch for dev loops) or a local file path (e.g. a workspace
	#: checkout); the gate and render logic are identical either way.
	DEFAULT_UTXO_PLUGIN_SOURCE = 'https://raw.githubusercontent.com/blocknetdx/utxo-plugin/main/main.py'

	def __init__(self, dirname, envfile, utxo_plugin_source=None):
		self.dirname = dirname
		self.inquirer = Inquirer()
		self.envfile = envfile
		self.config = dotenv.dotenv_values(self.envfile)
		self.utxo_plugin_source = utxo_plugin_source or self.DEFAULT_UTXO_PLUGIN_SOURCE
		source_text = read_utxo_plugin_source(self.utxo_plugin_source)
		self.supported_utxo_plugin_chains = coin_map_keys_from_source(source_text)
		if not self.supported_utxo_plugin_chains:
			raise Exception(f"EROR: No coin_map entries found in utxo-plugin source {self.utxo_plugin_source}.")
		#print("Supported UTXO Plugin Chains: ", self.supported_utxo_plugin_chains)

	def get_sudo(self):
		granted = False
		while not granted:
			sudo_pass = self.inquirer.get_password('Type your sudo pass:')
			proc = subprocess.Popen(['sudo', '-S', 'echo', 'VERIFIED'], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE).communicate(input='{}\n'.format(sudo_pass).encode())
			for response in proc:
				if 'VERIFIED' in response.decode('UTF-8'):
					self.sudo_pass = sudo_pass
					subprocess.Popen(['sudo', '-k'])
					print(f'[bold green]Sudo password verified[/bold green]')
					granted = True
					break
			if not granted:
				subprocess.Popen(['sudo', '-k'])
				print(f'[bold red]Wrong sudo password[/bold red]')

	def comparedisksize(self, app_list, directory_list, req_list):
		if app_list != ['TOTAL']:
			self.dir_cache = {}
		table = Table(box=None)
		table.add_column('',justify='left', style='bold cyan', no_wrap=True)
		table.add_column('Directory',justify='left', style='bold yellow', no_wrap=True)
		table.add_column('Available',justify='left', style='bold yellow', no_wrap=False)
		table.add_column('Required',justify='left', style='bold yellow', no_wrap=False)
		table.add_column('Existing dir',justify='left', style='bold yellow', no_wrap=False)
		table.add_column('',justify='left', style='bold yellow', no_wrap=False)
		table.add_column('Checks',justify='left', no_wrap=False)
		table.add_column('Requirement Calculations',justify='left', style='bold yellow', no_wrap=False)
		for index, app in enumerate(app_list):
			volume = directory_list[index]
			# A lost install location (moved/deleted chaindirs) must report,
			# never abort the build: heal_known_volumes() already reverted it
			# to the sources.yaml default, so reaching this branch means even
			# that default is absent. Measure its parent so the free-space
			# figure still belongs to the right filesystem.
			volume_exists = os.path.isdir(volume)
			dirr = volume if volume_exists else str(pathlib.Path(volume).parent)
			unmeasurable = False
			try:
				gb = round(psutil.disk_usage(dirr).free / (2**30))
			except OSError:
				gb = 0
				unmeasurable = True
			# Only the directly measured path is meaningful for `du`: a missing
			# volume has no per-chain dirs to size.
			sizeable = volume_exists and not unmeasurable
			exists = 0
			found = ''
			if sizeable:
				try:
					dirs = os.listdir(directory_list[index])
					_app = app
					if app == 'PAYMENT':
						_app = 'eth_pymt_db'
					for i, dir in enumerate(dirs):
						if _app.lower() == dir.lower():
							found = dir
							print(f'Found [bold cyan]{dir}[/bold cyan]')
							proc = subprocess.Popen(
								['sudo', '-S', 'du', '-k', '-d', '0', f'{dirr}/{dir}'],
								stdin=subprocess.PIPE,
								stdout=subprocess.PIPE,
								stderr=subprocess.PIPE
							).communicate(input=f'{self.sudo_pass}\n'.encode())
							data = int(int(proc[0].decode('UTF-8').split('\t')[0]))
							exists = round(data/float(1<<20),2)
							if dirr in self.dir_cache:
								self.dir_cache[dirr] += exists
							else:
								self.dir_cache[dirr] = exists
							# gb += exists
				except:
					pass
			if app == 'TOTAL':
				if directory_list[index] in self.dir_cache:
					exists = self.dir_cache[directory_list[index]]
			req_display = 'unknown' if req_list[index] <= 0 else f'{req_list[index]} GB'
			if not volume_exists:
				# Never a silent PASS: this location is not on disk, so the
				# deploy would bind-mount an empty directory created as root.
				# Distinct from UNKNOWN ("footprint not declared"), and the
				# free-space figure belongs to the parent filesystem.
				check, colour = 'NO DIR', 'red'
				calc_display = f'[{colour}]missing {volume}[/{colour}]'
			elif req_list[index] <= 0:
				# Unknown footprint (e.g. disk: 0 in sources.yaml): never
				# silent-PASS a safety check that cannot fail. Visible,
				# non-blocking; real requirements keep exact math.
				check, colour = 'UNKNOWN', 'yellow'
				calc_display = f'[{colour}]unknown requirement[/{colour}]'
			elif gb-req_list[index]+exists <=0:
				check, colour = 'WARNING', 'red'
				calc_display = f'[{colour}]{"{:.2f}".format(gb)}-({req_list[index]}-{exists}) = {"{:.2f}".format(gb-req_list[index]+exists)}[/{colour}] GB'
			else:
				check, colour = 'PASS', 'green'
				calc_display = f'[{colour}]{"{:.2f}".format(gb)}-({req_list[index]}-{exists}) = {"{:.2f}".format(gb-req_list[index]+exists)}[/{colour}] GB'
			table.add_row(app.ljust(8,' '), directory_list[index], f'[{colour}]{"{:.2f}".format(gb)} GB[/{colour}]',req_display,found, f'{exists} GB',f'[bold {colour}]{check}[/bold {colour}]',calc_display)
			if not volume_exists:
				print(f'[bold red]{directory_list[index]} is not on disk: {gb} GB free measured on {dirr} (would be created empty on deploy).[/bold red]')
			if app == 'TOTAL' and check == 'WARNING':
				print(f'[bold red]{"{:.2f}".format(gb-req_list[index]+exists)} GB more are required.\nYou might wanna change a {directory_list[index]} install location.[/bold red]')
		subprocess.Popen(['sudo','-k'])
		print(table)

	def atexit(self, code=0):
		if hasattr(self, 'client'):
			self.client.close()
		print("[bold red]Closing...[/bold red]")
		sys.exit(code)

	def checks(self):
		self.client = self.get_client()
		self.check_running_containers()

	def docker_prune(self):
		print('[bold cyan]Checking docker daemon...[/bold cyan]')
		try:
			self.client = docker.from_env()
			print('[bold cyan]Docker found[/bold cyan]')
		except Exception as e:
			self.atexit()
		self.prune_containers()
		self.prune_networks()
		self.prune_volumes()
		self.prune_images()
		self.atexit()

	def get_client(self):
		print('[bold cyan]Checking docker daemon...[/bold cyan]')
		self.check_docker()
		try:
			docker_client = docker.from_env()
			# print('[bold cyan]Docker found[/bold cyan]')
			return docker_client
		except Exception as e:
			print('[bold red]Something happened during Docker checks[/bold red]')
			# print("[bold cyan]Consider updating via:[/bold cyan]")
			# print("[bold cyan]./exr_env.sh --undocker[/bold cyan]")
			# print("[bold cyan]./exr_env.sh --docker[/bold cyan]")
			sys.exit(0)

	def check_docker(self):
		stream = os.popen("which docker")
		output = stream.read()
		if 'docker' not in output:
			print("[bold cyan]Docker not installed[/bold cyan]")
			print("[bold red]Install Docker Engine version v20.10.13 or latest.[/bold red]") # Please update via:
			sys.exit(0)
			# self.install_docker()
		elif 'docker' in output:
			proc = subprocess.check_output('docker --version',stdin=subprocess.PIPE, shell=True).decode('UTF-8')
			d_version = version.parse(re.search(r"\d+[.]\d+[.]*\d*", proc).group())
			if d_version < version.parse('20.10.13'):
				print("[bold red]Docker Engine version lower than v20.10.13. Update to continue[/bold red]") # Please update via:
				# print("[bold cyan]./exr_env.sh --undocker[/bold cyan]")
				# print("[bold cyan]./exr_env.sh --docker[/bold cyan]")
				sys.exit(0)				
			else:
				print(f"[bold cyan]Docker Engine version {d_version}[/bold cyan]")
				compose_cmd, compose_ver = resolve_compose()
				if compose_cmd is None:
					print("[bold cyan]Docker Compose not installed\nPlease install docker, docker-compose or start the daemon to continue...[/bold cyan]")
					sys.exit(0)
				dc_version = version.parse(compose_ver)
				print(f"[bold cyan]{compose_cmd} v{dc_version}[/bold cyan]")

	# def install_docker(self):
	# 	answer = self.inquirer.ask_question("Do you want to install docker?")
	# 	if answer ==  True:
	# 		print('[bold cyan]To install docker type:\n./exr_env.sh --docker[/bold cyan]')
	# 		sys.exit(0)
	# 	else:
	# 		print("[bold cyan]Docker daemon error.\nPlease install docker, docker-compose or start the daemon to continue...[/bold cyan]")
	# 		sys.exit(0)

	def check_running_containers(self):
		print('[bold cyan]Checking running containers...[/bold cyan]')
		try:
			containers = self.client.containers.list()
			for c in containers:
				if self.dirname in c.name:
					self.question_containers()
					break
		except Exception as e:
			print(f"Unable to check if [bold cyan]{self.dirname}[/bold cyan] containers are running...\n[bold red]Please stop and remove all {self.dirname} containers to continue[/bold red]")
			sys.exit(0)

	def question_containers(self):
		answer = self.inquirer.ask_question(f'Do you want to stop running and remove all {self.dirname} related containers/networks/volumes? (Will not delete synced blockchain data.) ')
		if answer == True:
			self.stop_containers()
		else:
			print(f"Current [bold cyan]{self.dirname}[/bold cyan] containers/networks/volumes need to be stopped and removed to continue")
			sys.exit(0)

	def stop_containers(self):
		print(f"Stopping [bold cyan]{self.dirname}[/bold cyan] containers")
		compose_cmd, _ = resolve_compose()
		if compose_cmd is None:
			print("[bold red]Docker Compose not installed[/bold red]")
			sys.exit(1)
		exit_code = subprocess.call(f"{compose_cmd} down", shell=True)
		# Error if exit_code not 0
		if exit_code != 0:
			print("An error occurred while stopping running containers.")
			sys.exit(exit_code)
		self.prune_containers()
		self.prune_networks()
		self.prune_volumes()
		self.prune_images()

	def prune_containers(self):
		# answer = self.inquirer.ask_question('Prune/remove all stopped docker containers? (Y/n)')
		# if answer == True:
		print('[bold cyan]Pruning all stopped docker containers...[/bold cyan]')
		self.client.containers.prune()

	def prune_networks(self):
		# answer = self.inquirer.ask_question('Prune/remove all unused docker networks?  (Y/n)')
		# if answer == True:
		print('[bold cyan]Pruning all docker networks...[/bold cyan]')
		self.client.networks.prune()

	def prune_volumes(self):
		# answer = self.inquirer.ask_question('Prune/remove all unused docker volumes (will not delete synced blockchain data)?  (Y/n)')
		# if answer == True:
		print('[bold cyan]Pruning all docker volumes...[/bold cyan]')
		self.client.volumes.prune()

	def prune_images(self):
		# answer = self.inquirer.ask_question('Prune/remove all unused docker images?  (Y/n)')
		# if answer == True:
		print('[bold cyan]Pruning all docker images...[/bold cyan]')
		self.client.images.prune()

	def externalIP(self):
		try:
			req = requests.get('http://checkip.amazonaws.com')
			req = req.text
			return req[:-1]
		except Exception as e:
			print(e)
			try:
				req = requests.get('http://ifconfig.co')
				req = req.text.split('code')[3].split('>')[1].split('<')[0]
				return req
			except Exception as e:
				print(e)
				return None

	def check_env_var(self, env_var, detail, q1):
		if self.config[env_var]=='':
			if env_var == 'PUBLIC_IP':
				print("Fetching [bold cyan]public IP Address[/bold cyan]...")
				IP = self.externalIP()
				if IP:
					print(f'Found [bold yellow]{env_var}[/bold yellow]=[bold cyan]{IP}[/bold cyan]')
					answer = self.inquirer.get_input(f'Press Enter to confirm {detail} or type a new one:')
					if answer == '':
						answer = IP
					dotenv.set_key(self.envfile, env_var, answer)
					try:
						if answer != socket.gethostbyname(answer):
							print(f'{socket.gethostbyname(answer)} - [bold cyan]{answer}[/bold cyan]')
					except:
						pass
					print(f'Saved [bold yellow]{env_var}[/bold yellow]=[bold cyan]{str(answer)}[/bold cyan]')
				else:
					answer = self.inquirer.get_input(q1)
					dotenv.set_key(self.envfile, env_var, answer)
					try:
						if answer != socket.gethostbyname(answer):
							print(f'{socket.gethostbyname(answer)} - [bold cyan]{answer}[/bold cyan]')
					except:
						pass
					print(f'Saved [bold yellow]{env_var}[/bold yellow]=[bold cyan]{str(answer)}[/bold cyan]')
			else:
				answer = self.inquirer.get_input(q1)
				dotenv.set_key(self.envfile, env_var, answer)
				print(f'Saved [bold yellow]{env_var}[/bold yellow]=[bold cyan]{str(answer)}[/bold cyan]')
		else:
			print(f'Found [bold yellow]{env_var}[/bold yellow]=[bold cyan]{self.config[env_var]}[/bold cyan]')
			answer = self.inquirer.get_input(f'Press Enter to confirm {detail} or type a new one:')	
			if answer == '':
				answer = self.config[env_var]
			dotenv.set_key(self.envfile, env_var, answer)
			print(f'Saved [bold yellow]{env_var}[/bold yellow]=[bold cyan]{str(answer)}[/bold cyan]')

	def env_vars(self):
		env_vars_data = [
		{
		'name': 'PUBLIC_IP',
		'detail': 'external IP address',
		'question': 'What\'s your external/public IP address?',
		},
		{
		'name': 'SN_NAME',
		'detail': 'Servicenode Name',
		'question': 'Set your Servicenode Name:',
		},
		{
		'name': 'SN_KEY',
		'detail': 'Servicenode Private Key',
		'question': 'Set your Servicenode Private Key:',
		},
		{
		'name': 'SN_ADDRESS',
		'detail': 'Servicenode Address',
		'question': 'Set your Servicenode Address:',
		},
		{
		'name': 'RPC_USER',
		'detail': 'Servicenode Username',
		'question': 'Specify the RPC username to use with your Servicenode:',
		},
		{
		'name': 'RPC_PASSWORD',
		'detail': 'Servicenode Password',
		'question': 'Specify the RPC password to use with your Servicenode:',
		},
		]

		for ev in env_vars_data:
			self.check_env_var(ev['name'],ev['detail'],ev['question'])
	


	def deploy(self):
		proc = subprocess.Popen(['./deploy.sh'], shell=False)
		proc.wait()


