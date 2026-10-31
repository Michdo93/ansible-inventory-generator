# Ansible Inventory Generator

An interactive Bash utility that scans an IPv4 range for SSH services, connects to selected hosts, collects the hostname and Python interpreter path, and writes an Ansible INI inventory to `hosts.new`.

## Requirements

- Bash 4 or newer, OpenSSH client, `sshpass`, `timeout`, `sed`, and `xargs`.
- Run on Linux or WSL. Targets must provide SSH and a POSIX-compatible shell.
- An account and password that can log in to each target. Root or sudo is not required for fact gathering.
- `hostname` and `python3` or `python` available on each target.

Install `sshpass` with your local package manager (for example, `sudo apt install sshpass` on Debian/Ubuntu). The script uses it to authenticate non-interactively so that the same password can be written to the inventory after a successful login.

## Run

```bash
chmod +x generate-inventory.sh
./generate-inventory.sh
```

The script asks for an optional existing inventory path. It reads group names from this file so you can assign discovered hosts to one of its groups; it does not copy existing hosts into the output. Then enter and confirm the IPv4 range. For each SSH-reachable host, decide whether to add it, enter the SSH username and password, and choose an existing group, a new group, or no group.

Yes accepts `y`, `Y`, `yes`, or `Yes`; No accepts `n`, `N`, `no`, or `No`. Press Enter to use the displayed default. SSH login is retried up to three times per selected host.

The output rows have this form:

```ini
[proxmox_bs]
pbs ansible_host=192.168.0.8 ansible_user=root ansible_password='example' ansible_become_password='example' ansible_python_interpreter=/usr/bin/python3
```

The inventory alias is the remotely detected hostname. Hosts assigned no group are written before all group sections, so they remain ungrouped in Ansible's INI inventory format. Set a different output location with `OUTPUT_FILE=/path/to/hosts.new ./generate-inventory.sh`.

## Security

Ansible INI inventory variables are written as plaintext. The script sets `hosts.new` to owner-only permissions (`0600`) where supported, but that does not encrypt the passwords. Do not commit or share this file. Prefer Ansible Vault for real inventories; after generation, move the credentials into encrypted vars or encrypt the inventory with `ansible-vault encrypt hosts.new` and enter the Vault password when prompted.

`sshpass` is used because the script must retain the entered password for the inventory. Its password is supplied through an environment variable rather than a command-line argument, but privileged local process inspection may still expose it briefly. SSH host key verification is enabled; verify fingerprints when prompted on the first connection.

## Limitations

- IPv4 only, up to 65,536 addresses per scan.
- SSH port 22 only.
- Windows targets are not supported by the current fact-gathering command.
- Existing inventory is used only to list group names. `hosts.new` contains newly discovered entries, not a copy of the source inventory.
- Duplicate hostnames are not automatically renamed; review the generated inventory for collisions before using it.
