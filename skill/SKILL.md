---
name: ergosphere
description: 'Deploys and operates Ergosphere, the local web portal for Grok CLI sessions. Use for Ergosphere, "deploy Ergosphere", "Ergosphere dashboard", and copying the page to another machine that has grok.'
---

# Ergosphere

Ergosphere is a local web portal for Grok CLI sessions. It lists your sessions and opens each one as the real grok TUI in your browser. `README.md` is the behavior reference.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/JuliusPIV/ergosphere/main/install.sh | bash
```

Options after `| bash -s --`: `--auto`, `--manual`, `--dir DIR`, `--port N`, `--bind ADDR`, `--yes`, `--check`, `--help`. No terminal means automatic: `~/.local/share/ergosphere`. First install: `127.0.0.1`, port `ERGOSPHERE_PORT` or `9241`; later runs default to the bind and port saved in `ergosphere.json`. A non-loopback bind needs `y` on the terminal or `--yes`. Prerequisites (Linux, python3, grok, bash, curl, tar, gzip, nohup, sha256sum) are checked and never installed. grok sign-in is not checked.

Refused: port below 1024 when not root, a bind that is not this machine's address, a non-empty non-Ergosphere dir, any other listener on the port, `ERGOSPHERE_TOKEN` set. Failures print `failed at check|download|install|start: ...` and exit 1.

Reruns are safe: only files whose sha256 differs are replaced, `ergosphere.json` is kept (agents and the remembered model/effort stay; `--bind`/`--port` or `ERGOSPHERE_PORT` update only those keys), a running server from the same dir is reused. When its code changed, its address changed or an agent was added, it is restarted only if it has no grok sessions open; otherwise it keeps running and the summary says how to finish the change. Stop with the `kill <pid>` the summary prints; that also stops its grok sessions.

## Requirements

- Linux (tested Ubuntu 24.04 LTS). WSL2 should work, untested. macOS not supported yet (planned later).
- Python 3, standard library only (tested 3.12).
- grok on `PATH`, signed in as the server user (tested 1.0.70). Sessions from `~/.grok/sessions`, agents from `~/.grok/agents` or `GROK_AGENTS_DIR`.
- `vendor/` (`xterm.js`, `xterm.css`, `addon-fit.js`) next to `ergosphere.py`. The server exits if one is missing.
- Configuration: `ergosphere.json` next to `ergosphere.py`, editable from the Settings page. Saving a port that is already in use is refused, and if the new address cannot be used at restart, Ergosphere falls back to the previous one. Do not publish a real one.

## Remote access

Default bind is `127.0.0.1`. From another computer use an SSH tunnel (`ssh -N -L 9241:127.0.0.1:9241 you@your-server`, then http://localhost:9241/), or bind a network address with `--bind ... --yes` or the Settings page. v0.1 has no login: anyone who can reach the port can run grok as the server user.

## Troubleshooting

```bash
python3 ergosphere.py --check
python3 ergosphere.py
```

`grok not on PATH` exits 1 before binding. The ready line is `ERGOSPHERE_READY`. A second start exits 0. Log: `ergosphere.log` in the install dir. Confirm grok flags with `grok --help`; on 1.0.70 Ask is `--permission-mode default`, Auto is `--permission-mode auto`, Always approve is `--always-approve`.

## Traps

- One session, one grok. A second view attaches to the same process. A grok CLI on the same machine holding the session is offered "Quit that CLI and resume here"; a grok run by another Ergosphere is not taken over.
- Only Create (`POST /launch`) and Resume (`POST /resume`) start grok. Loading `/chat` or `/term` never does.
- Stopped sessions resume in Ask regardless of their previous mode.
- Closing a tab does not stop grok. Quit does. Stopping Ergosphere stops its grok sessions.
- Ports 80 and 443, or a proxy that drops the port from Host, are not supported.
- `ERGOSPHERE_TOKEN` (path to a one-line token file) is only for a reverse proxy that sends `Authorization: Bearer <token>`. Browsers do not send it.
- Do not start `grok agent serve`. The page is a PTY of the interactive grok TUI.
