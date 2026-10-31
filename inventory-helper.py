#!/usr/bin/env python3
"""Small stdlib-only parser/updater for this tool's Ansible INI inventory."""
import base64
import json
import os
import shlex
import sys
from pathlib import Path


def parse(path):
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    section = ""
    entries = []
    groups = []
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("[") and "]" in stripped:
            name = stripped[1:stripped.index("]")]
            section = name.split(":", 1)[0]
            if ":" not in name and name not in groups:
                groups.append(name)
            continue
        if not stripped or stripped.startswith(("#", ";")):
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
        entries.append({"line": index, "alias": alias, "group": section,
                        "ip": variables.get("ansible_host", ""),
                        "user": variables.get("ansible_user", ""),
                        "password": variables.get("ansible_password", ""),
                        "port": variables.get("ansible_port", "22"),
                        "python": variables.get("ansible_python_interpreter", "/usr/bin/python3")})
    return lines, groups, entries


def b64(value):
    return base64.b64encode(value.encode()).decode()


def shell_quote(value):
    # POSIX-shell single-quoted value, accepted by shlex and Ansible INI parsing.
    return "'" + value.replace("'", "'\\''") + "'"


def main():
    action, path = sys.argv[1], sys.argv[2]
    if action == "groups":
        _, groups, _ = parse(path)
        for group in groups:
            print(group)
        return
    if action == "lookup":
        ip = sys.argv[3]
        _, _, entries = parse(path)
        matches = [e for e in entries if e["ip"] == ip]
        if matches:
            e = matches[0]
            print("|".join(b64(e[k]) for k in ("alias", "user", "password", "port", "group")))
        return
    if action == "lookup-port":
        ip = sys.argv[3]
        _, _, entries = parse(path)
        matches = [e for e in entries if e["ip"] == ip]
        if matches:
            print(matches[0]["port"])
        return
    if action == "update":
        payload = json.load(sys.stdin)
        lines, groups, entries = parse(path)
        matches = [e for e in entries if e["ip"] == payload["ip"]]
        old_group = payload.get("group", "")
        insert_at = None
        if matches:
            insert_at = matches[0]["line"]
            # Remove every stale duplicate for this address.
            remove = {e["line"] for e in matches}
            old_group = old_group or matches[0]["group"]
            lines = [line for i, line in enumerate(lines) if i not in remove]
            insert_at = min(insert_at, len(lines))

        values = [
            payload["alias"],
            f"ansible_host={payload['ip']}",
            f"ansible_user={payload['user']}",
            f"ansible_password={shell_quote(payload['password'])}",
            f"ansible_become_password={shell_quote(payload['password'])}",
            f"ansible_port={payload['port']}",
            f"ansible_python_interpreter={payload['python']}",
        ]
        host_line = " ".join(values)
        group = payload.get("group", "")
        if not group:
            # Ungrouped inventory hosts must precede group sections.
            first_section = next((i for i, line in enumerate(lines) if line.strip().startswith("[") and "]" in line), len(lines))
            lines.insert(first_section, host_line)
        else:
            section_re = f"[{group}]"
            starts = [i for i, line in enumerate(lines) if line.strip() == section_re]
            if starts:
                start = starts[0]
                end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[") and "]" in lines[i]), len(lines))
                lines.insert(end, host_line)
            else:
                if lines and lines[-1].strip():
                    lines.append("")
                lines.extend([section_re, host_line])
        destination = Path(path)
        temp = destination.with_name(destination.name + f".tmp.{os.getpid()}")
        temp.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        os.chmod(temp, 0o600)
        temp.replace(destination)
        os.chmod(destination, 0o600)
        return
    raise SystemExit("Unknown action")


if __name__ == "__main__":
    main()
