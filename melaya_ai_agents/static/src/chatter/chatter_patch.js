// Odoo 19.0 overlay of melaya_ai_agents/static/src/chatter/chatter_patch.js (20.0).
// Difference: Odoo 19 exports Chatter from @mail/chatter/web_portal/chatter
// (20 moved it to web_portal_project).
// Keep in sync: python integrations/odoo/compat/check_overlay.py
import { Chatter } from "@mail/chatter/web_portal/chatter";

import { status } from "@odoo/owl";

import { useService } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";

import { loadMelayaConfig } from "@melaya_ai_agents/core/melaya_config";

/**
 * "Melaya" button in the chatter top bar, next to Send message, Log note and
 * Activity, on saved records of the models exposed to this user. It opens the
 * same panel as Actions > Melaya (melaya.record.panel.action_open_record_panel),
 * which checks the user and the record again on the server.
 */
patch(Chatter.prototype, {
    setup() {
        super.setup(...arguments);
        this.melayaOrm = useService("orm");
        this.melayaActions = useService("action");
        Object.assign(this.state, { melayaModels: [], melayaBusy: false });
        loadMelayaConfig().then((config) => {
            if (config.enabled && config.models.length && status(this) !== "destroyed") {
                this.state.melayaModels = config.models;
            }
        });
    },

    get melayaVisible() {
        const thread = this.state.thread;
        return Boolean(
            thread &&
                Number.isInteger(thread.id) &&
                thread.id > 0 &&
                this.state.melayaModels.includes(thread.model)
        );
    },

    async onClickMelaya() {
        const thread = this.state.thread;
        if (!this.melayaVisible || this.state.melayaBusy) {
            return;
        }
        this.state.melayaBusy = true;
        try {
            const action = await this.melayaOrm.call(
                "melaya.record.panel",
                "action_open_record_panel",
                [thread.model, [thread.id]]
            );
            if (action) {
                await this.melayaActions.doAction(action);
            }
        } finally {
            if (status(this) !== "destroyed") {
                this.state.melayaBusy = false;
            }
        }
    },
});
