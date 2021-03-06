#!/usr/bin/env python
# vim:fileencoding=utf-8
# License: GPLv3 Copyright: 2020, Kovid Goyal <kovid at kovidgoyal.net>


import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from typing import Any, List, Optional

from kitty.cli import parse_args
from kitty.cli_stub import RemoteFileCLIOptions
from kitty.constants import cache_dir
from kitty.typing import BossType
from kitty.utils import command_for_open, get_editor, open_cmd

from ..ssh.main import SSHConnectionData
from ..tui.handler import result_handler
from ..tui.operations import (
    faint, raw_mode, reset_terminal, set_cursor_visible, styled
)


def key(x: str) -> str:
    return styled(x, bold=True, fg='green')


def get_key_press(allowed: str, default: str) -> str:
    response = default
    with raw_mode():
        try:
            while True:
                q = sys.stdin.buffer.read(1)
                if q:
                    if q in b'\x1b\x03':
                        break
                    with suppress(Exception):
                        response = q.decode('utf-8').lower()
                        if response in allowed:
                            break
        except (KeyboardInterrupt, EOFError):
            pass
    return response


def option_text() -> str:
    return '''\
--mode -m
choices=ask,edit
default=ask
Which mode to operate in.


--path -p
Path to the remote file.


--hostname -h
Hostname of the remote host.


--ssh-connection-data
The data used to connect over ssh.
'''


def show_error(msg: str) -> None:
    print(styled(msg, fg='red'))
    print()
    print('Press any key to exit...')
    sys.stdout.flush()
    with raw_mode():
        while True:
            try:
                q = sys.stdin.buffer.read(1)
                if q:
                    break
            except (KeyboardInterrupt, EOFError):
                break


def ask_action(opts: RemoteFileCLIOptions) -> str:
    print('What would you like to do with the remote file on {}:'.format(styled(opts.hostname or 'unknown', bold=True, fg='magenta')))
    print(styled(opts.path or '', fg='yellow', fg_intense=True))
    print()

    def help_text(x: str) -> str:
        return faint(x)

    print('{}dit the file'.format(key('E')))
    print(help_text('The file will be downloaded and opened in an editor. Any changes you save will'
                    ' be automatically sent back to the remote machine'))
    print()

    print('{}pen the file'.format(key('O')))
    print(help_text('The file will be downloaded and opened by the default open program'))
    print()

    print('{}ave the file'.format(key('S')))
    print(help_text('The file will be downloaded to a destination you select'))
    print()

    print('{}ancel'.format(key('C')))
    print()

    sys.stdout.flush()
    response = get_key_press('ceos', 'c')
    return {'e': 'edit', 'o': 'open', 's': 'save'}.get(response, 'cancel')


def simple_copy_command(conn_data: SSHConnectionData, path: str) -> List[str]:
    cmd = [conn_data.binary]
    if conn_data.port:
        cmd += ['-p', str(conn_data.port)]
    cmd += [conn_data.hostname, 'cat', path]
    return cmd


class ControlMaster:

    def __init__(self, conn_data: SSHConnectionData, remote_path: str):
        self.conn_data = conn_data
        self.remote_path = remote_path
        self.cmd_prefix = cmd = [
            conn_data.binary, '-o', f'ControlPath=~/.ssh/kitty-master-{os.getpid()}-%r@%h:%p',
            '-o', 'TCPKeepAlive=yes', '-o', 'ControlPersist=yes'
        ]
        if conn_data.port:
            cmd += ['-p', str(conn_data.port)]
        self.batch_cmd_prefix = cmd + ['-o', 'BatchMode=yes']

    def __enter__(self) -> 'ControlMaster':
        subprocess.check_call(
            self.cmd_prefix + ['-o', 'ControlMaster=auto', '-fN', self.conn_data.hostname])
        subprocess.check_call(
            self.batch_cmd_prefix + ['-O', 'check', self.conn_data.hostname])
        self.tdir = tempfile.mkdtemp()
        self.dest = os.path.join(self.tdir, os.path.basename(self.remote_path))
        return self

    def __exit__(self, *a: Any) -> bool:
        subprocess.Popen(
            self.batch_cmd_prefix + ['-O', 'exit', self.conn_data.hostname],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        ).wait()
        shutil.rmtree(self.tdir)
        return True

    @property
    def is_alive(self) -> bool:
        return subprocess.Popen(
            self.batch_cmd_prefix + ['-O', 'check', self.conn_data.hostname],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
        ).wait() == 0

    def download(self) -> bool:
        with open(self.dest, 'wb') as f:
            return subprocess.run(
                self.batch_cmd_prefix + [self.conn_data.hostname, 'cat', self.remote_path],
                stdout=f, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL
            ).returncode == 0

    def upload(self, suppress_output: bool = True) -> bool:
        cmd_prefix = self.cmd_prefix if suppress_output else self.batch_cmd_prefix
        cmd = cmd_prefix + [self.conn_data.hostname, 'cat', '>', self.remote_path]
        if not suppress_output:
            print(' '.join(map(shlex.quote, cmd)))
        redirect = subprocess.DEVNULL if suppress_output else None
        with open(self.dest, 'rb') as f:
            return subprocess.run(cmd, stdout=redirect, stderr=redirect, stdin=f).returncode == 0


def save_output(cmd: List[str], dest_path: str) -> bool:
    with open(dest_path, 'wb') as f:
        cp = subprocess.run(cmd, stdout=f)
        return cp.returncode == 0


Result = Optional[str]


def main(args: List[str]) -> Result:
    msg = 'Ask the user what to do with the remote file'
    try:
        cli_opts, items = parse_args(args[1:], option_text, '', msg, 'kitty remote_file', result_class=RemoteFileCLIOptions)
    except SystemExit as e:
        if e.code != 0:
            print(e.args[0])
            input('Press enter to quit...')
        raise SystemExit(e.code)

    print(set_cursor_visible(False), end='', flush=True)
    try:
        action = ask_action(cli_opts)
    finally:
        print(reset_terminal(), end='', flush=True)
    try:
        return handle_action(action, cli_opts)
    except Exception:
        print(reset_terminal(), end='', flush=True)
        import traceback
        traceback.print_exc()
        show_error('Failed with unhandled exception')


def save_as(conn_data: SSHConnectionData, remote_path: str) -> None:
    ddir = cache_dir()
    os.makedirs(ddir, exist_ok=True)
    last_used_store_path = os.path.join(ddir, 'remote-file-last-used.txt')
    try:
        with open(last_used_store_path) as f:
            last_used_path = f.read()
    except FileNotFoundError:
        last_used_path = tempfile.gettempdir()
    last_used_file = os.path.join(last_used_path, os.path.basename(remote_path))
    print(
        'Where do you wish to save the file? Leaving it blank will save it as:',
        styled(last_used_file, fg='yellow')
    )
    print('Relative paths will be resolved from:', styled(os.getcwd(), fg_intense=True, bold=True))
    print()
    from ..tui.path_completer import PathCompleter
    try:
        dest = PathCompleter().input()
    except (KeyboardInterrupt, EOFError):
        return
    if dest:
        dest = os.path.expandvars(os.path.expanduser(dest))
        if os.path.isdir(dest):
            dest = os.path.join(dest, os.path.basename(remote_path))
        with open(last_used_store_path, 'w') as f:
            f.write(os.path.dirname(os.path.abspath(dest)))
    else:
        dest = last_used_file
    if os.path.exists(dest):
        print(reset_terminal(), end='')
        print(f'The file {styled(dest, fg="yellow")} already exists. What would you like to do?')
        print(f'{key("O")}verwrite  {key("A")}bort  Auto {key("R")}ename {key("N")}ew name')
        response = get_key_press('anor', 'a')
        if response == 'a':
            return
        if response == 'n':
            print(reset_terminal(), end='')
            return save_as(conn_data, remote_path)

        if response == 'r':
            q = dest
            c = 0
            while os.path.exists(q):
                c += 1
                b, ext = os.path.splitext(dest)
                q = f'{b}-{c}{ext}'
            dest = q
    if os.path.dirname(dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
    cmd = simple_copy_command(conn_data, remote_path)
    if not save_output(cmd, dest):
        show_error('Failed to copy file from remote machine')


def handle_action(action: str, cli_opts: RemoteFileCLIOptions) -> Result:
    conn_data = SSHConnectionData(*json.loads(cli_opts.ssh_connection_data or ''))
    remote_path = cli_opts.path or ''
    if action == 'open':
        print('Opening', cli_opts.path, 'from', cli_opts.hostname)
        cmd = simple_copy_command(conn_data, remote_path)
        dest = os.path.join(tempfile.mkdtemp(), os.path.basename(remote_path))
        if save_output(cmd, dest):
            return dest
        show_error('Failed to copy file from remote machine')
    elif action == 'edit':
        print('Editing', cli_opts.path, 'from', cli_opts.hostname)
        with ControlMaster(conn_data, remote_path) as master:
            if not master.download():
                show_error(f'Failed to download {remote_path}')
                return None
            mtime = os.path.getmtime(master.dest)
            print(reset_terminal(), end='', flush=True)
            editor = get_editor()
            editor_process = subprocess.Popen(editor + [master.dest])
            while editor_process.poll() is None:
                time.sleep(0.1)
                newmtime = os.path.getmtime(master.dest)
                if newmtime > mtime:
                    mtime = newmtime
                    if master.is_alive:
                        master.upload()
            print(reset_terminal(), end='', flush=True)
            if master.is_alive:
                if not master.upload(suppress_output=False):
                    show_error(f'Failed to upload {remote_path}')
            else:
                show_error(f'Failed to upload {remote_path}, SSH master process died')
    elif action == 'save':
        print('Saving', cli_opts.path, 'from', cli_opts.hostname)
        save_as(conn_data, remote_path)


@result_handler()
def handle_result(args: List[str], data: Result, target_window_id: int, boss: BossType) -> None:
    if data:
        cmd = command_for_open(boss.opts.open_url_with)
        open_cmd(cmd, data)


if __name__ == '__main__':
    main(sys.argv)
