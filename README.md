# board-server

Zero-dependency web server (Python stdlib) that renders the **Kiro Crew artifact
library** and the **Galleon Scrum board** as HTML over the private tailnet.

- **Live URL:** <https://diezy-realm.taildae239.ts.net/> (tailscale serve → `127.0.0.1:8787`)
- **Runtime:** `python3` stdlib only (no pip deps). Tested on Python 3.15.
- **Lifecycle:** systemd **socket-activation** — starts on first HTTP hit, exits
  after `ARTIFACT_IDLE_TIMEOUT` seconds idle. The socket stays listening while
  the service is dead, so it costs nothing when nobody is looking.

## Code vs data — what this repo owns

This repo holds **only the server code**. It does **not** own the data it renders,
and that data must stay where Kiro Crew and the board tooling write it:

| Thing | Path | Owner | This server |
|---|---|---|---|
| Server code | `~/Apps/board-server/artifact-server.py` | **this repo** | — |
| Artifact store | `~/.kiro/crew/artifacts/` | Kiro Crew dashboard / MCP | reads only |
| Published docs | `~/.kiro/crew/published/docs/` | Kiro Crew | reads only |
| Scrum board SSOT | `~/Apps/board/` | board tooling (`validate.py`) | reads only |

All data paths are resolved from `Path.home()` inside the server (constants
`ART_DIR`, `DOCS_DIR`, `BOARD_DIR`), so the code file can live anywhere — only
the systemd `ExecStart` points at this location. **Do not move the artifact store
into this repo:** the dashboard keeps writing to `~/.kiro/crew/artifacts/`, so a
moved copy would desync.

## Routes

- `/` — Artifact Library (folder tree, date/format/folder filter, search,
  markdown→HTML, print-to-PDF).
- `/board` — toggle to the Galleon board (Status / Kanban view, drag-to-move
  tasks, sprint progress), read from `~/Apps/board/`.
- `POST /api/create|mkdir|move|edit|delete|import` — artifact mutations.
- `POST /api/board/status` — board task status change.

## systemd (socket-activation, user units)

Units live in `~/.config/systemd/user/`:

- `artifact-board.socket` — `ListenStream=127.0.0.1:8787`, `Accept=no`.
- `artifact-board.service` — `ExecStart=/usr/bin/python3 ~/Apps/board-server/artifact-server.py`,
  `Environment=ARTIFACT_IDLE_TIMEOUT=300`.

`linger` is enabled (`loginctl enable-linger`) so the socket survives with no
active login session.

### Maintenance

```bash
# status
systemctl --user status artifact-board.socket artifact-board.service

# apply code change (service exits on idle; next hit runs new code) — or force now:
systemctl --user restart artifact-board.service

# change idle timeout: edit Environment= in the .service, then
systemctl --user daemon-reload

# logs
journalctl --user -u artifact-board.service -f

# disable entirely (frees port 8787)
systemctl --user disable --now artifact-board.socket artifact-board.service
```

tailscale serve is already pointed at `8787` and needs no change
(`tailscale serve status` to verify).

## History

Server previously lived at `~/.kiro/crew/published/artifact-server.py` (hidden,
un-versioned). Moved here on 2026-10-17 for a clear, git-tracked path. The old
copy is retained there as a `.bak.py` and can be deleted once this is confirmed
stable.
