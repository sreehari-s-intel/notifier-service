"""
core/k8s_port_forward.py
Starts/stops kubectl port-forward for in-cluster Elasticsearch service access.
"""

import logging
import shlex
import socket
import subprocess
import time

logger = logging.getLogger(__name__)


class KubectlPortForward:
    def __init__(
        self,
        namespace: str,
        service_name: str,
        remote_port: int,
        local_port: int,
        kubeconfig: str | None = None,
        startup_timeout_seconds: int = 20,
    ):
        self.namespace = namespace
        self.service_name = service_name
        self.remote_port = remote_port
        self.local_port = local_port
        self.kubeconfig = kubeconfig
        self.startup_timeout_seconds = startup_timeout_seconds
        self._process: subprocess.Popen | None = None

    def start(self):
        if self._process is not None:
            return

        preferred_port = self.local_port
        candidate_ports = [preferred_port] + [self._find_free_local_port() for _ in range(4)]
        last_error: Exception | None = None

        for i, candidate_port in enumerate(candidate_ports, start=1):
            self.local_port = candidate_port
            logger.info(
                "Starting kubectl port-forward (candidate %s/%s) on localhost:%s",
                i,
                len(candidate_ports),
                candidate_port,
            )
            self._start_with_current_port()

            try:
                self._wait_until_ready()
                return
            except RuntimeError as exc:
                last_error = exc
                if self._is_bind_conflict(str(exc)) and i < len(candidate_ports):
                    logger.warning(
                        "Local port %s is busy. Retrying with another free port.",
                        candidate_port,
                    )
                    self.stop()
                    continue
                self.stop()
                raise
            except TimeoutError as exc:
                last_error = exc
                self.stop()
                raise

        raise RuntimeError(
            "Failed to start kubectl port-forward after trying multiple local ports."
        ) from last_error

    def _start_with_current_port(self):
        command = [
            "kubectl",
            "-n",
            self.namespace,
            "port-forward",
            f"svc/{self.service_name}",
            f"{self.local_port}:{self.remote_port}",
            "--address",
            "127.0.0.1",
        ]
        if self.kubeconfig:
            command[1:1] = ["--kubeconfig", self.kubeconfig]

        logger.info("Starting kubectl port-forward: %s", " ".join(shlex.quote(c) for c in command))
        self._process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )

    def _wait_until_ready(self):
        deadline = time.time() + self.startup_timeout_seconds
        last_lines: list[str] = []

        while time.time() < deadline:
            line = self._process.stdout.readline() if self._process and self._process.stdout else ""
            if line:
                line = line.strip()
                last_lines.append(line)
                last_lines = last_lines[-5:]
                logger.info("kubectl: %s", line)

                if "Forwarding from" in line:
                    logger.info(
                        "Port-forward ready: localhost:%s -> svc/%s:%s",
                        self.local_port,
                        self.service_name,
                        self.remote_port,
                    )
                    return

                if self._is_bind_conflict(line):
                    raise RuntimeError(line)

            if self._process and self._process.poll() is not None:
                tail = " | ".join(last_lines) if last_lines else "no kubectl output"
                raise RuntimeError(f"kubectl port-forward exited before startup: {tail}")

            time.sleep(0.1)

        raise TimeoutError("Timed out waiting for kubectl port-forward to become ready.")

    @staticmethod
    def _is_bind_conflict(message: str) -> bool:
        msg = message.lower()
        return "unable to listen on port" in msg or "bind:" in msg

    @staticmethod
    def _find_free_local_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def stop(self):
        if self._process is None:
            return

        if self._process.poll() is None:
            logger.info("Stopping kubectl port-forward process.")
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()

        self._process = None