import { rpc } from "@web/core/network/rpc";

const DISABLED = Object.freeze({ enabled: false, models: [] });
let configPromise = null;

/**
 * Whether Melaya is on for this user (database connected, Melaya / User) and
 * which models show the Melaya button in the chatter. Asked once per page
 * load and shared by the chatter button and the top bar menu; every click is
 * checked again on the server. Any failure hides the Melaya entry points.
 */
export function loadMelayaConfig() {
    if (!configPromise) {
        configPromise = rpc("/melaya_ai_agents/exposed_models", {}, { silent: true })
            .then((result) => ({
                enabled: Boolean(result && result.enabled),
                models: Array.isArray(result?.models)
                    ? result.models.filter((m) => typeof m === "string")
                    : [],
            }))
            .catch(() => DISABLED);
    }
    return configPromise;
}
