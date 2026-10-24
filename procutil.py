"""Starting, feeding, interrupting and stopping child processes across platforms."""

import codecs
import os
import signal
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")
READ_CHUNK = 4096


def spawn(args, cwd, shell=False):
    """Start a child with binary pipes in its own process group/session.

    The separate group lets Ctrl+C reach only the child and lets stop() take down its descendants."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"       # pipes are block-buffered otherwise, so output would arrive late
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONUTF8", "1")
    return subprocess.Popen(
        args, cwd=cwd, shell=shell, env=env, bufsize=0,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0,
        start_new_session=not IS_WINDOWS,
    )


def pump(stream, is_err, out_queue):
    """Forward a pipe to out_queue as (text, is_err) chunks until EOF.

    Reads whatever is available instead of whole lines so prompts without a newline show up,
    and decodes incrementally so invalid bytes never stall or kill the reader."""
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        while True:
            data = stream.read(READ_CHUNK)
            text = decoder.decode(data, final=not data)
            if text:
                out_queue.put((text, is_err))
            if not data:
                break
    except (OSError, ValueError):
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass


def send_line(proc, text):
    """Write a line to the child's stdin; False if it is gone."""
    try:
        proc.stdin.write((text + "\n").encode("utf-8"))
        proc.stdin.flush()
        return True
    except (OSError, ValueError, AttributeError):
        return False


def interrupt(proc):
    """Deliver a terminal-style Ctrl+C to the child's whole group."""
    try:
        if IS_WINDOWS:
            proc.send_signal(signal.CTRL_BREAK_EVENT)    # CTRL_C_EVENT is ignored in a new process group
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    except (OSError, ValueError):
        try:
            proc.send_signal(signal.SIGINT if not IS_WINDOWS else signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass


def stop(proc, force=False):
    """Terminate the child and everything it started; force escalates to a hard kill."""
    if proc.poll() is not None:
        return
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=5,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL if force else signal.SIGTERM)
        return
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        proc.kill() if force else proc.terminate()
    except OSError:
        pass
