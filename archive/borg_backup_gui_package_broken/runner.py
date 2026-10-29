"""
Befehlsausführung für Borg Backup GUI.

Führt Borg-Befehle in Hintergrund-Threads aus und liefert
Output thread-sicher per Callback an die GUI zurück.
"""

import os
import shutil
import subprocess
import threading

BORG_BIN = shutil.which('borg') or 'borg'


class CommandRunner:
    """Führt Befehle im Thread aus und liefert Logs thread-sicher an die GUI."""

    def __init__(self, append_callback, schedule_main):
        self.append_callback = append_callback
        self.schedule_main = schedule_main
        self.process = None
        self.stop_flag = False

    def append_log(self, text):
        self.append_callback(text)

    def run(self, cmd, done_callback=None, env=None, cwd=None):
        """Befehl in Hintergrund-Thread starten (Live-Output)."""

        def target():
            result = {
                'exit_code': -1,
                'stopped': False,
                'exception': ''
            }
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    universal_newlines=True,
                    env=env if env else os.environ,
                    cwd=cwd
                )

                for line in self.process.stdout:
                    if self.stop_flag:
                        self.process.terminate()
                        result['stopped'] = True
                        break
                    self.append_log(line)

                exit_code = self.process.wait()
                result['exit_code'] = exit_code

                if exit_code == 1 and not self.stop_flag:
                    self.append_log(f'\n[WARNUNG] Befehl endete mit Code {exit_code}\n')
                elif exit_code != 0 and not self.stop_flag:
                    self.append_log(f'\n[FEHLER] Befehl endete mit Code {exit_code}\n')
                elif exit_code == 0:
                    self.append_log('\n[OK] Fertig!\n')
            except Exception as exc:
                result['exception'] = str(exc)
                self.append_log(f'\n[EXCEPTION] {exc}\n')
            finally:
                if done_callback:
                    try:
                        self.schedule_main(lambda: done_callback(result))
                    except Exception as e:
                        print(f'[DEBUG] schedule_main failed for run: {e}', flush=True)
                        done_callback(result)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def run_capture(self, cmd, done_callback=None, env=None, cwd=None, timeout=120):
        """Befehl in Hintergrund-Thread starten, stdout/stderr sammeln, kein Live-Log."""

        def target():
            result = {
                'exit_code': -1,
                'stopped': False,
                'exception': '',
                'stdout': '',
                'stderr': ''
            }
            try:
                self.process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env if env else os.environ,
                    cwd=cwd
                )
                try:
                    out, err = self.process.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.communicate()
                    result['exception'] = f'Zeitüberschreitung nach {timeout}s – Server nicht erreichbar?'
                    return
                result['stdout'] = out
                result['stderr'] = err
                result['exit_code'] = self.process.returncode
            except Exception as exc:
                result['exception'] = str(exc)
            finally:
                if done_callback:
                    try:
                        self.schedule_main(lambda: done_callback(result))
                    except Exception as e:
                        # Fallback: direkt aufrufen falls schedule_main fehlschlägt
                        print(f'[DEBUG] schedule_main failed for run_capture: {e}', flush=True)
                        done_callback(result)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread

    def stop(self):
        self.stop_flag = True
        if self.process and self.process.poll() is None:
            self.process.terminate()
