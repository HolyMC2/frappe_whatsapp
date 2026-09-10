// Copyright (c) 2025, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.ui.form.on("WhatsApp Account", {
	refresh(frm) {
		if (!frm.is_new()) {
			frm.add_custom_button(__("Subscribe App to Webhooks"), () => {
				frappe.confirm(
					__("Subscribe this app to webhooks for WhatsApp Business Account {0}?", [
						frm.doc.business_id || frm.doc.account_name,
					]),
					() => {
						frm.call({
							doc: frm.doc,
							method: "subscribe_app",
							freeze: true,
							freeze_message: __("Subscribing app to webhooks..."),
							callback: (r) => {
								if (!r.exc) {
									frappe.show_alert({
										message: __("App subscribed to webhooks"),
										indicator: "green",
									});
								}
							},
						});
					}
				);
			});
		}
	},
});

// Health is a read broker over the saved account. Opening it never calls Meta.
(() => {
	const manager = () => (frappe.user_roles || []).includes("System Manager");
	const clean = (frm, pinned) => manager() && !frm.is_new() && !frm.is_dirty()
		&& (!pinned || (frm.doc.name === pinned.name && frm.doc.modified === pinned.modified));
	const esc = value => frappe.utils.escape_html(String(value ?? "—"));
	const states = {
		not_checked: "Not checked", verified: "Phone metadata verified for this check",
		missing_configuration: "Required phone check configuration is missing or invalid",
		demo_no_remote: "Demo: no remote check", inactive: "Account is not Active and Live",
		authentication_failed: "Authentication failed", permission_denied: "Permission denied",
		rate_limited: "Rate limited", unavailable: "Provider check unavailable or response invalid",
	};
	function summary(result) {
		const row = (label, value) => `<tr><th>${esc(__(label))}</th><td>${esc(value)}</td></tr>`;
		let html = `<p>${esc(__("Configuration presence is local evidence. This view does not prove subscriptions or end-to-end webhook delivery."))}</p><table class="table table-bordered">`;
		html += row("Account", result.account_name) + row("Mode", result.mode) + row("Status", result.status);
		const labels = {token: "Access token", app_secret: "App secret", app_id: "App ID", business_id: "WABA ID", phone_id: "Phone ID", webhook_verify_token: "Webhook verify token", version: "Graph version"};
		for (const [key, label] of Object.entries(labels)) html += row(label, __(result.configuration?.[key] || "unknown"));
		for (const [key, label] of [["phone_receipts", "Phone receipts"], ["waba_receipts", "WABA receipts"]]) {
			const counts = result[key];
			html += row(label, counts?.available ? __("Total {0}; backlog {1}; failed {2}; last received {3}", [counts.total, counts.backlog, counts.failed, counts.last_received_at || "—"]) : __("Unavailable"));
		}
		const outbox = result.outbox;
		html += row("Native outbound intents", outbox?.available ? __("Backlog {0}; failed {1}; Unknown {2}", [outbox.backlog, outbox.failed, outbox.unknown]) : __("Unavailable"));
		const check = result.check || {};
		html += row("Current phone check", __(states[check.state] || "Unknown"));
		html += row("Quality returned by this check", check.quality_rating || __("Unknown"));
		html += row("Check source", check.source) + row("Checked at", check.state === "not_checked" ? __("Not checked") : check.checked_at);
		html += row("Subscriptions", __("Unknown")) + row("End-to-end webhook delivery", __("Unknown")) + "</table>";
		html += `<p>${esc(__("Provider results are temporary and are not saved. A successful metadata read does not authorize sending."))}</p>`;
		if ((result.waba_observations || []).length) {
			html += `<p>${esc(__("Recent WABA observations: retained evidence only, unsupported or order unverified; phone mapping unverified."))}</p><ul>`;
			for (const item of result.waba_observations) html += `<li>${esc(item.event_type)} · ${esc(item.received_at)} · ${esc(item.receipt_state)} · ${esc(item.source)}</li>`;
			html += "</ul>";
		}
		html += `<p>${esc(__("Complete missing configuration, save and reopen Health. For authentication or permission errors, review the token in Meta App Dashboard. Retry rate-limited checks later. Review failed receipts and Unknown intents individually; Unknown does not mean safe to resend."))}</p>`;
		if (result.phone_receipts?.available) html += `<button type="button" class="btn btn-default btn-sm" data-health-route="phone">${esc(__("Open phone receipts"))}</button> `;
		if (result.waba_receipts?.available) html += `<button type="button" class="btn btn-default btn-sm" data-health-route="waba">${esc(__("Open WABA receipts"))}</button> `;
		if (result.outbox?.available) {
			html += `<p>${esc(__("Open customer conversations and use its Envíos panel to review authorized sends."))}</p>`;
			html += `<button type="button" class="btn btn-default btn-sm" data-health-route="outbox">${esc(__("Open customer conversations"))}</button>`;
		}
		return html;
	}
	async function open_health(frm) {
		if (!clean(frm)) return frappe.msgprint(__("Save and reload the account before opening Health."));
		const pinned = {name: frm.doc.name, modified: frm.doc.modified};
		let dialog, result, busy = false;
		async function load(method) {
			if (busy) return;
			if (!clean(frm, pinned)) return frappe.msgprint(__("The account changed. Close Health, save and reload it."));
			busy = true;
			try {
				const response = await frappe.call({method: `frappe_whatsapp.account_health.${method}`,
					args: {account_name: pinned.name, expected_modified: pinned.modified}, freeze: true,
					freeze_message: __(method === "check_phone" ? "Checking phone metadata..." : "Reading local health...")});
				const next = response.message;
				if (!clean(frm, pinned) || !next || next.account_name !== pinned.name || next.account_modified !== pinned.modified) {
					frappe.msgprint(__("Health scope changed. Reload the account."));
					return;
				}
				result = next;
				if (!dialog) {
					dialog = new frappe.ui.Dialog({title: __("WhatsApp Account Health"), size: "large",
						fields: [{fieldtype: "HTML", fieldname: "summary"}],
						primary_action_label: __("Check phone metadata"), primary_action: () => load("check_phone"),
						secondary_action_label: __("Refresh local evidence"), secondary_action: () => load("get_health")});
					dialog.fields_dict.summary.$wrapper.on("click", "[data-health-route]", event => {
						if (!clean(frm, pinned)) return frappe.msgprint(__("Reload the account before opening its evidence."));
						const kind = event.currentTarget.dataset.healthRoute, scope = result.scope || {};
						if (!["phone", "waba", "outbox"].includes(kind)) return;
						const account_id = kind === "waba" ? scope.business_id : scope.phone_id;
						if (!account_id || (kind !== "outbox" && !scope.app_id)) return;
						dialog.hide();
						if (kind === "outbox") {
							frappe.route_options = {provider: "WhatsApp", account_id};
							frappe.set_route("customer-conversations");
						} else {
							frappe.set_route("List", "Meta Webhook Receipt", {provider: "WhatsApp", account_id, app_id: scope.app_id});
						}
					});
				}
				dialog.fields_dict.summary.$wrapper.html(summary(result));
				dialog.show();
			} finally {busy = false;}
		}
		await load("get_health");
	}
	frappe.ui.form.on("WhatsApp Account", {refresh(frm) {
		if (manager() && !frm.is_new()) frm.add_custom_button(__("Health"), () => open_health(frm));
	}});
})();
