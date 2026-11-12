"""Reading the effective Typer/Click surface of the CLI, shared by the matrix tests (moved out by REF-20)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import typer
from typer.main import get_command

from ayvu.interfaces.cli.app import app


def _subcommands(command: Any) -> dict[str, Any] | None:
    commands = getattr(command, "commands", None)
    if commands is None:
        return None
    assert isinstance(commands, dict), "command children must be stored by name"
    return commands


def normalized_cli_contract():
    """Effective Click/Typer metadata, including generated options and defaults (#132/#158)."""
    from enum import Enum
    def value(item):
        if isinstance(item, Enum):
            return item.value
        if isinstance(item, Path):
            return str(item)
        if isinstance(item, (tuple, list)):
            return [value(part) for part in item]
        return item
    def command_record(command):
        parameters = []
        for parameter in command.get_params(typer.Context(command)):
            parameter_type = parameter.type
            parameters.append({
                'name': parameter.name, 'kind': parameter.param_type_name,
                'opts': list(parameter.opts), 'secondary_opts': list(getattr(parameter, 'secondary_opts', ())),
                'required': parameter.required, 'default': value(parameter.default),
                'nargs': parameter.nargs, 'multiple': getattr(parameter, 'multiple', False),
                'envvar': parameter.envvar, 'type': parameter_type.name,
                'choices': value(getattr(parameter_type, 'choices', None)),
                'is_flag': getattr(parameter, 'is_flag', False),
                'help': getattr(parameter, 'help', None),
            })
        return {'parameters': parameters, 'help': command.help,
                'commands': {name: command_record(child) for name, child in sorted((_subcommands(command) or {}).items())}}
    return command_record(get_command(app))
