#!/usr/bin/env bash
# Discover SSH hosts and write an Ansible INI inventory to hosts.new.
set -uo pipefail

readonly SSH_PORT=22
readonly CONNECT_TIMEOUT=5
readonly OUTPUT_FILE="${OUTPUT_FILE:-hosts.new}"

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
ask_yes_no() {
  local prompt=$1 default=$2 answer
  while true; do
    if [[ $default == y ]]; then read -r -p "$prompt [Y/n] " answer || return 1
    else read -r -p "$prompt [y/N] " answer || return 1; fi
    answer=${answer,,}
    case "$answer" in y|yes) return 0;; n|no) return 1;; '') [[ $default == y ]] && return 0 || return 1;; *) printf 'Please answer y/yes or n/no.\n';; esac
  done
}
ipv4_to_int() {
  local a b c d
  IFS=. read -r a b c d <<< "$1"
  [[ "$a$b$c$d" =~ ^[0-9]+$ ]] || return 1
  ((a<=255 && b<=255 && c<=255 && d<=255)) || return 1
  printf '%u' "$(((a<<24)|(b<<16)|(c<<8)|d))"
}
int_to_ipv4() { local n=$1; printf '%d.%d.%d.%d' "$((n>>24&255))" "$((n>>16&255))" "$((n>>8&255))" "$((n&255))"; }
ini_quote() { local value=$1; printf '%q' "$value"; }
valid_group() { [[ $1 =~ ^[A-Za-z0-9_][A-Za-z0-9_-]*$ ]]; }
valid_user() { [[ $1 =~ ^[A-Za-z0-9_.@+-]+[$]?$ ]]; }

command -v ssh >/dev/null || die 'OpenSSH client (ssh) is required.'
command -v sshpass >/dev/null || die "sshpass is required for password authentication (install it with your package manager)."
command -v timeout >/dev/null || die "The 'timeout' command is required (Linux or WSL)."

printf 'Existing inventory file to read group names from (Enter for none):\n'
read -r -p 'Inventory path: ' source_inventory || exit 0
declare -a groups=()
if [[ -n $source_inventory ]]; then
  [[ -r $source_inventory ]] || die "Cannot read inventory file: $source_inventory"
  while IFS= read -r group; do
    [[ -n $group ]] || continue
    found=false
    for g in "${groups[@]}"; do [[ $g == "$group" ]] && found=true; done
    [[ $found == true ]] || groups+=("$group")
  done < <(sed -nE 's/^[[:space:]]*\[([A-Za-z0-9_-]+):?(children|vars)?\][[:space:]]*(#.*)?$/\1/p' "$source_inventory")
fi

while true; do
  read -r -p 'First IPv4 address: ' first || exit 0
  read -r -p 'Last IPv4 address:  ' last || exit 0
  first_n=$(ipv4_to_int "$first") || { printf 'Invalid first IPv4 address.\n'; continue; }
  last_n=$(ipv4_to_int "$last") || { printf 'Invalid last IPv4 address.\n'; continue; }
  (( first_n <= last_n )) || { printf 'The first address must not be greater than the last address.\n'; continue; }
  (( last_n-first_n <= 65535 )) || { printf 'Ranges are limited to 65,536 addresses.\n'; continue; }
  ask_yes_no "Scan ${first} through ${last}?" y && break
done

printf '\nChecking SSH reachability on TCP port %d...\n' "$SSH_PORT"
declare -a reachable=()
ssh_opts=(-p "$SSH_PORT" -o ConnectTimeout="$CONNECT_TIMEOUT" -o StrictHostKeyChecking=accept-new)
for ((n=first_n; n<=last_n; n++)); do
  ip=$(int_to_ipv4 "$n")
  probe=$(timeout "$CONNECT_TIMEOUT" ssh -F /dev/null "${ssh_opts[@]}" -o BatchMode=yes \
    -o PreferredAuthentications=none "inventory-probe@$ip" true 2>&1 || true)
  if [[ $probe == *'Permission denied'* || $probe == *'no mutual signature'* || $probe == *'Too many authentication failures'* ]]; then reachable+=("$ip"); fi
done
if ((${#reachable[@]}==0)); then printf 'No SSH-reachable devices found. No inventory was written.\n'; exit 0; fi
printf 'Found %d SSH-reachable device(s).\n' "${#reachable[@]}"

declare -a row_hosts=() row_ip=() row_user=() row_pass=() row_python=() row_group=()
for ip in "${reachable[@]}"; do
  printf '\nDevice: %s\n' "$ip"
  ask_yes_no 'Connect and add this device to the inventory?' y || { printf 'Skipped %s.\n' "$ip"; continue; }
  read -r -p 'SSH username: ' ssh_user
  valid_user "$ssh_user" || { printf 'Invalid or empty username; skipped.\n'; continue; }
  read -r -s -p 'SSH password: ' ssh_password; printf '\n'
  [[ -n $ssh_password ]] || { printf 'Password cannot be empty; skipped.\n'; continue; }
  facts=''
  for attempt in 1 2 3; do
    printf 'SSH login attempt %d of 3 for %s.\n' "$attempt" "$ip"
    facts=$(SSHPASS="$ssh_password" timeout 30 sshpass -e ssh -tt -F /dev/null "${ssh_opts[@]}" \
      -o NumberOfPasswordPrompts=1 "$ssh_user@$ip" "printf 'HOSTNAME='; hostname; printf '\\nPYTHON='; command -v python3 || command -v python" 2>&1 || true)
    if [[ $facts == *'HOSTNAME='* && $facts == *'PYTHON='* ]]; then break; fi
    if ((attempt<3)); then printf 'Login or remote information query failed.\n'; else printf 'Could not log in after 3 attempts; skipping %s.\n' "$ip"; fi
  done
  if [[ $facts != *'HOSTNAME='* || $facts != *'PYTHON='* ]]; then unset ssh_password; continue; fi
  hostname=$(printf '%s\n' "$facts" | tr -d '\r' | sed -n 's/^.*HOSTNAME=//p' | head -n1 | xargs)
  python_path=$(printf '%s\n' "$facts" | tr -d '\r' | sed -n 's/^.*PYTHON=//p' | head -n1 | xargs)
  if [[ -z $hostname || -z $python_path || $hostname =~ [[:space:]] || $hostname =~ [\[\]\#] ]]; then
    printf 'Could not obtain a safe hostname or Python interpreter path; skipped.\n'; unset ssh_password; continue
  fi
  printf 'Hostname: %s\nPython interpreter: %s\n' "$hostname" "$python_path"

  group=
  if ((${#groups[@]}>0)) && ask_yes_no 'Add this host to an existing inventory group?' y; then
    printf '\nAvailable groups:\n'
    for i in "${!groups[@]}"; do printf '  %d) %s\n' "$((i+1))" "${groups[$i]}"; done
    read -r -p 'Choose a group number or enter its name: ' choice
    if [[ $choice =~ ^[0-9]+$ ]] && ((choice>=1 && choice<=${#groups[@]})); then group=${groups[$((choice-1))]}
    else
      for g in "${groups[@]}"; do [[ $choice == "$g" ]] && group=$g; done
    fi
    if [[ -z $group ]]; then printf 'No matching existing group; host skipped.\n'; unset ssh_password; continue; fi
  elif ask_yes_no 'Add this host to a new inventory group?' n; then
    read -r -p 'New group name: ' group
    if ! valid_group "$group"; then printf 'Invalid group name; use letters, numbers, underscore, or hyphen.\n'; unset ssh_password; continue; fi
  fi
  row_hosts+=("$hostname"); row_ip+=("$ip"); row_user+=("$ssh_user"); row_pass+=("$ssh_password"); row_python+=("$python_path"); row_group+=("$group")
  unset ssh_password
done

if ((${#row_hosts[@]}==0)); then printf '\nNo hosts were added. No inventory was written.\n'; exit 0; fi
if [[ -e $OUTPUT_FILE ]] && ! ask_yes_no "$OUTPUT_FILE already exists. Replace it?" n; then
  printf 'Kept existing file; no changes written.\n'; exit 0
fi

# Build a temporary file beside the requested output, then set private permissions and
# atomically replace the destination. New hosts are grouped, and ungrouped rows go first.
tmp_file="${OUTPUT_FILE}.tmp.$$"
umask 077
: > "$tmp_file" || die "Cannot create output file: $tmp_file"
chmod 600 "$tmp_file" 2>/dev/null || true
printf '# Generated by Ansible Inventory Generator. Review before use.\n' >> "$tmp_file"

write_row() {
  local i=$1 qpass
  qpass=$(ini_quote "${row_pass[$i]}")
  printf '%s ansible_host=%s ansible_user=%s ansible_password=%s ansible_become_password=%s ansible_python_interpreter=%s\n' \
    "${row_hosts[$i]}" "${row_ip[$i]}" "${row_user[$i]}" "$qpass" "$qpass" "${row_python[$i]}" >> "$tmp_file"
}
for i in "${!row_hosts[@]}"; do [[ -z ${row_group[$i]} ]] && write_row "$i"; done
declare -a output_groups=()
for i in "${!row_hosts[@]}"; do
  g=${row_group[$i]}; [[ -n $g ]] || continue
  found=false; for existing in "${output_groups[@]}"; do [[ $existing == "$g" ]] && found=true; done
  [[ $found == true ]] || output_groups+=("$g")
done
for g in "${output_groups[@]}"; do
  printf '\n[%s]\n' "$g" >> "$tmp_file"
  for i in "${!row_hosts[@]}"; do [[ ${row_group[$i]} == "$g" ]] && write_row "$i"; done
done

mv -f -- "$tmp_file" "$OUTPUT_FILE" || die "Could not replace $OUTPUT_FILE"
chmod 600 "$OUTPUT_FILE" 2>/dev/null || true
printf '\nCreated %s with %d host(s). File permissions are set to owner-only where supported.\n' "$OUTPUT_FILE" "${#row_hosts[@]}"
printf 'Keep this file secret: it contains plaintext SSH and privilege-escalation passwords.\n'
