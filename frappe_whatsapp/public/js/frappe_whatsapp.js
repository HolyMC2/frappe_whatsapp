$(document).on('app_ready', function () {
	// waiting for page to load completely
	frappe.router.on("change", () => {
		var route = frappe.get_route();
		// every form's menu offers a template send (documents use their owning app's sheet)
		if (route && route[0] == "Form") {
			frappe.ui.form.on(route[1], {
				refresh: function (frm) {
					// Label kept: doco's erp_experience.js finds this item by __("Send To Whatsapp").
					frm.page.add_menu_item(__("Send To Whatsapp"), function () {
						var dialog = new frappe.ui.Dialog({
							'fields': [
								{ 'fieldname': 'ht', 'fieldtype': 'HTML' },
								{ 'label': __('Plantilla'), 'fieldname': 'template', 'reqd': 1, 'fieldtype': 'Link', 'options': 'WhatsApp Templates' },
								{ 'label': __('Contacto'), 'fieldname': 'contact', 'reqd': 1, 'fieldtype': 'Link', 'options': 'Contact', change() {
									let contact_name = dialog.get_value('contact');
									if (!contact_name) {
										dialog.set_value('mobile_no', '');
										return;
									}
									frappe.call({
										method: 'frappe.client.get_value',
										args: {
											doctype: 'Contact',
											filters: { name: contact_name },
											fieldname: ['mobile_no']
										},
										callback: function (r) {
											if (r.message && r.message.mobile_no) {
												dialog.set_value('mobile_no', r.message.mobile_no);
											} else {
												dialog.set_value('mobile_no', '');
												frappe.msgprint(__('Este contacto no tiene celular. Agrégalo en el contacto o escribe el número.'));
											}
										}
									});
								}},
								{ 'label': __('Celular'), 'fieldname': 'mobile_no', 'fieldtype': 'Data' },
							],
							'primary_action_label': __('Enviar'),
							'title': __('Enviar plantilla de WhatsApp'),
							primary_action: function () {
								var values = dialog.get_values();
								if (!values) {
									return;
								}
								// The comment is written only after the server accepted the
								// send, and it says what actually happened (queued or sent).
								frappe.call({
									method: "frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.send_template",
									args: {
										to: values.mobile_no,
										template: values.template,
										reference_doctype: frm.doc.doctype,
										reference_name: frm.doc.name
									},
									freeze: true,
									callback: (r) => {
										var queued = r.message && r.message.status === "Queued";
										var text = queued
											? __("En cola para {0}. El estado se actualiza en la conversación.", [values.mobile_no])
											: __("Enviado a {0}.", [values.mobile_no]);
										frappe.show_alert({ message: text, indicator: queued ? "blue" : "green" });
										frappe.call({
											method: "frappe.desk.form.utils.add_comment",
											args: {
												reference_doctype: frm.doc.doctype,
												reference_name: frm.doc.name,
												content: __("WhatsApp ({0}) a {1}: plantilla {2}", [queued ? __("en cola") : __("enviado"), values.mobile_no, values.template]),
												comment_email: frappe.session.user,
												comment_by: frappe.session.user_fullname
											},
										});
										dialog.hide();
									}
								});
							},
							no_submit_on_enter: true,
						});
						let template = dialog.fields_dict.template;
	                    if (template) {
	                        // Dynamically set the get_query function for the user field
	                        template.get_query = function() {
	                            return {
	                                filters: { "for_doctype": frm.doc.doctype },
	                                doctype: "WhatsApp Templates"
	                            };
	                        };
	                        // Refresh the field to apply the new query
	                        template.refresh();
	                    }
						dialog.show();
					});
				}
			});
		};
	})
});