"""Offline binding to PipeWire's SPA echo cancellation engine."""
import ctypes as ct
import errno
import os
import shlex
import subprocess

from _kara_aec_prototype import I16
from _kara_paths import BIN


def build(destination):
    try:
        flags = subprocess.check_output(["pkg-config", "--cflags", "--libs", "libpipewire-0.3"],
                                        text=True, stderr=subprocess.DEVNULL, timeout=10)
    except subprocess.CalledProcessError as error:
        raise FileNotFoundError("En-têtes PipeWire indisponibles.") from error
    source = BIN.parent / "native/src/_kara_aec_pipewire.c"
    subprocess.run(["cc", "-std=c17", "-D_GNU_SOURCE", "-Wall", "-Wextra", "-Werror", "-O2", "-shared",
                    "-fPIC", str(source), "-o", str(destination), *shlex.split(flags), "-lm"],
                   check=True, timeout=30)


class PipeWire:
    def __init__(self, library, render_channels):
        self.lib = ct.CDLL(library)
        self.lib.kara_pw_aec_create.argtypes = [ct.c_uint32, ct.POINTER(ct.c_void_p)]
        self.lib.kara_pw_aec_create.restype = ct.c_int
        self.lib.kara_pw_aec_destroy.argtypes = [ct.c_void_p]
        self.lib.kara_pw_aec_destroy.restype = None
        self.lib.kara_pw_aec_step.argtypes = [ct.c_void_p, I16, ct.c_size_t, I16, ct.c_size_t, I16, ct.c_size_t]
        self.lib.kara_pw_aec_step.restype = ct.c_int
        self.state = ct.c_void_p()
        status = self.lib.kara_pw_aec_create(render_channels, ct.byref(self.state))
        if status < 0:
            if -status in (errno.ENOENT, errno.ENOTSUP):
                raise OSError(-status, "Greffon SPA Echo Cancel ou interface init2 indisponible.")
            raise RuntimeError(f"Initialisation PipeWire SPA ({os.strerror(-status)}).")

    def step(self, render, capture, out, _delay_ms):
        status = self.lib.kara_pw_aec_step(self.state, render, render.size, capture, capture.size, out, out.size)
        if status < 0:
            raise RuntimeError(f"Trame PipeWire SPA refusée ({os.strerror(-status)}).")

    def close(self):
        if self.state:
            self.lib.kara_pw_aec_destroy(self.state)
            self.state = None
