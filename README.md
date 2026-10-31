# Ansible Inventory Generator

Single entry point for onboarding hosts to Ansible. Run it **on the Ansible control node**. It scans an IPv4 range for SSH, makes sure every host has a dedicated Ansible account that accepts the control node's SSH key (bootstrapping the host through [ssh-ansible-bootstrap-manager](https://github.com/Michdo93/ssh-ansible-bootstrap-manager) when needed) and writes an inventory **without plaintext passwords**: the password used for privilege escalation is stored per host in Ansible Vault.

```text
inventory/
├── ansible.cfg                  inventory, private key, vault password file
├── hosts.ini                    hosts, ports, users, Python interpreter - no secrets
└── host_vars/<host>/
    ├── vars.yml                 become method, connection settings (plaintext)
    └── vault.yml                vault_ansible_become_password (ansible-vault encrypted)
```

| Goal | Mechanism |
| --- | --- |
| No SSH passwords | SSH key pair of the control node; the public key is installed for the Ansible account |
| No sudo/runas password in clear text | `ansible_become_password: "{{ vault_ansible_become_password }}"` + encrypted `vault.yml` |
| No manual fingerprint confirmation | `StrictHostKeyChecking=accept-new` (first contact = "yes"); a *changed* key is reported and only replaced after you confirm |

## Requirements

On the control node: Bash 4+, OpenSSH client, Python 3, `ansible-core` (`ansible-vault`, `ansible`), `git` (to fetch the bootstrap manager if it is not checked out next to this repository) and either `sshpass` or OpenSSH ≥ 8.4 for the one-time administrator password login.

```bash
sudo apt install openssh-client python3 ansible-core git sshpass
```

Recommended layout (the generator finds the manager and the provider scripts automatically and uses these reviewed local copies instead of downloading them):

```bash
mkdir ansible-tools && cd ansible-tools
for r in ansible-inventory-generator ssh-ansible-bootstrap-manager ansible-linux-bootstrap ansible-kali-bootstrap ansible-windows-bootstrap; do
  git clone https://github.com/Michdo93/$r
done
```

## Run

```bash
chmod +x ansible-inventory-generator/generate-inventory.sh
./ansible-inventory-generator/generate-inventory.sh
```

1. **SSH key**: uses `~/.ssh/id_ed25519` (or `id_ecdsa`/`id_rsa`); generates an ed25519 key without passphrase if none exists. Override with `BM_KEY_FILE=/path/key`.
2. **Vault password**: uses `~/.ansible/vault_pass.txt` if it exists. Otherwise you choose to store one there (mode 0600; leave the prompt empty to generate a random one, and back the file up) or to keep it only in your head and use `--ask-vault-pass` later.
3. **Inventory**: updates `./inventory/hosts.ini` (`OUTPUT_DIR=...` to change). A new inventory can start from an existing INI file such as `/etc/ansible/hosts`. Plaintext `ansible_password`/`ansible_become_password` values found in it are moved into per-host vaults.
4. **Account**: name of the dedicated account (e.g. `ansible`, `sysadmin` - the name you enter is used everywhere, including the sudo rule) and its password, plus the Linux sudo mode (see below).
5. **Scan**: IPv4 range and SSH port(s).
6. **Per host**: if key login as the account works, facts are collected immediately. Otherwise the host is bootstrapped: you enter an administrator login once (password, or empty for key/agent; for Linux a sudo password is asked if needed). The bootstrap creates the account, sets its password, installs the public key and configures sudo (Linux) or Administrators membership (Windows). Then the become password is verified on the host, the alias (remote hostname, made unique if needed) and group are chosen and the files are written.
7. **Verification** (optional): runs `ansible <host> -b -m command -a 'id -u'` for each saved Linux host (expects `0`) and `raw whoami` for Windows.

Afterwards:

```bash
cd inventory
ansible all -m ping
ansible lab -b -m ansible.builtin.apt -a 'update_cache=yes upgrade=dist'
```

`ansible.cfg` points to `hosts.ini`, the private key and - if stored - the Vault password file, so no password prompt is needed. Without a stored Vault password add `--ask-vault-pass`.

## Sudo mode on Linux

| Mode | sudoers rule (for account `ansible`) | Secret Ansible needs |
| --- | --- | --- |
| `vault` (default) | `ansible ALL=(ALL:ALL) ALL` | account password from Ansible Vault |
| `nopasswd` | `ansible ALL=(ALL:ALL) NOPASSWD: ALL` | none (the vault entry is still written) |

Vault mode means that a stolen SSH key alone is not enough to become root. Windows always uses Vault: `ansible_become_method: runas` with the account password, which is required for operations such as Windows Update over SSH.

## Generated host variables

Linux (`host_vars/<host>/vars.yml`):

```yaml
ansible_become_method: sudo
ansible_become_password: "{{ vault_ansible_become_password }}"
```

Windows:

```yaml
ansible_connection: ssh
ansible_shell_type: powershell
ansible_become_method: runas
ansible_become_user: "ansible"
ansible_become_password: "{{ vault_ansible_become_password }}"
```

Only the block between the `BEGIN`/`END ansible-inventory-generator` markers is managed; your own additions in `vars.yml` are kept. Inspect or edit a vault with `ansible-vault view|edit host_vars/<host>/vault.yml`. Change the Vault password for all files with `ansible-vault rekey host_vars/*/vault.yml`.

## Notes

- Re-running the generator updates existing entries (matched by IP) and keeps user-added host variables; existing vaults are decrypted, updated and re-encrypted.
- Entries in other groups that only reference an alias (extra group memberships) are left untouched.
- IPv4 only, up to 65,536 addresses per scan; INI inventories only.
- Windows hosts use the Windows OpenSSH server. For `ansible.windows` modules install the collection: `ansible-galaxy collection install ansible.windows`.
