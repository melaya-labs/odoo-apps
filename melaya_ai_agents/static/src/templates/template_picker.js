// Odoo 19.0 overlay of melaya_ai_agents/static/src/templates/template_picker.js (20.0).
// Difference: OWL 2 (Odoo 19) instead of OWL 3: useState for proxy, static
// props for useProps, and the dialog service instead of the 20 DialogPlugin.
// Keep in sync: python integrations/odoo/compat/check_overlay.py
import { Component, onWillStart, useState } from "@odoo/owl";
import { Dialog } from "@web/core/dialog/dialog";
import { _t } from "@web/core/l10n/translation";
import { rpc } from "@web/core/network/rpc";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const ROUTE = "/melaya_ai_agents/templates/";

/**
 * Add from templates: Melaya's Odoo templates, grouped by Odoo app. The
 * administrator ticks some and clicks Add; Odoo asks Melaya to create each
 * pipeline in the project, puts it on the exposed models it works on and
 * saves them to Melaya (melaya.template.picker, admin only on the server).
 */
export class MelayaTemplatePicker extends Component {
    static template = "melaya_ai_agents.TemplatePicker";
    static components = { Dialog };

    static props = {
        close: Function,
        model: { type: String, optional: true },
        reload: { type: Boolean, optional: true },
    };
    static defaultProps = { model: "", reload: false };

    setup() {
        this.title = String(_t("Add from templates"));
        this.actions = useService("action");
        this.state = useState({
            loading: true,
            busy: false,
            error: "",
            loadFailed: false,
            templates: [],
            model: "",
            modelLabel: "",
            projectName: "",
            showAll: !this.props.model,
            selected: [],
            result: null,
        });
        onWillStart(() => this.load());
    }

    async load() {
        this.state.loading = true;
        this.state.error = "";
        try {
            const result = await rpc(ROUTE + "list", { model: this.props.model || "" });
            if (!result.ok) {
                this.state.error = result.error || String(_t("Something went wrong. Try again."));
                this.state.loadFailed = true;
                return;
            }
            this.state.templates = result.templates || [];
            this.state.model = result.model || "";
            this.state.modelLabel = result.model_label || "";
            this.state.projectName = result.project_name || "";
            if (!this.state.model) {
                this.state.showAll = true;
            }
        } catch {
            this.state.error = String(_t("Something went wrong. Try again."));
            this.state.loadFailed = true;
        } finally {
            this.state.loading = false;
        }
    }

    get visible() {
        const model = this.state.model;
        if (this.state.showAll || !model) {
            return this.state.templates;
        }
        return this.state.templates.filter((tpl) => tpl.models.includes(model));
    }

    /** Templates in their app group, in the order the server sorted them. */
    get groups() {
        const groups = [];
        for (const tpl of this.visible) {
            let group = groups.find((g) => g.name === tpl.group);
            if (!group) {
                group = { name: tpl.group, key: `g${groups.length}`, templates: [] };
                groups.push(group);
            }
            group.templates.push(tpl);
        }
        return groups;
    }

    canPick(tpl) {
        return tpl.state === "new" || tpl.state === "in_project";
    }

    isSelected(tpl) {
        return this.state.selected.includes(tpl.id);
    }

    onToggle(tpl, ev) {
        const on = ev.target.checked;
        const rest = this.state.selected.filter((id) => id !== tpl.id);
        this.state.selected = on && this.canPick(tpl) ? [...rest, tpl.id] : rest;
    }

    onShowAll() {
        this.state.showAll = true;
    }

    onShowModel() {
        this.state.showAll = false;
        const shown = new Set(this.visible.map((tpl) => tpl.id));
        this.state.selected = this.state.selected.filter((id) => shown.has(id));
    }

    get addLabel() {
        const count = this.state.selected.length;
        return count ? _t("Add %s", count) : _t("Add");
    }

    templateName(id) {
        const tpl = this.state.templates.find((x) => x.id === id);
        return tpl ? tpl.name : id;
    }

    async onAdd() {
        if (!this.state.selected.length || this.state.busy) {
            return;
        }
        this.state.busy = true;
        this.state.error = "";
        try {
            const result = await rpc(ROUTE + "add", { template_ids: this.state.selected });
            if (!result.ok) {
                this.state.error = result.error || String(_t("Something went wrong. Try again."));
                return;
            }
            this.state.result = result;
        } catch {
            this.state.error = String(_t("Something went wrong. Try again."));
        } finally {
            this.state.busy = false;
        }
    }

    async onDone() {
        const changed = Boolean(this.state.result && this.state.result.added.length);
        this.props.close();
        if (changed && this.props.reload) {
            await this.actions.doAction({ type: "ir.actions.client", tag: "soft_reload" });
        }
    }
}

/**
 * Client action returned by melaya.template.picker.action_open (Exposed
 * models, Settings) and opened by the record panel. Like the record panel, it
 * opens a dialog over the current screen through the Odoo 19 dialog service.
 */
function openMelayaTemplatePicker(env, action) {
    const params = action.params || {};
    env.services.dialog.add(MelayaTemplatePicker, {
        model: String(params.model || ""),
        reload: Boolean(params.reload),
    });
}

registry.category("actions").add("melaya_ai_agents.template_picker", openMelayaTemplatePicker);
