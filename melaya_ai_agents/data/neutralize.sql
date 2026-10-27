-- deactivate the Melaya connection: a neutralized copy must never call the
-- Melaya project of the original database
-- * delete the Melaya Agent API keys (Melaya holds the original's key)
DELETE FROM res_users_apikeys
    WHERE user_id IN (
        SELECT value::integer FROM ir_config_parameter
            WHERE key = 'melaya_ai_agents.agent_user_id' AND value ~ '^[0-9]+$'
    );
-- * delete the install token and the connection
DELETE FROM ir_config_parameter
    WHERE key IN (
        'melaya_ai_agents.install_token',
        'melaya_ai_agents.token_database_uuid',
        'melaya_ai_agents.project_name',
        'melaya_ai_agents.project_url',
        'melaya_ai_agents.available_pipelines',
        'melaya_ai_agents.last_error'
    );
-- * forget which users linked their Melaya account
DELETE FROM melaya_user_link;
