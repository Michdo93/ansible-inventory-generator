#!/usr/bin/env python3
"""Stdlib-only helper for generate-inventory.sh.

Maintains an Ansible INI inventory *without* secrets plus per-host variables:

    <dir>/hosts.ini                      hosts, ports, users, Python interpreter
    <dir>/host_vars/<alias>/vars.yml     connection/become settings (plaintext, no secrets)
    <dir>/host_vars/<alias>/vault.yml    secrets, encrypted with ansible-vault

Secrets are passed on stdin (JSON) and reach ansible-vault through a pipe; they are
never written unencrypted to disk or placed on a command line.
"""
import base64
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

SECRET_KEYS = ("ansible_password", "ansible_ssh_pass", "ansible_become_password",
               "ansible_become_pass", "ansible_sudo_pass")
MANAGED_KEYS = ("ansible_host", "ansible_user", "ansible_port", "ansible_python_interpreter") + SECRET_KEYS
BEGIN = "# BEGIN ansible-inventory-generator (managed block, do not edit)"
END = "# END ansible-inventory-generator"
IP_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")


def parse(path):
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    section = ""
    entries, groups = [], []
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and "]" in stripped:
            name = stripped[1:stripped.index("]")]
            section = name if ":" not in name else ":" + name  # ":"-prefixed = vars/children
            if ":" not in name and name not in groups:
                groups.append(name)
            continue
        if not stripped or stripped.startswith(("#", ";")) or section.startswith(":"):
            continue
        try:
            tokens = shlex.split(raw, comments=True, posix=True)
        except ValueError:
            continue
        if not tokens:
            continue
        alias = tokens[0]
        variables = {}
        for token in tokens[1:]:
            if "=" in token:
                key, value = token.split("=", 1)
                variables[key] = value
        ip = variables.get("ansible_host", alias if IP_RE.match(alias) else "")
        entries.append({"line": index, "alias": alias, "group": section, "ip": ip,
                        "user": variables.get("ansible_user", ""),
                        "port": variables.get("ansible_port", "22"),
                        "python": variables.get("ansible_python_interpreter", ""),
                        "vars": variables})
    return lines, groups, entries


def b64(value):
    return base64.b64encode(value.encode()).decode()


def ini_value(value):
    if value and re.fullmatch(r"[A-Za-z0-9_./:@%+-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def write_atomic(path, text, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(path.name + f".tmp.{os.getpid()}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(temp, mode)
    temp.replace(path)


def yaml_str(value):
    return json.dumps(value, ensure_ascii=False)  # JSON strings are valid YAML scalars


# ----------------------------------------------------------------- vault ---
def vault_read(path, password_file):
    if not Path(path).exists():
        return ""
    result = subprocess.run(["ansible-vault", "decrypt", "--vault-password-file", password_file,
                             "--output", "-", str(path)], capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"Cannot decrypt {path} with the configured Vault password: {result.stderr.strip()}")
    return result.stdout


def vault_write(path, password_file, text):
    Path(path).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = Path(str(path) + f".tmp.{os.getpid()}")
    result = subprocess.run(["ansible-vault", "encrypt", "--vault-password-file", password_file,
                             "--output", str(temp), "-"], input=text, capture_output=True, text=True)
    if result.returncode != 0:
        temp.unlink(missing_ok=True)
        raise SystemExit(f"ansible-vault encrypt failed: {result.stderr.strip()}")
    os.chmod(temp, 0o600)
    temp.replace(path)


def vault_set(path, password_file, values):
    text = vault_read(path, password_file)
    lines = [line for line in text.splitlines() if line.strip() != "---"]
    for key, value in values.items():
        new = f"{key}: {yaml_str(value)}"
        pattern = re.compile(rf"^{re.escape(key)}\s*:")
        for i, line in enumerate(lines):
            if pattern.match(line):
                lines[i] = new
                break
        else:
            lines.append(new)
    vault_write(path, password_file, "---\n" + "\n".join(lines).strip() + "\n")


def vars_set(path, managed_lines):
    """Replace the managed block in vars.yml, keeping anything the user added."""
    path = Path(path)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else ["---"]
    out, skipping = [], False
    for line in existing:
        if line.strip() == BEGIN:
            skipping = True
            continue
        if line.strip() == END:
            skipping = False
            continue
        if not skipping:
            out.append(line)
    while out and not out[-1].strip():
        out.pop()
    if not out:
        out = ["---"]
    out += [BEGIN] + managed_lines + [END]
    write_atomic(path, "\n".join(out) + "\n")


def host_vars_dir(inventory, alias):
    return Path(inventory).resolve().parent / "host_vars" / alias


# --------------------------------------------------------------- actions ---
def action_update(inventory):
    """stdin JSON: alias, ip, user, port, python, group. Writes a secret-free host line."""
    payload = json.load(sys.stdin)
    lines, _, entries = parse(inventory)
    # Only lines that define this address; bare "alias" lines (extra group memberships) stay.
    matches = [e for e in entries if e["ip"] == payload["ip"]]
    extra = {}
    for e in matches:  # keep user-added variables of the old line
        extra.update({k: v for k, v in e["vars"].items() if k not in MANAGED_KEYS})
    if matches:
        remove = {e["line"] for e in matches}
        lines = [line for i, line in enumerate(lines) if i not in remove]
    values = [payload["alias"], f"ansible_host={payload['ip']}", f"ansible_port={payload['port']}",
              f"ansible_user={ini_value(payload['user'])}"]
    if payload.get("python"):
        values.append(f"ansible_python_interpreter={ini_value(payload['python'])}")
    values += [f"{k}={ini_value(v)}" for k, v in extra.items()]
    host_line = " ".join(values)
    group = payload.get("group", "")
    if not group:
        first = next((i for i, l in enumerate(lines) if l.strip().startswith("[") and "]" in l), len(lines))
        while first > 0 and not lines[first - 1].strip():
            first -= 1
        lines.insert(first, host_line)
    else:
        header = f"[{group}]"
        starts = [i for i, l in enumerate(lines) if l.strip() == header]
        if starts:
            end = next((i for i in range(starts[0] + 1, len(lines))
                        if lines[i].strip().startswith("[") and "]" in lines[i]), len(lines))
            while end > starts[0] + 1 and not lines[end - 1].strip():
                end -= 1
            lines.insert(end, host_line)
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines += [header, host_line]
    write_atomic(inventory, "\n".join(lines).rstrip() + "\n")


def action_hostvars(inventory, password_file):
    """stdin JSON: alias, family (linux|windows), user, become_password."""
    payload = json.load(sys.stdin)
    directory = host_vars_dir(inventory, payload["alias"])
    if payload["family"] == "windows":
        managed = ["ansible_connection: ssh", "ansible_shell_type: powershell",
                   "ansible_become_method: runas", f"ansible_become_user: {yaml_str(payload['user'])}"]
    else:
        managed = ["ansible_become_method: sudo"]
    managed.append('ansible_become_password: "{{ vault_ansible_become_password }}"')
    vault_set(directory / "vault.yml", password_file,
              {"vault_ansible_become_password": payload["become_password"]})
    vars_set(directory / "vars.yml", managed)
    os.chmod(directory, 0o700)


def action_migrate(inventory, password_file):
    """Move plaintext passwords from host lines into per-host vaults."""
    lines, _, entries = parse(inventory)
    migrated = []
    for e in entries:
        secrets = {k: v for k, v in e["vars"].items() if k in SECRET_KEYS}
        if not secrets:
            continue
        directory = host_vars_dir(inventory, e["alias"])
        vault, managed = {}, []
        ssh_pw = secrets.get("ansible_password") or secrets.get("ansible_ssh_pass")
        become_pw = (secrets.get("ansible_become_password") or secrets.get("ansible_become_pass")
                     or secrets.get("ansible_sudo_pass"))
        if ssh_pw:
            vault["vault_ansible_password"] = ssh_pw
            managed.append('ansible_password: "{{ vault_ansible_password }}"')
        if become_pw:
            vault["vault_ansible_become_password"] = become_pw
            managed.append('ansible_become_password: "{{ vault_ansible_become_password }}"')
        vault_set(directory / "vault.yml", password_file, vault)
        vars_set(directory / "vars.yml", managed)
        tokens = shlex.split(lines[e["line"]], comments=True, posix=True)
        kept = [tokens[0]] + [f"{k}={ini_value(v)}" for k, v in
                              (t.split("=", 1) for t in tokens[1:] if "=" in t) if k not in SECRET_KEYS]
        lines[e["line"]] = " ".join(kept)
        migrated.append(e["alias"])
    if migrated:
        write_atomic(inventory, "\n".join(lines).rstrip() + "\n")
    for alias in migrated:
        print(alias)


def action_alias(inventory, ip, hostname):
    """Unique, Ansible-safe alias for hostname (suffix with IP on collision)."""
    alias = re.sub(r"[^A-Za-z0-9_.-]", "-", hostname).strip("-.") or "host-" + ip.replace(".", "-")
    _, _, entries = parse(inventory)
    if any(e["alias"] == alias and e["ip"] != ip for e in entries):
        alias = f"{alias}-{ip.replace('.', '-')}"
    print(alias)


def main():
    action, inventory = sys.argv[1], sys.argv[2]
    if action == "groups":
        for group in parse(inventory)[1]:
            print(group)
    elif action == "lookup":
        matches = [e for e in parse(inventory)[2] if e["ip"] == sys.argv[3]]
        if matches:
            e = matches[0]
            print("|".join(b64(e[k]) for k in ("alias", "user", "port", "group")))
    elif action == "update":
        action_update(inventory)
    elif action == "hostvars":
        action_hostvars(inventory, sys.argv[3])
    elif action == "migrate":
        action_migrate(inventory, sys.argv[3])
    elif action == "alias":
        action_alias(inventory, sys.argv[3], sys.argv[4])
    elif action == "plaintext-secrets":
        found = [e["alias"] for e in parse(inventory)[2] if any(k in e["vars"] for k in SECRET_KEYS)]
        print("\n".join(found))
        raise SystemExit(1 if found else 0)
    else:
        raise SystemExit("Unknown action")


if __name__ == "__main__":
    main()
