"""Secrets behind an environment: a command prints the connection URL or API
token, and the output lives in memory only.

A command that fails with one of the environment's auth markers (an expired
`infisical login`, say) raises AuthExpired, which the UI answers by running
login_command once.
"""

import subprocess

from dex.config import Environment, environment

COMMAND_TIMEOUT_SECONDS = 20
LOGIN_TIMEOUT_SECONDS = 300


class AuthExpired(Exception):
    """The login behind a secret command has lapsed."""


class CredentialsError(Exception):
    """A secret command failed or printed nothing."""


def read_secret(target: Environment) -> str:
    try:
        result = subprocess.run(target.secret_command, shell=True, capture_output=True, text=True,
                                timeout=COMMAND_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        raise CredentialsError(f"the secret command for {target.name} ran past {COMMAND_TIMEOUT_SECONDS}s") from None
    # Only stderr is quoted back: stdout may be a partial secret.
    if result.returncode != 0:
        output = result.stderr.strip()
        if any(marker.lower() in output.lower() for marker in target.auth_markers):
            raise AuthExpired(f"login expired while reading the {target.name} secret")
        raise CredentialsError(f"the secret command for {target.name} failed: {output[:400]}")
    secret = result.stdout.strip()
    if not secret:
        raise CredentialsError(f"the secret command for {target.name} printed nothing")
    return secret


def can_login(environment_name: str) -> bool:
    return bool(environment(environment_name).login_command)


def login(environment_name: str) -> None:
    """Run the environment's login_command (a browser flow, say); blocks."""
    command = environment(environment_name).login_command
    if not command:
        raise CredentialsError(f"no login_command configured for {environment_name}")
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=LOGIN_TIMEOUT_SECONDS)
    if result.returncode != 0:
        raise CredentialsError(f"login failed: {(result.stderr or result.stdout).strip()[:400]}")
