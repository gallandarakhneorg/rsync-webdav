# rsync-webdav.py – Synchronize local folder to WebDAV with local hashing

This Python script synchronizes a local directory to a remote WebDAV server.  
**Local files are always the reference** (one‑way sync).  
It uses a local state file (`.webdav_sync_state.json`) with SHA256 hashes to reliably detect changes, bypassing WebDAV's lack of checksum support.

## Features

- **One‑way sync** (local → remote)  
- **Local hashing** (SHA256) – detects content changes even when size/mtime are identical  
- **Dynamic progress display** – overall transfer speed, current file, recently uploaded files (requires `rich`)  
- **Exclude patterns** – skip files/directories by name or wildcard  
- **Dry‑run mode** – preview what would be uploaded/deleted  
- **Delete remote files** – remove files that no longer exist locally (disable with `--nodelete`)  
- **Preserves directory structure** – creates remote directories automatically  
- **Stores configuration** – WebDAV URL, credentials, local source, excludes  
- **Works on Linux, macOS, Windows** (requires Python 3.6+)

## Installation

### 1. Clone or download the script

Save the script as `rsync-webdav.py` and make it executable:

```bash
chmod +x rsync-webdav.py
```

### 2. Install Python dependencies

```bash
pip install webdavclient3
```

- `webdavclient3` – WebDAV operations  

## Configuration

Before synchronizing, you must create a configuration file.  
Run:

```bash
./rsync-webdav.py create [options...]
```

You will be prompted for your WebDAV password.  
The configuration is saved to `~/.config/webdav_sync_<NAME>.conf` (plain text; use `--password` on the command line only if you understand the security implications).

**`<NAME>`** is the name of the configuration. It is determined from the name of the script at launching time. For example, if you rename the script `rsync-my_config.py`, then the `<NAME>` becomes `my_config`. On Unix systems, we recommend to create symbolic links to the original python script but with the expected name for the link.

**Additional `create` options:**

| Option | Description |
|--------|-------------|
| `--url` | WebDAV server URL, e.g., `https://myhost.mydomain.com/path/to/files`) |
| `--user` | WebDAV username (default: current system user) |
| `--source` | Local source directory (default: `./`) |
| `--excludes` | Space‑separated list of exclusion patterns, e.g., `conf db lists ` |

Example with custom excludes:

```bash
./rsync-my_config.py create --source /home/user/data --excludes tmp cache '*.log'
```

## Usage

### Basic sync

```bash
./rsync-webdav.py sync
```

This will upload all changed/new files to the remote root (as defined in the configuration).

### Dry run (simulate)

```bash
./rsync-webdav.py sync --dry-run -v
```

### No deletion of the remote files that no longer exist locally

```bash
./rsync-webdav.py sync --nodelete
```

### Override exclusions

```bash
./rsync-webdav.py sync --excludes tmp '*.pyc'
```

### Verbose output

```bash
./rsync-webdav.py sync -v
```

## How it works

1. **Scanning** – walks the local directory, computes SHA256 hash for every file.  
2. **Comparison** – compares current hash with the hash stored in `.webdav_sync_state.json`.  
3. **Upload** – only files with different/missing hashes are uploaded.  
4. **Progress** – a live display shows overall speed, current file.  
5. **Deletion** (if `--nodelete` not given) – removes remote files whose local counterparts no longer exist.  
6. **State update** – after a successful sync, the state file is updated.

## Excluding files/directories

Exclusion patterns support simple wildcards (`*`) and directory names.  
Examples:

- `tmp` – excludes any file/directory named `tmp` anywhere
- `*.pyc` – excludes all `.pyc` files
- `conf/*` – excludes everything inside `conf/`
- `generate_template_archives.py` – excludes that specific file

The state file `.webdav_sync_state.json` is automatically excluded.

