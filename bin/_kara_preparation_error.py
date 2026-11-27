"""Diagnostics partagés par les moteurs et la préparation Kara."""


class PreparationError(RuntimeError):
    def __init__(self, message, *, phase=None, exit_code=None, protection=None):
        super().__init__(message)
        self.phase = phase
        self.exit_code = exit_code
        self.protection = protection


def error_details(error, phase, *, exit_code=None):
    # No child has an exit status when a guard refuses admission before spawn.
    code = getattr(error, "exit_code", None)
    if code is None:
        code = getattr(error, "returncode", None)
    return {"phase": getattr(error, "phase", None) or phase, "message": str(error),
            "exit_code": code if code is not None else exit_code,
            "protection": getattr(error, "protection", None)}
