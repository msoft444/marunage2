from .command_runner import SecureCommandRunner
from .file_ops import SafeFileOps
from .sandbox import WorkspaceSandbox
from .secret_scanner import SecretScanner

__all__ = [
    "SecretScanner",
    "WorkspaceSandbox",
    "SecureCommandRunner",
    "SecureDashboard",
    "SafeFileOps",
]


def __getattr__(name: str):
    if name == "SecureDashboard":
        from .dashboard import SecureDashboard

        return SecureDashboard
    raise AttributeError(name)