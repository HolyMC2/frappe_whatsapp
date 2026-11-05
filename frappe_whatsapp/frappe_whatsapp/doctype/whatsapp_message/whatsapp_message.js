// Copyright (c) 2022, Shridhar Patil and contributors
// For license information, please see license.txt

frappe.ui.form.on('WhatsApp Message', {
	onload: function(frm) {
		// The account's switch rides __onload (WhatsAppMessage.onload): most staff can't read
		// WhatsApp Account. Opening the form marks the message read without a dialog.
		if (auto_read_receipt(frm) && frm.doc.type === "Incoming" && frm.doc.status !== "marked as read" && frm.doc.message_id) {
			send_read_receipt(frm, true);
		}
	},
	refresh: function(frm) {
		if (frm.doc.type == 'Incoming'){
			frm.add_custom_button(__("Reply"), function(){
				frappe.new_doc("WhatsApp Message", {"to": frm.doc.from});

			});
		}

		// add custom button to send read receipt
		add_mark_as_read(frm);
	}
});

// custom button
function add_mark_as_read(frm){
	if(frm.doc.type === "Outgoing" || frm.doc.status == "marked as read" || !frm.doc.message_id)
		return
	
	if (auto_read_receipt(frm)) return; // opening the form already marked it

	frm.add_custom_button(__('Mark as read'), function(){
		send_read_receipt(frm, false);
	});
}

function auto_read_receipt(frm) {
	return Boolean(frm.doc.__onload && frm.doc.__onload.auto_read_receipt);
}

// quiet: sent because the form opened, so a failure is not the person's to close.
function send_read_receipt(frm, quiet) {
	frappe.call({
		doc: frm.doc,
		method: "send_read_receipt",
		silent: quiet,
		callback: function(r) {
			if (r && r.message && !quiet) {
				frappe.show_alert({ message: __('Marked as read'), indicator: 'green' });
			}
		},
		error: function() {},
	});
}
