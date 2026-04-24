#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
# Copyright (C) 2026 Stephane Galland <galland@arakhne.org>
#
# This program is free library; you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License as
# published by the Free Software Foundation; either version 3 of the
# License, or any later version.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this library; see the file COPYING.  If not,
# write to the Free Software Foundation, Inc., 59 Temple Place - Suite
# 330, Boston, MA 02111-1307, USA.

"""
Synchronize a local folder to a WebDAV server using local hashing.
Local files are always the reference.
"""

import os
import re
import sys
from collections import deque
import argparse
import getpass
import configparser
import hashlib
import json
from dataclasses import dataclass
import traceback
from tqdm import tqdm
from abc import ABC, abstractmethod
from pathlib import Path
from typing import override
import fnmatch
from colorama import Fore, Style
from webdav3.client import Client
from webdav3.exceptions import WebDavException
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------- Configuration ----------
SCRIPT_LAUNCH_NAME = os.path.basename(sys.argv[0])
m = re.match(r'rsync-(.+)', SCRIPT_LAUNCH_NAME, re.S + re.DOTALL)
if m:
    DEFAULT_CONFIG_NAME : str = m.group(1).strip('.py').strip()
else:
    DEFAULT_CONFIG_NAME : str = 'default'
DEFAULT_WEBDAV_URL : str = ""
DEFAULT_LOCAL_SOURCE : str = "./"
DEFAULT_CONFIG_FILE : Path = Path.home() / ".config" / ("webdav_sync_" + DEFAULT_CONFIG_NAME + ".conf")
STATE_FILENAME : str = ".webdav_sync_state.json"
DEFAULT_EXCLUDES : list[str] = []


# ---------- Base Logger ----------
class BaseTool(ABC):

    # noinspection PyMethodMayBeStatic
    def error(self, *messages : str):
        """
        Show up an error message. This function exists the script with return code to 255.
        """
        for message in messages:
            print(Fore.RED + f"ERROR  : {message}" + Style.RESET_ALL)
        sys.exit(255)

    # noinspection PyMethodMayBeStatic
    def error_tqdm(self, pbar : tqdm, message : str):
        """
        Show up an error message before the progress bar. This function exists the script with return code to 255.
        """
        pbar.write(Fore.RED + f"ERROR  : {message}" + Style.RESET_ALL)

    # noinspection PyMethodMayBeStatic
    def info(self, *messages : str):
        """
        Show up an information message.
        """
        for message in messages:
            print(Fore.BLUE + f"INFO   : {message}" + Style.RESET_ALL)

    # noinspection PyMethodMayBeStatic
    def info_tqdm(self, pbar : tqdm, message : str):
        """
        Show up an information message before the progress bar. This function exists the script with return code to 255.
        """
        pbar.write(Fore.BLUE + f"INFO   : {message}" + Style.RESET_ALL)

    # noinspection PyMethodMayBeStatic
    def info2(self, *messages : str):
        """
        Show up a level-2 information message.
        """
        for message in messages:
            print(Fore.CYAN + "INFO   :" + Style.RESET_ALL + f"    {message}")

    # noinspection PyMethodMayBeStatic
    def success(self, *messages : str):
        """
        Show up a success message.
        """
        for message in messages:
            print(Fore.GREEN + "SUCCESS:" + Style.RESET_ALL + f" {message}")

    # noinspection PyMethodMayBeStatic
    def human_readable_size(self, size_bytes: int) -> str:
        """
        Convert a size in bytes to a human-readable string using binary prefixes (1 KB = 1024 bytes).
        """
        if size_bytes == 0:
            return "0B"

        units = ["B", "KB", "MB", "GB", "TB", "PB", "EB"]
        factor = 1024.0

        unit_index = 0
        size = float(size_bytes)
        while size >= factor and unit_index < len(units) - 1:
            size /= factor
            unit_index += 1

        # Format: 1 decimal place if size < 10 and unit is not bytes, else 0 or 1 as appropriate
        if unit_index == 0:  # Bytes
            return f"{int(size)} {units[unit_index]}"
        elif size < 10:
            return f"{size:.1f} {units[unit_index]}"
        else:
            return f"{size:.1f} {units[unit_index]}"

    # noinspection PyMethodMayBeStatic
    def should_exclude(self, path: Path, root: Path | None, patterns: list[str]) -> bool:
        """
        Check if a file or directory should be excluded.
        Supports simple wildcards (e.g., '*.tmp', 'conf/**' simplified).
        """
        if root is None:
            rel_path = path
        else:
            rel_path = path.relative_to(root)
        parts = rel_path.parts
        for pattern in patterns:
            if "/" in pattern:
                if fnmatch.fnmatch(str(rel_path), pattern):
                    return True
            else:
                for part in parts:
                    if fnmatch.fnmatch(part, pattern):
                        return True
                if fnmatch.fnmatch(path.name, pattern):
                    return True
        return False

    # noinspection PyMethodMayBeStatic
    def _convert_to_local_fs(self, remote_path : str) -> str:
        return remote_path.replace('/', os.sep)


# ---------- WebDAV connector ----------
class WebDAVConnector(BaseTool):

    def __init__(self, url : str, login : str, password : str = None):
        client_options = {
            'webdav_hostname': url,
            'webdav_login': login,
            'webdav_password': password,
        }
        parsed = urlparse(url)
        path = parsed.path
        self.__url = url
        self.__remote_root = path.rstrip('/')
        self.__login = login
        self.__client = Client(client_options)

    @property
    def url(self) -> str:
        return self.__url

    @property
    def login(self) -> str:
        return self.__login

    def _ensure_remote_directory(self, remote_dir: str):
        """Create remote directory recursively if it doesn't exist."""
        if remote_dir in ("", "/", "."):
            return
        try:
            self.__client.mkdir(remote_dir)
        except WebDavException:
            # Directory may already exist
            pass

    def upload_file(self, local_file: Path, remote_path: str, progress = None):
        """Upload a single file to WebDAV, creating parent directories as needed."""
        try:
            parent = os.path.dirname(remote_path)
            if parent and parent != "/":
                self._ensure_remote_directory(parent)
            self.__client.upload_file(remote_path=remote_path,
                                      local_path=local_file,
                                      progress=progress)
        except WebDavException as e:
            self.error(f"Fail to upload {local_file}: {e}")

    def delete_remote_file(self, remote_path: str, verbose: bool = False):
        """Delete a remote file or empty directory."""
        try:
            self.__client.clean(remote_path)
            if verbose:
                self.info2(f"remote file deleted: {remote_path}")
        except WebDavException as e:
            if 'not found' not in str(e):
                self.error(f"Fail to delete {remote_path}: {e}")

    # noinspection PyMethodMayBeStatic
    def __join(self, a : str, b : str) -> str:
        if a and b and (a.endswith('/') or b.startswith('/')):
            r = a + b
        else:
            r = (a or '') + '/' + (b or '')
        return r.rstrip('/')

    def get_all_remote_files(self, remote_path: str,
                             excludes : list[str] = None,
                             relative_paths : bool = False, verbose : bool = False) -> list[str]:
        """List all files on a WebDAV server."""
        if excludes is None:
            excludes = list()

        root_len = len(self.__remote_root)

        queue = deque()
        queue.append(remote_path)

        treated = set()
        treated.add(self.__join(self.__remote_root, remote_path))

        all_files = []
        while queue:
            current_remote_path = queue.popleft()
            if verbose:
                self.info2(current_remote_path)
            items = self.__client.list(current_remote_path, get_info=True)
            for item in items:
                if not item:
                    continue
                if 'path' not in item or not item['path']:
                    continue
                path = item['path'].rstrip('/')
                if path in treated:
                    continue
                treated.add(path)
                abs_path = path[root_len:]
                is_dir = item['isdir'] if 'isdir' in item else False
                if is_dir:
                    queue.appendleft(abs_path)
                else:
                    if relative_paths and abs_path.startswith('/'):
                        abs_path = abs_path[1:]
                    local_fn = self._convert_to_local_fs(abs_path)
                    local_path = Path(local_fn)
                    if not excludes or not self.should_exclude(local_path, None, excludes):
                        all_files.append(local_fn)
                    elif verbose:
                        self.info2(f"excluded file: {local_fn}")

        return all_files



# ---------- Base Command ----------
class BaseCommand(BaseTool, ABC):

    def __init__(self, args : argparse.Namespace, config_file : Path = DEFAULT_CONFIG_FILE, algorithm : str = "sha256"):
        self.__cli_args = args
        self.__config_file = config_file
        self.__algorithm = algorithm

    @property
    def hash_algorithm(self) -> str:
        return self.__algorithm

    @property
    def config_file(self) -> Path:
        return self.__config_file

    @property
    def args(self) -> argparse.Namespace:
        return self.__cli_args

    @abstractmethod
    def run(self):
        raise NotImplementedError()

    def load_config(self) -> configparser.ConfigParser:
        """Load configuration from file."""
        if not self.config_file.exists():
            self.error("No configuration found. Please run 'create' first.")
        config = configparser.ConfigParser()
        config.read(self.config_file)
        return config

    def ask_passwd(self, config : configparser.ConfigParser | None = None) -> str:
        """Query the password if not provided"""
        pwd = self.args.password
        if pwd:
            password = pwd
        else:
            if config is None:
                config = self.load_config()
            assert config is not None
            webdav_section = config['webdav']
            password = webdav_section.get('password', '')
            if not password:
                password = getpass.getpass(f"WebDAV password for {webdav_section['user']}: ")
        return password



# ---------- Show Command ----------
class ShowCommand(BaseCommand):

    def __init__(self, args : argparse.Namespace, config_file : Path = DEFAULT_CONFIG_FILE):
        super().__init__(args, config_file)

    def show_config(self, config : configparser.ConfigParser | None = None):
        """Show the configuration file."""
        if config is None:
            config = self.load_config()

        assert config is not None

        self.info("--- Configuration: %s ---" % DEFAULT_CONFIG_NAME)

        webdav_section = config['webdav']
        self.info("[webdav]")
        self.info("url = %s" % webdav_section.get('url', DEFAULT_WEBDAV_URL))
        self.info("user = %s" % webdav_section.get('user'))
        self.info("password = %s" % ('*****' if webdav_section.get('password', '') else 'not set'))

        self.info("")

        sync_section = config['sync']
        self.info("[sync]")
        self.info("local_source = %s" % sync_section.get('local_source', DEFAULT_LOCAL_SOURCE))
        for excl in re.split(r'\s*' + re.escape(os.pathsep) + r'\s*', sync_section.get('excludes', os.pathsep.join(DEFAULT_EXCLUDES))):
            self.info("excludes = %s" % excl)

    @override
    def run(self):
        self.show_config()


# ---------- Delete Command ----------
class DeleteCommand(BaseCommand):

    def __init__(self, args : argparse.Namespace, config_file : Path = DEFAULT_CONFIG_FILE):
        super().__init__(args, config_file)

    @override
    def run(self):
        if self.config_file.is_file():
            path = self.config_file.name
            self.config_file.unlink(missing_ok=True)
            self.success(f"Configuration file %s deleted" % path)
        else:
            self.info("No configuration file")


# ---------- Create Command ----------
class CreateCommand(BaseCommand):

    def __init__(self, args : argparse.Namespace, config_file : Path = DEFAULT_CONFIG_FILE):
        super().__init__(args, config_file)

    @override
    def run(self):
        config = configparser.ConfigParser()

        config['webdav'] = {
            'url': self.args.url or DEFAULT_WEBDAV_URL,
            'user': self.args.user or os.getlogin(),
        }

        # Ask for password if not provided
        password = self.ask_passwd(config)
        config['webdav']['password'] = password

        local_source : Path = Path(self.args.source).resolve() if self.args.source else Path(DEFAULT_LOCAL_SOURCE).resolve()
        excludes : list[str] = self.args.excludes if self.args.excludes else DEFAULT_EXCLUDES
        config['sync'] = {
            'local_source': str(local_source),
            'excludes': os.pathsep.join(excludes),
        }

        # Ensure config directory exists
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        # Note: password stored in plain text. For better security, consider keyring.
        with open(self.config_file, 'w') as f:
            config.write(f)
        self.success(f"Configuration saved to {self.config_file.name}")

# ---------- CandidateDescription ----------
@dataclass
class CandidateDescription:
    relative_path : str
    local_path : Path
    remote_path : str
    size: int
    reason : str = ''

# ---------- Abstract Sync Command ----------
class AbsractSyncCommand(BaseCommand,ABC):

    def __init__(self, args : argparse.Namespace, config_file : Path = DEFAULT_CONFIG_FILE):
        super().__init__(args, config_file)

    # noinspection PyMethodMayBeStatic
    def _load_state(self, state_path: Path) -> dict[str,dict[str,int|float|str]]:
        """Load the state JSON file. Returns empty dict if not exists."""
        if state_path.exists():
            with open(state_path, "r") as f:
                return json.load(f)
        return {}

    # noinspection PyMethodMayBeStatic
    def _save_state(self, state_path: Path, state: dict[str,dict[str,int|float|str]]):
        """Save the state dictionary to JSON file."""
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)

    def _get_local_file_info_with_hash(self, file_path: Path) -> tuple[int, float, str]:
        """Return (size, mtime, hash) for a local file."""
        stat = file_path.stat()
        hash_func = hashlib.new(self.hash_algorithm)
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hash_func.update(chunk)
        file_hash = hash_func.hexdigest()
        return stat.st_size, stat.st_mtime, file_hash

    def _analyze_local_files(self, remote_root : str, local_root : Path, excludes : list[str],
                             state : dict[str,dict[str,int|float|str]],
                             verbose: bool = False) -> tuple[list[CandidateDescription],dict[str,dict[str,int|float|str]],int]:
        """Phase 1: Walk local files and upload changed/new files"""
        self.info("Analyzing local files...")
        queue : list[CandidateDescription] = list()
        current_state : dict[str,dict[str,int|float|str]] = {}
        total_size = 0
        for root, dirs, files in os.walk(local_root):
            current_path = Path(root)

            # Filter directories to exclude (modify dirs in-place to avoid walking into them)
            dirs_to_remove = []
            for d in dirs:
                full_dir = current_path / d
                if self.should_exclude(full_dir, local_root, excludes):
                    if self.args.verbose:
                        self.info2(f"excluded directory: {full_dir.relative_to(local_root)}")
                    dirs_to_remove.append(d)
            for d in dirs_to_remove:
                dirs.remove(d)

            for file in files:
                local_file = current_path / file
                rel_path = local_file.relative_to(local_root)
                rel_path_str = str(rel_path).replace("\\", "/")

                # Skip excluded files
                if self.should_exclude(local_file, local_root, excludes):
                    if verbose:
                        self.info2(f"excluded file: {rel_path_str}")
                    continue

                # Compute current file info (size, mtime, hash)
                size, mtime, file_hash = self._get_local_file_info_with_hash(local_file)

                # Store in current state (will be saved later)
                current_state[rel_path_str] = {
                    "size": size,
                    "mtime": mtime,
                    "hash": file_hash,
                }

                # Check if file needs upload
                old_record = state.get(rel_path_str)
                if old_record and old_record.get("hash") == file_hash:
                    # Hash matches -> unchanged
                    if verbose:
                        self.info2(f"unchanged: {rel_path_str}")
                    continue

                # File is new or modified
                remote_path = os.path.join(remote_root, rel_path_str).replace("\\", "/")
                reason = "new file" if not old_record else "new content"

                candidate = CandidateDescription(
                    relative_path=rel_path_str,
                    local_path=local_file,
                    remote_path=remote_path,
                    reason=reason,
                    size=size)
                queue.append(candidate)
                total_size += size
                if verbose:
                    self.info2(f"changed file: {rel_path_str}")

        human_size = self.human_readable_size(total_size)
        self.success(f"Found {len(queue)} files to upload for {human_size}.")
        return queue, current_state, total_size

    # noinspection PyMethodMayBeStatic
    def __upload_with_progress(self, connector : WebDAVConnector, candidate : CandidateDescription, pbar_files : tqdm):
        desc = candidate.relative_path[-60:]
        try:
            with tqdm(total=candidate.size, unit="B", unit_scale=True, leave=False, desc=desc) as pbar_file:
                def progress_callback(current,total):
                    nonlocal pbar_file
                    pbar_file.n = current
                    pbar_file.refresh()  # force display update
                connector.upload_file(local_file=candidate.local_path,
                                      remote_path=candidate.remote_path,
                                      progress=progress_callback)
        except BaseException as e:
            details = traceback.format_exc()
            self.error_tqdm(pbar_files, f"Cannot upload {candidate.local_path} for of exception of type {type(e).__name__}: {e}\n{details}")

    def _upload(self, connector : WebDAVConnector, queue : list[CandidateDescription], dry_run : bool = False,
                workers : int = 5):
        """Phase 2: Upload files to the remote server."""
        self.info("Uploading files...")
        with tqdm(total=len(queue), unit="file", desc="", leave=False) as pbar_files:
            future_to_file = {}
            try:
                if workers > 1 and not dry_run:
                    pbar_files.set_description('')
                    with ThreadPoolExecutor(max_workers=workers) as executor:
                        # Submit all upload tasks to the executor
                        future_to_file = {
                            executor.submit(self.__upload_with_progress, connector, candidate, pbar_files): candidate.local_path
                            for candidate in queue
                        }

                        # Process results as they complete
                        for future in as_completed(future_to_file):
                            local_file = future_to_file[future]
                            try:
                                future.result()
                                pbar_files.update(1)
                                self.info_tqdm(pbar_files, f'{local_file} transferred')
                            except Exception as e:
                                details = traceback.format_exc()
                                self.error_tqdm(pbar_files, f'{local_file} generated an exception of type {type(e).__name__}: {e}\n{details}')
                else:
                    for candidate in queue:
                        pbar_files.set_description(candidate.relative_path[-60:])
                        if not dry_run:
                            self.__upload_with_progress(connector, candidate)
                        pbar_files.update(1)
                pbar_files.set_description('')
            except KeyboardInterrupt:
                for future in future_to_file:
                    future.cancel()
                self.error_tqdm(pbar_files, "Program interrupted by the user. All uploading threads stopped.")
                sys.exit(255)
            except BaseException as e:
                details = traceback.format_exc()
                self.error_tqdm(pbar_files, f"Cannot upload files because of exception of type {type(e).__name__}: {e}\n{details}")
        if dry_run:
            self.success(f"{len(queue)} files are uploadable (DRY RUN mode)")
        else:
            self.success(f"{len(queue)} files were uploaded")


    def _delete_remote_files(self, connector : WebDAVConnector, state : dict,
                             current_state : dict[str,dict[str,int|float|str]],
                             remote_root : str, dry_run : bool = False, verbose : bool = False):
        """Phase 3: Handle deletions on remote server."""
        self.info("Deleting unnecessary remote files...")
        # Find files that exist in state but no longer locally
        nb_deleted = 0
        with tqdm(total=len(state), desc="", leave=False) as pbar:
            for rel_path_str, old_info in state.items():
                pbar.set_description(rel_path_str)
                if rel_path_str not in current_state:
                    remote_path = os.path.join(remote_root, rel_path_str).replace("\\", "/")
                    #if verbose:
                    #    self.info2(f"{rel_path_str} (was present locally before)")
                    if not dry_run:
                        connector.delete_remote_file(remote_path, verbose)
                    nb_deleted += 1
                    self.info_tqdm(pbar, f"{remote_path} deleted")
                pbar.update(1)
            pbar.set_description('')
        if dry_run:
            self.success(f"{nb_deleted} file(s) may be deleted (DRY RUN mode).")
        else:
            self.success(f"{nb_deleted} file(s) were deleted.")


    def _save_hash(self, state_path : Path, current_state : dict[str,dict[str,int|float|str]],
                   dry_run : bool = False):
        """Phase 3: Save updated state."""
        if not dry_run:
            self._save_state(state_path, current_state)
            self.success("Local hash values saved.")
        else:
            self.info("Local hash values not updated (DRY RUN mode).")


    def sync_with_local_hashing(self, connector : WebDAVConnector, local_root: Path, remote_root: str,
                                excludes: list[str], dry_run: bool, verbose: bool,
                                delete: bool,
                                workers : int = 5):
        """
        Synchronize local -> remote using a local hash state file.
        """
        self.info("Loading local hash values...")
        local_root = local_root.resolve()
        state_path = local_root / STATE_FILENAME
        state = self._load_state(state_path)

        if verbose:
            self.info2(f"Local root: {local_root}")
            self.info2(f"Remote root: {remote_root}")
            self.info2(f"State file: {state_path}")
            self.info2(f"Excludes: {os.pathsep.join(excludes)}")
            self.info2(f"Delete extra files: {delete}")

        queue, current_state, total_size = self._analyze_local_files(local_root=local_root,
                                                                     remote_root=remote_root,
                                                                     excludes=excludes,
                                                                     state=state,
                                                                     verbose=verbose)
        self._upload(connector=connector,
                     queue=queue,
                     dry_run=dry_run,
                     workers=workers)

        if delete:
            self._delete_remote_files(connector=connector,
                                      state=state,
                                      current_state=current_state,
                                      remote_root=remote_root,
                                      dry_run=dry_run,
                                      verbose=verbose)

        self._save_hash(state_path=state_path,
                        current_state=current_state,
                        dry_run=dry_run)


    def connect(self) -> tuple[WebDAVConnector, Path, str, list[str]]:
        """ Start a connection to the WebDav server."""
        config = self.load_config()
        webdav_section = config['webdav']
        sync_section = config['sync']

        local_source = Path(sync_section.get('local_source', DEFAULT_LOCAL_SOURCE))
        if not local_source.exists():
            self.error(f"Local source directory does not exist: {local_source}")

        remote_root = self.args.remote_root or ""

        if self.args.excludes:
            excludes : list[str] = self.args.excludes
        else:
            excludes : list[str] = sync_section.get('excludes', "").split(os.pathsep) if sync_section.get('excludes') else DEFAULT_EXCLUDES
        excludes.append(STATE_FILENAME)

        password = self.ask_passwd(config)

        # Connect to WebDAV
        connector = WebDAVConnector(url=webdav_section['url'],
                                    login=webdav_section['user'],
                                    password=password)
        return connector, local_source, remote_root, excludes



# ---------- Sync Command ----------
class SyncCommand(AbsractSyncCommand):

    def __init__(self, args : argparse.Namespace, config_file : Path = DEFAULT_CONFIG_FILE):
        super().__init__(args, config_file)

    @override
    def run(self):
        connector, local_source, remote_root, excludes = self.connect()

        self.info(f"Syncing from {local_source} to {connector.url}/{remote_root}")
        if self.args.dryrun:
            self.info("DRY RUN mode - no actual changes")

        self.sync_with_local_hashing(
            connector=connector,
            local_root=local_source,
            remote_root=remote_root,
            excludes=excludes,
            dry_run=self.args.dryrun,
            verbose=self.args.verbose,
            delete=not self.args.nodelete,
            workers=self.args.workers)

        self.success("Sync completed.")


# ---------- Update Command ----------
class UpdateCommand(AbsractSyncCommand):

    def __init__(self, args : argparse.Namespace, config_file : Path = DEFAULT_CONFIG_FILE):
        super().__init__(args, config_file)

    @override
    def run(self):
        connector, local_source, remote_root, excludes = self.connect()

        self.info("Read hash values...")
        local_source = local_source.resolve()
        state_path = local_source / STATE_FILENAME
        current_state = self._load_state(state_path=state_path)

        self.info("Retrieving remote files...")
        remote_files = connector.get_all_remote_files(remote_root,
                                                      excludes=excludes,
                                                      relative_paths=True,
                                                      verbose=True)

        self.info("Merging hash values...")
        new_files = 0
        for remote_file in remote_files:
            if remote_file not in current_state:
                current_state[remote_file] = {
                    "size": 0,
                    "mtime": 0,
                    "hash": '',
                }
                new_files += 1

        if self.args.dryrun:
            self.info(f"{new_files} new hash values were found for a total of {len(current_state)} (DRY RUN mode).")
        else:
            self._save_hash(state_path=state_path,
                            current_state=current_state,
                            dry_run=False)
            self.success(f"{len(current_state)} hash values saved with {new_files} new hash values from remote files.")


# ---------- Main ----------
def main():
    try:
        parser = argparse.ArgumentParser(description="Synchronize local folder to WebDAV using local hashing")
        subparsers = parser.add_subparsers(dest='command', required=True)

        # Command: delete
        subparsers.add_parser('delete', help="Delete configuration")

        # Command: create
        parser_create = subparsers.add_parser('create', help="Create configuration")
        parser_create.add_argument('--url', help="WebDAV server URL")
        parser_create.add_argument('--user', help="Username")
        parser_create.add_argument('--password', help="Password (insecure, better leave empty to prompt)")
        parser_create.add_argument('--source', help="Local source path (default: ./)")
        parser_create.add_argument('--excludes', nargs='*', help="Exclusion patterns (space-separated)")

        # Command: show
        subparsers.add_parser('show', help="Show configuration")

        # Command: sync
        parser_sync = subparsers.add_parser('sync', help="Run synchronization")
        parser_sync.add_argument('--password', help="Password to pass to the WebDAV server")
        parser_sync.add_argument('--remote-root', default="", help="Remote root path (default: /)")
        parser_sync.add_argument('--nodelete', action='store_true', help="Do not delete remote files not present locally")
        parser_sync.add_argument('--dryrun', action='store_true', help="Simulate without making changes")
        parser_sync.add_argument('--verbose', '-v', action='store_true', help="Show detailed output")
        parser_sync.add_argument('--workers', type=int, default=5, help="Number of parallel workers (default: 5)")
        parser_sync.add_argument('--excludes', nargs='*', help="Override exclusion patterns")

        # Command: update
        parser_update = subparsers.add_parser('update', help="Update the local hash values from the remote server")
        parser_update.add_argument('--password', help="Password to pass to the WebDAV server")
        parser_update.add_argument('--remote-root', default="", help="Remote root path (default: /)")
        parser_update.add_argument('--dryrun', action='store_true', help="Simulate without making changes")
        parser_update.add_argument('--verbose', '-v', action='store_true', help="Show detailed output")
        parser_update.add_argument('--excludes', nargs='*', help="Override exclusion patterns")

        args = parser.parse_args()

        if args.command == 'create':
            cmd = CreateCommand(args)
        elif args.command == 'delete':
            cmd = DeleteCommand(args)
        elif args.command == 'show':
            cmd = ShowCommand(args)
        elif args.command == 'sync':
            cmd = SyncCommand(args)
        elif args.command == 'update':
            cmd = UpdateCommand(args)
        else:
            parser.print_help()
            cmd = None

        if cmd is not None:
            cmd.run()
            sys.exit(0)
        else:
            sys.exit(255)
    except KeyboardInterrupt:
        sys.exit(255)

if __name__ == "__main__":
    main()
