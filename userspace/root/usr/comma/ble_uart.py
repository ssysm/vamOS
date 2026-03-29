#!/usr/bin/env python3
"""
BLE UART Terminal Service — Nordic UART Service (NUS)
Exposes a shell session over BLE using the standard NUS profile.

  Service UUID : 6E400001-B5A3-F393-E0A9-E50E24DCCA9E
  RX char UUID : 6E400002-B5A3-F393-E0A9-E50E24DCCA9E  (client→device, write)
  TX char UUID : 6E400003-B5A3-F393-E0A9-E50E24DCCA9E  (device→client, notify)

WARNING: this service provides an unauthenticated root shell over BLE.
         Enable only on trusted, physically-secure hardware.
"""

import asyncio
import fcntl
import os
import pty
import select
import signal
import subprocess

from bless import BlessServer, GATTAttributePermissions, GATTCharacteristicProperties

NUS_SERVICE_UUID = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_UUID      = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX_UUID      = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"

# Safe BLE notification chunk size (BLE 4.0 floor; negotiated MTU may be larger)
NOTIFY_CHUNK = 20


def _pick_term(*candidates: str) -> str:
    """Return the first terminal type whose terminfo entry exists on disk."""
    search_dirs = ["/usr/share/terminfo", "/lib/terminfo", "/etc/terminfo"]
    for term in candidates:
        for base in search_dirs:
            if os.path.exists(os.path.join(base, term[0], term)):
                return term
    return "dumb"


class ShellSession:
    def __init__(self):
        self.master_fd: int | None = None
        self._proc: subprocess.Popen | None = None

    def start(self) -> None:
        master, slave = pty.openpty()
        self.master_fd = master

        flags = fcntl.fcntl(master, fcntl.F_GETFL)
        fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        home = "/root"
        term = _pick_term("vt100")
        env = {**os.environ, "TERM": term, "HOME": home, "USER": "root"}
        self._proc = subprocess.Popen(
            ["/bin/bash", "-l"],
            stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, env=env, cwd=home,
        )
        os.close(slave)
        print(f"ble_uart: shell started (pid {self._proc.pid})")

    def write(self, data: bytes) -> None:
        if self.master_fd is not None:
            os.write(self.master_fd, data)

    def read_available(self) -> bytes:
        if self.master_fd is None:
            return b""
        try:
            r, _, _ = select.select([self.master_fd], [], [], 0)
            if r:
                return os.read(self.master_fd, 512)
        except OSError:
            pass
        return b""

    def stop(self) -> None:
        if self._proc:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                pass
            self._proc = None
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


async def pty_to_ble(server: BlessServer, session: ShellSession) -> None:
    """Poll PTY output and push it to the TX characteristic."""
    while True:
        data = session.read_available()
        if data:
            for i in range(0, len(data), NOTIFY_CHUNK):
                chunk = data[i:i + NOTIFY_CHUNK]
                server.get_characteristic(NUS_TX_UUID).value = bytearray(chunk)
                server.update_value(NUS_SERVICE_UUID, NUS_TX_UUID)
        await asyncio.sleep(0.02)


def write_request(characteristic, value: bytearray) -> None:
    """Called by bless when the client writes to the RX characteristic."""
    if characteristic.uuid.lower() == NUS_RX_UUID:
        session.write(bytes(value))


def read_request(characteristic, **_) -> bytearray:
    return characteristic.value or bytearray()


async def main() -> None:
    global session

    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def _signal_handler():
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, _signal_handler)
    loop.add_signal_handler(signal.SIGINT, _signal_handler)

    session = ShellSession()
    session.start()

    server = BlessServer(name="vamOS", loop=loop)
    server.read_request_func = read_request
    server.write_request_func = write_request

    await server.add_new_service(NUS_SERVICE_UUID)

    # TX: device → client (notify)
    await server.add_new_characteristic(
        NUS_SERVICE_UUID,
        NUS_TX_UUID,
        GATTCharacteristicProperties.notify,
        bytearray(),
        GATTAttributePermissions.readable,
    )

    # RX: client → device (write)
    await server.add_new_characteristic(
        NUS_SERVICE_UUID,
        NUS_RX_UUID,
        GATTCharacteristicProperties.write | GATTCharacteristicProperties.write_without_response,
        bytearray(),
        GATTAttributePermissions.writeable,
    )

    await server.start()
    print('ble_uart: advertising as "vamOS"')

    pty_task = asyncio.create_task(pty_to_ble(server, session))

    await stop_event.wait()

    pty_task.cancel()
    await server.stop()
    session.stop()


if __name__ == "__main__":
    asyncio.run(main())
