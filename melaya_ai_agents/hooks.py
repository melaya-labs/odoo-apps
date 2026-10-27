import logging

_logger = logging.getLogger(__name__)


def _best_effort(env, label, func):
    try:
        with env.cr.savepoint():
            func()
    except Exception:  # noqa: BLE001 (an uninstall must never be blocked by Melaya)
        _logger.warning("Melaya uninstall step failed: %s", label)


def uninstall_hook(env):
    """Best effort: revoke the install in Melaya (never from a database copy),
    delete the Melaya Agent's API keys and archive it, remove the bound
    "Melaya" actions and delete every melaya_ai_agents.* system parameter."""
    client = env["melaya.client"]
    if client._is_connected():
        _best_effort(env, "disconnect", lambda: client._disconnect(reason="uninstall", as_addon=True))
    _best_effort(env, "delete Melaya Agent API keys", client._revoke_agent_keys)
    _best_effort(env, "archive Melaya Agent", client._archive_agent)
    _best_effort(env, "remove Melaya actions", lambda: env["melaya.exposed.model"].sudo().search([])._remove_bindings())
    _best_effort(env, "delete Melaya parameters", client._delete_all_params)
