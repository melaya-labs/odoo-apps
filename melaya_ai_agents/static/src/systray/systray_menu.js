// Odoo 19.0 overlay of melaya_ai_agents/static/src/systray/systray_menu.js (20.0).
// Difference: OWL 2 (Odoo 19) has no proxy(); the state is a useState.
// Keep in sync: python integrations/odoo/compat/check_overlay.py
import { Component, onMounted, onWillUnmount, status, useState } from "@odoo/owl";
import { Dropdown } from "@web/core/dropdown/dropdown";
import { useDropdownState } from "@web/core/dropdown/dropdown_hooks";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

import { loadMelayaConfig } from "@melaya_ai_agents/core/melaya_config";
import { statusLabel } from "@melaya_ai_agents/record_panel/record_panel";

// Odoo's own copy of the user's runs (no call to Melaya): refreshed when the
// menu opens and every two minutes while the tab is visible.
const REFRESH_MS = 2 * 60 * 1000;

/**
 * Melaya logo in the top bar for Melaya / User members of a connected
 * database: the user's 10 most recent runs started from Odoo and a counter
 * of the runs waiting for their approval in Melaya.
 */
export class MelayaSystray extends Component {
    static template = "melaya_ai_agents.Systray";
    static components = { Dropdown };

    setup() {
        this.actions = useService("action");
        this.dropdown = useDropdownState();
        this.state = useState({
            enabled: false,
            loading: false,
            loaded: false,
            error: false,
            runs: [],
            approvalCount: 0,
        });
        this.timer = null;
        onMounted(async () => {
            // Not in onWillStart: the top bar never waits for Melaya.
            const config = await loadMelayaConfig();
            if (!config.enabled || status(this) === "destroyed") {
                return;
            }
            this.state.enabled = true;
            await this.load();
            this.timer = setInterval(() => {
                if (!document.hidden) {
                    this.load();
                }
            }, REFRESH_MS);
        });
        onWillUnmount(() => clearInterval(this.timer));
    }

    get togglerLabel() {
        if (this.state.approvalCount) {
            return _t("Melaya: %(count)s waiting for your approval", {
                count: this.state.approvalCount,
            });
        }
        return _t("Melaya: your recent runs");
    }

    get waitingLabel() {
        return _t("%(count)s waiting for your approval", { count: this.state.approvalCount });
    }

    statusLabel(run) {
        if (run.state === "stale") {
            return _t("Check in Melaya");
        }
        return statusLabel(run.status);
    }

    badgeClass(run) {
        if (run.state === "approval") {
            return "text-bg-warning";
        }
        if (run.state === "running") {
            return "text-bg-info";
        }
        if (run.state === "done" && ["failed", "error"].includes(run.status)) {
            return "text-bg-danger";
        }
        if (run.state === "done" && ["success", "completed", "succeeded", "done", "finished"].includes(run.status)) {
            return "text-bg-success";
        }
        return "text-bg-secondary";
    }

    async load() {
        if (this.state.loading) {
            return;
        }
        this.state.loading = true;
        try {
            const result = await rpc("/melaya_ai_agents/runs/recent", {}, { silent: true });
            if (status(this) === "destroyed") {
                return;
            }
            if (result && result.ok) {
                this.state.runs = Array.isArray(result.runs) ? result.runs : [];
                this.state.approvalCount = Number(result.approval_count) || 0;
                this.state.error = false;
            } else {
                this.state.error = true;
            }
        } catch {
            this.state.error = true;
        } finally {
            this.state.loading = false;
            this.state.loaded = true;
        }
    }

    onBeforeOpen() {
        return this.load();
    }

    async openRecord(run) {
        this.dropdown.close();
        await this.actions.doAction({
            type: "ir.actions.act_window",
            res_model: run.model,
            res_id: run.res_id,
            views: [[false, "form"]],
            target: "current",
        });
    }
}

registry.category("systray").add("melaya_ai_agents.systray", { Component: MelayaSystray }, { sequence: 25 });
