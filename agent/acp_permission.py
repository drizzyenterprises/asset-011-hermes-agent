"""Route ACP session/request_permission through Hermes' approval gate.

Restores cron_mode / yolo / approvals.mode behaviour for the copilot-acp
backend so unattended runs no longer abort on ACP-native tool calls.
"""
import json


def _denied(message_id):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {"outcome": {"outcome": "cancelled"}},
    }


def _selected(message_id, option_id):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {"outcome": {"outcome": "selected", "optionId": option_id}},
    }


def _allow_option_id(options):
    if not isinstance(options, list):
        return None
    allow_once = None
    allow_other = None
    for opt in options:
        if not isinstance(opt, dict):
            continue
        opt_id = opt.get("optionId")
        if not isinstance(opt_id, str) or not opt_id:
            continue
        kind = str(opt.get("kind") or "").strip().lower()
        if kind == "allow_once":
            allow_once = allow_once or opt_id
        elif kind.startswith("allow"):
            allow_other = allow_other or opt_id
    return allow_once or allow_other


def _command_text(params):
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        return ""
    parts = []
    title = tool_call.get("title")
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())
    raw_input = tool_call.get("rawInput")
    if isinstance(raw_input, dict):
        command = raw_input.get("command")
        if isinstance(command, str) and command.strip():
            parts.append(command.strip())
        else:
            try:
                parts.append(json.dumps(raw_input, ensure_ascii=False))
            except Exception:
                pass
    return "\n".join(parts)


def resolve(message_id, params):
    """Decide an ACP permission request via Hermes' approval gate.

    Returns an ACP 'selected'(allow) reply when the same guard the
    terminal tool uses approves the command; otherwise the safe
    'cancelled' reply. Fails closed on any error.
    """
    if not isinstance(params, dict):
        return _denied(message_id)
    allow_id = _allow_option_id(params.get("options"))
    if not allow_id:
        return _denied(message_id)
    command_text = _command_text(params)
    try:
        from tools.approval import check_all_command_guards
        decision = check_all_command_guards(command_text, "local")
    except Exception:
        return _denied(message_id)
    if isinstance(decision, dict) and decision.get("approved"):
        return _selected(message_id, allow_id)
    return _denied(message_id)
