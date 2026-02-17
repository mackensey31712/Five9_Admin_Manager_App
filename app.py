import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

import pandas as pd
import streamlit as st


st.set_page_config(page_title="Five9 Campaign Manager", layout="wide")


_INSTALL_DIR = Path(tempfile.gettempdir()) / "five9_installer"
_INSTALL_DIR.mkdir(exist_ok=True)
_INSTALL_STDOUT = _INSTALL_DIR / "stdout.txt"
_INSTALL_STDERR = _INSTALL_DIR / "stderr.txt"
_INSTALL_LOCK = _INSTALL_DIR / "running.lock"


def ps_base_args(command: str) -> List[str]:
	return [
		"powershell.exe",
		"-NoLogo",
		"-NoProfile",
		"-NonInteractive",
		"-ExecutionPolicy",
		"Bypass",
		"-WindowStyle",
		"Hidden",
		"-Command",
		command,
	]


def get_creation_flags() -> int:
	return subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0


def ps_escape(value: str) -> str:
	return (value or "").replace("'", "''")


def run_powershell_raw(command: str) -> Tuple[str, str]:
	completed = subprocess.run(
		ps_base_args(command),
		capture_output=True,
		text=True,
		creationflags=get_creation_flags(),
	)

	return completed.stdout.strip(), completed.stderr.strip()


def start_install_detached(command: str) -> None:
	"""Launch installer as a fully detached PowerShell process via Start-Process.
	Python returns immediately. Output is written to temp files."""
	for f in [_INSTALL_STDOUT, _INSTALL_STDERR]:
		f.write_text("", encoding="utf-8")
	_INSTALL_LOCK.write_text("running", encoding="utf-8")

	wrapper_script = (
		f"try {{ {command} | Out-File -FilePath '{_INSTALL_STDOUT}' -Encoding utf8 }} "
		f"catch {{ $_.Exception.Message | Out-File -FilePath '{_INSTALL_STDERR}' -Encoding utf8 }}\n"
		f"finally {{ Remove-Item -Path '{_INSTALL_LOCK}' -Force -ErrorAction SilentlyContinue }}"
	)

	script_file = _INSTALL_DIR / "install_script.ps1"
	script_file.write_text(wrapper_script, encoding="utf-8")

	launch_cmd = (
		f"Start-Process powershell.exe "
		f"-ArgumentList '-NoLogo','-NoProfile','-NonInteractive',"
		f"'-ExecutionPolicy','Bypass','-File','{script_file}' "
		f"-WindowStyle Hidden"
	)

	subprocess.run(
		ps_base_args(launch_cmd),
		capture_output=True,
		text=True,
		creationflags=get_creation_flags(),
	)


def get_install_status() -> Dict[str, object]:
	"""Check temp files for installer status."""
	running = _INSTALL_LOCK.exists()
	stdout = _INSTALL_STDOUT.read_text(encoding="utf-8").strip() if _INSTALL_STDOUT.exists() else ""
	stderr = _INSTALL_STDERR.read_text(encoding="utf-8").strip() if _INSTALL_STDERR.exists() else ""
	done = not running and (_INSTALL_STDOUT.exists() or _INSTALL_STDERR.exists())
	return {"running": running, "stdout": stdout, "stderr": stderr, "done": done}


def run_powershell_command(username: str, password: str, command: str) -> Tuple[str, str]:
	safe_user = ps_escape(username)
	safe_pwd = ps_escape(password)
	ps_script = f"""
$ErrorActionPreference = 'Stop'
$secpasswd = ConvertTo-SecureString '{safe_pwd}' -AsPlainText -Force
$creds = New-Object System.Management.Automation.PSCredential ('{safe_user}', $secpasswd)
Connect-Five9AdminWebService -Credential $creds
{command}
"""

	completed = subprocess.run(
		ps_base_args(ps_script),
		capture_output=True,
		text=True,
		creationflags=get_creation_flags(),
	)

	return completed.stdout.strip(), completed.stderr.strip()


STATE_MAP = {0: "NotRunning", 1: "Starting", 2: "Running", 3: "Stopping"}
TYPE_MAP = {0: "Inbound", 1: "Outbound", 2: "AutoDial"}


def parse_campaigns_json(raw_json: str) -> pd.DataFrame:
	if not raw_json:
		return pd.DataFrame(columns=["Name", "State", "Type"])

	try:
		parsed = json.loads(raw_json)
	except json.JSONDecodeError:
		return pd.DataFrame(columns=["Name", "State", "Type"])

	if isinstance(parsed, dict):
		records = [parsed]
	elif isinstance(parsed, list):
		records = parsed
	else:
		records = []

	normalized = []
	for rec in records:
		lower_rec = {k.lower(): v for k, v in rec.items()}
		name = lower_rec.get("name", "")
		raw_state = lower_rec.get("state", "")
		raw_type = lower_rec.get("type", "")

		if isinstance(raw_state, int):
			state = STATE_MAP.get(raw_state, str(raw_state))
		else:
			state = str(raw_state)

		if isinstance(raw_type, int):
			ctype = TYPE_MAP.get(raw_type, str(raw_type))
		else:
			ctype = str(raw_type)

		normalized.append({"Name": name, "State": state, "Type": ctype})

	df = pd.DataFrame.from_records(normalized)
	if df.empty:
		return pd.DataFrame(columns=["Name", "State", "Type"])

	return df


def get_default_state() -> Dict[str, object]:
	return {
		"campaigns_df": pd.DataFrame(columns=["Name", "State", "Type"]),
		"last_stdout": "",
		"last_stderr": "",
		"cached_user": "",
		"cached_pass": "",

	}


for key, value in get_default_state().items():
	if key not in st.session_state:
		st.session_state[key] = value


def get_effective_credentials(username: str, password: str, use_cached: bool) -> Tuple[str, str]:
	if username and password:
		return username, password
	if use_cached and st.session_state.cached_user and st.session_state.cached_pass:
		return st.session_state.cached_user, st.session_state.cached_pass
	return username, password


def parse_action_results(raw_json: str) -> Tuple[List[str], Dict[str, str]]:
	if not raw_json:
		return [], {}

	try:
		parsed = json.loads(raw_json)
	except json.JSONDecodeError:
		return [], {"(unknown)": "Failed to parse PowerShell result."}

	if isinstance(parsed, dict):
		records = [parsed]
	elif isinstance(parsed, list):
		records = parsed
	else:
		records = []

	successes: List[str] = []
	failures: Dict[str, str] = {}
	for record in records:
		name = str(record.get("Name", "(unknown)"))
		if record.get("Success"):
			successes.append(name)
		else:
			failures[name] = str(record.get("Error") or "Unknown error")

	return successes, failures


with st.sidebar:
	st.header("Five9 Connection")
	username = st.text_input("Five9 Username")
	password = st.text_input("Five9 Password", type="password")
	remember_creds = st.checkbox("Remember credentials for this session")
	use_cached = st.checkbox("Use cached credentials", value=True)

	if remember_creds and username and password:
		st.session_state.cached_user = username
		st.session_state.cached_pass = password
	if st.session_state.cached_user and st.session_state.cached_pass:
		if st.button("Clear cached credentials"):
			st.session_state.cached_user = ""
			st.session_state.cached_pass = ""
			st.info("Cached credentials cleared.")

	if st.button("Install/Update Five9 Module"):
		install_command = (
			"irm 'https://raw.githubusercontent.com/Five9DeveloperProgram/PSFive9Admin/"
			"main/installer.ps1' | iex"
		)
		pre_status = get_install_status()
		if pre_status["running"]:
			st.warning("Install already in progress.")
		else:
			start_install_detached(install_command)
			st.info("Install started in background. Click 'Check Installer Status' to view results.")

	install_status = get_install_status()

	if install_status["running"] or install_status["done"]:
		col1, col2 = st.columns(2)
		with col1:
			if st.button("Check Installer Status"):
				status = get_install_status()
				if status["running"]:
					st.info("Install still running...")
				else:
					st.session_state.last_stdout = status["stdout"]
					st.session_state.last_stderr = status["stderr"]
					if status["stderr"]:
						st.error("Module install failed. Check Debug Console.")
					else:
						st.success("Module installed/updated. Check Debug Console for details.")
		with col2:
			if st.button("Clear Install Status"):
				for f in [_INSTALL_LOCK, _INSTALL_STDOUT, _INSTALL_STDERR]:
					if f.exists():
						f.unlink()
				st.info("Install status cleared.")

	if st.button("Get Campaign Status"):
		eff_user, eff_pass = get_effective_credentials(username, password, use_cached)
		if not eff_user or not eff_pass:
			st.error("Enter username and password before fetching campaigns.")
		else:
			fetch_command = (
				"$types = @('Inbound','Outbound','AutoDial')\n"
				"$all = @()\n"
				"foreach ($t in $types) {\n"
				"  try { $all += Get-Five9Campaign -Type $t } catch {}\n"
				"}\n"
				"$all | ForEach-Object {\n"
				"  [pscustomobject]@{\n"
				"    Name = $_.name\n"
				"    State = $_.state.ToString()\n"
				"    Type = $_.type.ToString()\n"
				"  }\n"
				"} | ConvertTo-Json"
			)
			stdout, stderr = run_powershell_command(eff_user, eff_pass, fetch_command)
			st.session_state.last_stdout = stdout
			st.session_state.last_stderr = stderr
			if stderr:
				st.error("Failed to fetch campaigns. Check Debug Console.")
			else:
				st.session_state.campaigns_df = parse_campaigns_json(stdout)
				if st.session_state.campaigns_df.empty:
					st.warning("No campaigns returned.")


st.title("Five9 Campaign Manager")

campaigns_df = st.session_state.campaigns_df

if campaigns_df.empty:
	st.info("No campaign data loaded. Use 'Get Campaign Status' to load campaigns.")

left, right = st.columns([2, 1])

with left:
	st.subheader("Control Panel")
	status_choice = st.radio(
		"Campaign State",
		["Running", "Otherwise (Stopped/Stopping)"],
		horizontal=True,
	)

	if not campaigns_df.empty:
		running_mask = campaigns_df["State"].str.lower() == "running"
		if status_choice == "Running":
			filtered_df = campaigns_df[running_mask]
			action_label = "Stop Selected Campaigns"
			action_color = "#d33"
		else:
			filtered_df = campaigns_df[~running_mask]
			action_label = "Start Selected Campaigns"
			action_color = "#1f8b4c"
	else:
		filtered_df = pd.DataFrame(columns=["Name", "State", "Type"])
		action_label = "Stop Selected Campaigns"
		action_color = "#d33"

	campaign_options = filtered_df["Name"].tolist() if not filtered_df.empty else []
	selected_campaigns = st.multiselect("Select Campaigns", campaign_options)
	st.metric("Selected Campaigns", len(selected_campaigns))

	if filtered_df.empty:
		st.info("No campaigns available for this filter.")
	else:
		csv_data = filtered_df.to_csv(index=False)
		st.download_button(
			"Download Filtered CSV",
			csv_data,
			file_name="five9_campaigns_filtered.csv",
			mime="text/csv",
		)

with right:
	st.subheader("Action")
	confirm_change = st.checkbox("I confirm I want to change campaign states")
	auto_refresh = st.checkbox("Auto-refresh after action", value=True)
	eff_user, eff_pass = get_effective_credentials(username, password, use_cached)
	action_enabled = confirm_change and bool(selected_campaigns) and eff_user and eff_pass

	button_style = f"""
		<style>
		div.stButton > button:first-child {{
			background-color: {action_color};
			color: white;
			border: none;
		}}
		</style>
	"""
	st.markdown(button_style, unsafe_allow_html=True)
	if st.button(action_label, disabled=not action_enabled):
		if not eff_user or not eff_pass:
			st.error("Enter username and password before running actions.")
		elif not selected_campaigns:
			st.warning("Select at least one campaign.")
		else:
			action_command = "Stop-Five9Campaign -Force $true" if status_choice == "Running" else "Start-Five9Campaign"
			progress = st.progress(0)

			safe_campaigns_json = ps_escape(json.dumps(selected_campaigns))
			ps_command = f"""
$campaigns = ConvertFrom-Json '{safe_campaigns_json}'
$results = @()
foreach ($campaign in $campaigns) {{
	try {{
		{action_command} -Name $campaign
		$results += [pscustomobject]@{{Name = $campaign; Success = $true; Error = $null}}
	}} catch {{
		$results += [pscustomobject]@{{Name = $campaign; Success = $false; Error = $_.Exception.Message}}
	}}
	$results | Out-Null
}}
$results | ConvertTo-Json -Depth 3
"""

			stdout, stderr = run_powershell_command(eff_user, eff_pass, ps_command)
			st.session_state.last_stdout = stdout
			st.session_state.last_stderr = stderr

			progress.progress(1.0)
			if stderr:
				st.error("Action failed. Check Debug Console.")
			else:
				successes, failure_details = parse_action_results(stdout)
				if successes:
					st.success(f"Updated campaigns: {', '.join(successes)}")
				if failure_details:
					error_lines = [f"{name}: {detail}" for name, detail in failure_details.items()]
					st.error("Failed campaigns:\n" + "\n".join(error_lines))

			if auto_refresh and eff_user and eff_pass:
				refresh_command = (
					"$types = @('Inbound','Outbound','AutoDial')\n"
					"$all = @()\n"
					"foreach ($t in $types) {\n"
					"  try { $all += Get-Five9Campaign -Type $t } catch {}\n"
					"}\n"
					"$all | ForEach-Object {\n"
					"  [pscustomobject]@{\n"
					"    Name = $_.name\n"
					"    State = $_.state.ToString()\n"
					"    Type = $_.type.ToString()\n"
					"  }\n"
					"} | ConvertTo-Json"
				)
				refresh_stdout, refresh_stderr = run_powershell_command(eff_user, eff_pass, refresh_command)
				st.session_state.last_stdout = refresh_stdout
				st.session_state.last_stderr = refresh_stderr
				if not refresh_stderr:
					st.session_state.campaigns_df = parse_campaigns_json(refresh_stdout)


st.subheader("Campaigns")
st.dataframe(campaigns_df, use_container_width=True)

with st.expander("Debug Console", expanded=False):
	st.text("Last stdout:")
	st.code(st.session_state.last_stdout or "(empty)", language="text")
	st.text("Last stderr:")
	st.code(st.session_state.last_stderr or "(empty)", language="text")
