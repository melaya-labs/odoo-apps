// Odoo 19.0 overlay of melaya_ai_agents/static/src/record_panel/record_panel.js (20.0).
// Difference: OWL 2 (Odoo 19) instead of OWL 3: useState for proxy, static
// props for useProps, and the dialog service instead of the 20 DialogPlugin.
// Keep in sync: python integrations/odoo/compat/check_overlay.py
import { Component, onMounted, onWillStart, onWillUnmount, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const ROUTE = "/melaya_ai_agents/panel/";
// Run status polling: back off while nothing changes, reset on a change,
// stop after 30 minutes without a change (Melaya allows 30 status calls per
// user and minute; this stays far below).
const POLL_DELAYS_MS = [4000, 8000, 15000, 30000];
const POLL_STOP_MS = 30 * 60 * 1000;
const STARTING_REFRESH_MS = 5000;
const MAX_BRIEF = 6000;
export const FINISHED = new Set([
    "completed", "succeeded", "success", "done", "finished",
    "failed", "error", "cancelled", "canceled", "rejected", "expired",
]);

export function statusLabel(status) {
    const labels = {
        started: _t("Started"),
        starting: _t("Starting"),
        queued: _t("Queued"),
        pending: _t("Queued"),
        running: _t("Running"),
        approval_needed: _t("Approval needed"),
        awaiting_approval: _t("Approval needed"),
        waiting_approval: _t("Approval needed"),
        completed: _t("Completed"),
        succeeded: _t("Completed"),
        success: _t("Completed"),
        done: _t("Completed"),
        finished: _t("Completed"),
        failed: _t("Failed"),
        error: _t("Failed"),
        cancelled: _t("Cancelled"),
        canceled: _t("Cancelled"),
        rejected: _t("Rejected"),
        expired: _t("Expired"),
        unknown: _t("Unknown"),
    };
    return labels[status] || status || _t("Unknown");
}

/**
 * Open a tab synchronously on the click (so popup blockers allow it), then
 * point it at the URL once the server answered. The opener link is cut.
 */
function openPendingTab() {
    const tab = window.open("", "_blank");
    if (tab) {
        tab.opener = null;
    }
    return tab;
}

function sendTab(tab, url) {
    if (!url) {
        if (tab) {
            tab.close();
        }
        return;
    }
    if (tab) {
        tab.location.href = url;
    } else {
        window.open(url, "_blank", "noopener");
    }
}

export class MelayaRecordPanel extends Component {
    static template = "melaya_ai_agents.RecordPanel";
    static components = { Dialog };

    static props = {
        close: Function,
        model: String,
        recordId: Number,
        recordName: { type: String, optional: true },
    };
    static defaultProps = { recordName: "" };

    setup() {
        this.title = String(_t("Melaya"));
        this.maxBrief = MAX_BRIEF;
        this.state = useState({
            loading: true,
            busy: false,
            error: "",
            notice: "",
            reasonMessage: "",
            pollStopped: false,
            linked: false,
            linkOpened: false,
            canAsk: false,
            canRun: false,
            pipelines: [],
            runs: [],
            pipeline: "",
            brief: "",
            recordName: this.props.recordName || "",
            confirmUnlink: false,
            canAddTemplates: false,
        });
        this.actions = useService("action");
        this.pollTimer = null;
        this.refreshTimer = null;
        this.pollStep = 0;
        this.lastChange = Date.now();
        this.unmounted = false;
        onWillStart(() => this.loadCard());
        onMounted(() => this.schedulePoll());
        onWillUnmount(() => {
            this.unmounted = true;
            clearTimeout(this.pollTimer);
            clearTimeout(this.refreshTimer);
        });
    }

    get params() {
        return { model: this.props.model, record_id: this.props.recordId };
    }

    get activeRunIds() {
        return this.state.runs
            .filter((run) => !FINISHED.has(run.status))
            .map((run) => run.run_id)
            .slice(0, 20);
    }

    statusLabel(status) {
        return statusLabel(status);
    }

    isFinished(status) {
        return FINISHED.has(status);
    }

    formatDate(value) {
        const date = value ? new Date(value) : null;
        return date && !Number.isNaN(date.getTime()) ? date.toLocaleString() : value || "";
    }

    async call(endpoint, params = {}) {
        return rpc(ROUTE + endpoint, { ...this.params, ...params });
    }

    showError(result) {
        this.state.error = result.error || String(_t("Something went wrong. Try again."));
    }

    // ------------------------------------------------------------------
    // Polling
    // ------------------------------------------------------------------

    /** Something changed (a run started or a status moved): poll fast again. */
    resetPolling() {
        this.pollStep = 0;
        this.lastChange = Date.now();
        if (this.state.pollStopped) {
            this.state.pollStopped = false;
        }
        this.schedulePoll();
    }

    schedulePoll() {
        clearTimeout(this.pollTimer);
        if (this.unmounted) {
            return;
        }
        if (Date.now() - this.lastChange > POLL_STOP_MS) {
            this.state.pollStopped = this.activeRunIds.length > 0;
            return;
        }
        const delay = POLL_DELAYS_MS[Math.min(this.pollStep, POLL_DELAYS_MS.length - 1)];
        this.pollTimer = setTimeout(() => this.pollRuns(), delay);
    }

    async pollRuns() {
        const runIds = this.activeRunIds;
        if (!runIds.length || document.hidden) {
            // Nothing to follow, or the tab is hidden: check again later, without a call.
            this.pollStep += 1;
            this.schedulePoll();
            return;
        }
        let changed = false;
        try {
            const result = await this.call("runs_status", { run_ids: runIds });
            if (result.ok) {
                for (const update of result.runs) {
                    const run = this.state.runs.find((r) => r.run_id === update.run_id);
                    if (run && run.status !== update.status) {
                        run.status = update.status;
                        changed = true;
                    }
                    if (run && update.url) {
                        run.url = update.url;
                    }
                }
            }
        } catch {
            // Network hiccup: the next tick tries again.
        }
        if (changed) {
            this.pollStep = 0;
            this.lastChange = Date.now();
        } else {
            this.pollStep += 1;
        }
        this.schedulePoll();
    }

    // ------------------------------------------------------------------
    // Card, link, ask, run
    // ------------------------------------------------------------------

    async loadCard() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const result = await this.call("card");
            if (!result.ok) {
                if (result.code === "not_linked") {
                    this.state.linked = false;
                } else {
                    this.showError(result);
                }
                return;
            }
            this.state.linked = result.linked;
            this.state.canAsk = result.can_ask;
            this.state.canRun = result.can_run;
            this.state.reasonMessage = result.reason_message || "";
            this.state.pipelines = result.pipelines;
            this.state.runs = result.runs;
            this.state.recordName = result.record_name || this.state.recordName;
            this.state.canAddTemplates = Boolean(result.can_add_templates);
            if (!this.state.pipeline && result.pipelines.length) {
                this.state.pipeline = result.pipelines[0].id;
            }
        } catch {
            this.showError({});
        } finally {
            this.state.loading = false;
        }
    }

    async onLink() {
        const tab = openPendingTab();
        this.state.busy = true;
        this.state.error = "";
        try {
            const result = await this.call("link_start");
            if (!result.ok) {
                sendTab(tab, null);
                this.showError(result);
                return;
            }
            sendTab(tab, result.url);
            this.state.linkOpened = true;
            this.state.notice = String(_t("Finish linking in the Melaya tab, then click I have linked my account."));
        } catch {
            sendTab(tab, null);
            this.showError({});
        } finally {
            this.state.busy = false;
        }
    }

    async onLinkDone() {
        this.state.notice = "";
        await this.loadCard();
    }

    async onAsk() {
        const tab = openPendingTab();
        this.state.busy = true;
        this.state.error = "";
        try {
            const result = await this.call("ask");
            if (!result.ok) {
                sendTab(tab, null);
                this.showError(result);
                return;
            }
            sendTab(tab, result.url);
        } catch {
            sendTab(tab, null);
            this.showError({});
        } finally {
            this.state.busy = false;
        }
    }

    onPipelineChange(ev) {
        this.state.pipeline = ev.target.value;
    }

    onBriefInput(ev) {
        this.state.brief = ev.target.value.slice(0, MAX_BRIEF);
    }

    async onRun() {
        if (!this.state.pipeline || this.state.busy) {
            return;
        }
        this.state.busy = true;
        this.state.error = "";
        this.state.notice = "";
        try {
            const chosen = this.state.pipelines.find((p) => p.id === this.state.pipeline);
            const result = await this.call("run", {
                pipeline: this.state.pipeline,
                pipeline_name: chosen ? chosen.name : "",
                brief: this.state.brief,
            });
            if (!result.ok) {
                this.showError(result);
                return;
            }
            this.state.brief = "";
            if (result.starting) {
                // Melaya records the run once it starts: refresh the card shortly.
                this.state.notice = result.message;
                clearTimeout(this.refreshTimer);
                this.refreshTimer = setTimeout(async () => {
                    if (!this.unmounted) {
                        await this.loadCard();
                        this.resetPolling();
                    }
                }, STARTING_REFRESH_MS);
                return;
            }
            this.state.runs.unshift(result.run);
            this.state.notice = String(_t("Run started. Its status updates below while this window is open."));
            this.resetPolling();
        } catch {
            this.showError({});
        } finally {
            this.state.busy = false;
        }
    }

    /** Administrators: Add from templates, filtered to this kind of record. */
    async onAddTemplates() {
        const model = this.props.model;
        this.props.close();
        await this.actions.doAction({
            type: "ir.actions.client",
            tag: "melaya_ai_agents.template_picker",
            params: { model, reload: false },
        });
    }

    onAskUnlink() {
        this.state.confirmUnlink = true;
    }

    onKeepLink() {
        this.state.confirmUnlink = false;
    }

    async onUnlink() {
        this.state.busy = true;
        try {
            const result = await this.call("unlink");
            if (!result.ok) {
                this.showError(result);
                return;
            }
            this.state.confirmUnlink = false;
            this.state.linked = false;
            this.state.linkOpened = false;
            this.state.runs = [];
            this.state.pipelines = [];
            this.state.reasonMessage = "";
            this.state.notice = String(_t("Your Melaya account is no longer linked to this Odoo user."));
        } catch {
            this.showError({});
        } finally {
            this.state.busy = false;
        }
    }
}

/**
 * Client action returned by the bound "Melaya" server action. It opens the
 * panel in a dialog (Odoo 19 dialog service) and stays on the current record.
 */
function openMelayaRecordPanel(env, action) {
    const params = action.params || {};
    env.services.dialog.add(MelayaRecordPanel, {
        model: String(params.model || ""),
        recordId: Number(params.record_id),
        recordName: String(params.record_name || ""),
    });
}

registry.category("actions").add("melaya_ai_agents.record_panel", openMelayaRecordPanel);
