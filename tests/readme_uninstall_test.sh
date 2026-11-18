#!/usr/bin/env bash
# shellcheck disable=SC2016,SC2088  # $DEST and ~ are text to find, not to expand
# Checks the README has uninstall steps under Reference, that they name the
# paths install.sh and ergosphere.py actually write, and that the FAQ points
# at them.  No install is run: this reads README.md, install.sh and
# ergosphere.py only.
set -u
root="$(dirname "$0")/.."
readme="$root/README.md"
fail=0

# Print the lines under one heading, up to the next heading of any level.
section() {
  awk -v h="$1" '
    /^#+ / { inside = ($0 == h); next }
    inside { print }
  ' "$readme"
}

has() { if grep -qF -- "$2" <<<"$1"; then echo "ok:   $3"; else echo "FAIL: $3"; fail=1; fi; }
lacks() { if grep -qE -- "$2" <<<"$1"; then echo "FAIL: $3"; fail=1; else echo "ok:   $3"; fi; }

# The section must sit under "## 📚 Reference": the last "## " heading
# before it is Reference.
parent=$(awk '/^## / { h = $0 } /^### Uninstall$/ { print h; exit }' "$readme")
if [[ "$parent" == "## 📚 Reference" ]]; then echo "ok:   Uninstall is under Reference"; else echo "FAIL: no '### Uninstall' under '## 📚 Reference' (found under '${parent:-nothing}')"; fail=1; fi

un="$(section '### Uninstall')"
installer="$(cat "$root/install.sh")"
server="$(cat "$root/ergosphere.py")"

# The default install dir, as install.sh sets it, with $HOME written as ~.
dest=$(sed -n 's/^ *DEFAULT_DEST="\${HOME:-}\(.*\)"$/~\1/p' "$root/install.sh" | head -n 1)
if [[ -n "$dest" ]]; then echo "ok:   install.sh default dir is $dest"; else echo "FAIL: DEFAULT_DEST not found in install.sh"; fail=1; fi
has "$un" "rm -rf ${dest:-<none>}" "Uninstall deletes the default install dir ($dest)"
has "$un" '--dir' 'Uninstall covers a custom --dir'

# What the installer and server write, and that the steps name each one.
has "$installer" '"$DEST/ergosphere.log"'  'install.sh writes ergosphere.log in the install dir'
has "$un"        'ergosphere.log'          'Uninstall names ergosphere.log'
has "$installer" 'CFG_FILE="$DEST/ergosphere.json"' 'install.sh writes ergosphere.json in the install dir'
has "$un"        'ergosphere.json'         'Uninstall names ergosphere.json'
has "$server"    'path.name + ".bak"'      'ergosphere.py keeps a .bak next to the config'
has "$un"        '.bak'                    'Uninstall names the .bak'
has "$server"    'os.environ.get("ERGOSPHERE_CONFIG")' 'ergosphere.py reads ERGOSPHERE_CONFIG'
has "$un"        'ERGOSPHERE_CONFIG'       'Uninstall covers ERGOSPHERE_CONFIG'
has "$un"        'kill <pid>'              'Uninstall stops the server first'
has "$un"        '/health'                 'Uninstall says where to find the pid'

# The FAQ offers running it from your own systemd unit with Restart=always;
# kill alone does not stop that (systemd starts it again), so the steps must
# say how to stop and remove the unit.
has "$(cat "$readme")" 'your own systemd unit' 'README offers a user-run systemd unit'
has "$un" 'systemctl --user disable --now <unit>' 'Uninstall stops and disables a systemd unit'
has "$un" 'unit file'                             'Uninstall deletes the unit file'
has "$un" 'daemon-reload'                         'Uninstall reloads systemd after removing the unit'

# "Nothing else to remove" holds only while install.sh writes none of these.
lacks "$installer" 'systemctl|crontab|\.bashrc|\.profile|\.zshrc|/etc/|\.grok' 'install.sh adds no service, cron, profile or ~/.grok change'
has "$un" '~/.grok' 'Uninstall says ~/.grok is left alone'

# The FAQ answer points at the section.
faq="$(awk '/How do I stop or remove it\?/ { on = 1 } on && /<\/details>/ { exit } on { print }' "$readme")"
has "$faq" '(#uninstall)' 'FAQ stop/remove answer links to Uninstall'
exit "$fail"
