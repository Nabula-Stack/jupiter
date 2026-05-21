import paramiko
import socket
import time

class ESXiConnect:
    def __init__(
        self,
        host,
        user=None,
        port=22,
        timeout=10,
        command_timeout=20,
        key_filename=None,
        key_passphrase=None,
    ):
        self.host = host
        self.client = None
        self.timeout = timeout
        self.command_timeout = command_timeout
        
        # We no longer look at settings.py or config.py.
        # We use exactly what is passed in from the Database (Host model).
        self.user = user
        self.port = port
        self.key_filename = key_filename
        self.key_passphrase = key_passphrase

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def open(self):
        has_key = bool(self.key_filename)

        # Key-only auth mode.
        if not self.user or not has_key:
            raise ConnectionError(
                f"Missing SSH credentials for host {self.host}. "
                "Set username and SSH key env vars (SSH_KEY_PATH)."
            )
        try:
            self.client = paramiko.SSHClient()
            self.client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            connect_kwargs = {
                "hostname": self.host,
                "port": self.port,
                "username": self.user,
                "timeout": self.timeout,
                "look_for_keys": False,
                "allow_agent": False,
                "banner_timeout": 20,
                "auth_timeout": 20,
                "key_filename": self.key_filename,
            }
            if self.key_passphrase:
                connect_kwargs["passphrase"] = self.key_passphrase

            self.client.connect(**connect_kwargs)
            transport = self.client.get_transport()
            if transport:
                transport.set_keepalive(30)
            return self.client
        except paramiko.AuthenticationException as e:
            self.close()
            raise ConnectionError(f"Authentication failed for {self.user}@{self.host}: {e}")
        except socket.timeout:
            self.close()
            raise ConnectionError(f"Connection timed out for {self.host}")
        except Exception as e:
            self.close()
            raise ConnectionError(f"Connection failed: {str(e)}")

    def run(self, cmd, timeout=None):
        if not self.client:
            self.open()
        else:
            transport = self.client.get_transport()
            if not transport or not transport.is_active():
                self.close()
                self.open()

        cmd_timeout = timeout if timeout is not None else self.command_timeout
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=cmd_timeout)
        channel = stdout.channel
        deadline = time.monotonic() + max(1, int(cmd_timeout))

        while not channel.exit_status_ready():
            if time.monotonic() >= deadline:
                channel.close()
                return f"Error: Command timed out after {cmd_timeout}s"
            time.sleep(0.05)

        exit_code = channel.recv_exit_status()
        if exit_code != 0:
            return f"Error: {stderr.read().decode().strip()}"
        return stdout.read().decode().strip()

    def upload_file(self, file_obj, remote_path):
        """Upload a file-like object to the remote host via SFTP."""
        if not self.client:
            self.open()
        sftp = self.client.open_sftp()
        try:
            # Ensure transfer starts at the beginning when the object supports seek.
            if hasattr(file_obj, "seek"):
                try:
                    file_obj.seek(0)
                except Exception:
                    pass

            # Stream in chunks to avoid buffering large files in memory.
            with sftp.open(remote_path, "wb") as remote_file:
                while True:
                    chunk = file_obj.read(1024 * 1024)
                    if not chunk:
                        break
                    remote_file.write(chunk)
        finally:
            sftp.close()

    def close(self):
        if self.client:
            self.client.close()
            self.client = None